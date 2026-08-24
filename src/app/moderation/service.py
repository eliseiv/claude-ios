"""Сервис модерации UGC поверх OpenAI omni-moderation (ADR-086).

Вердикт вычисляется ДЕТЕРМИНИРОВАННО из ответа провайдера (§6): сработала категория из BLOCK →
``blocked``; иначе ``flagged`` провайдера → ``flagged``; иначе ``passed``. Порогов по
``category_scores`` этот модуль не вводит (Q-086-4).

Fail-closed (§7): любой сбой провайдера — таймаут, сеть, 5xx, нечитаемый ответ — поднимает
``ModerationUnavailableError`` (503), кроме случая, когда оператор аварийно включил
``MODERATION_FAIL_OPEN``; тогда вердикт ``unchecked`` и WARNING в лог.

Модуль НИКОГДА не логирует проверяемый контент: ни промпт, ни текст сообщения, ни base64, ни URL
ассета целиком (allowlist полей лога — §10).
"""

from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass, field
from typing import Any, cast

import openai

from app.config import Settings
from app.errors import ModerationNotConfiguredError, ModerationUnavailableError
from app.observability.logging import log_event
from app.observability.metrics import moderation_decisions_total, moderation_errors_total

logger = logging.getLogger(__name__)

STATUS_PASSED = "passed"
STATUS_FLAGGED = "flagged"
STATUS_BLOCKED = "blocked"
STATUS_UNCHECKED = "unchecked"

STAGE_INPUT = "input"
STAGE_OUTPUT = "output"

SURFACE_CHAT = "chat"
SURFACE_MEDIA_SUBMIT = "media_submit"
SURFACE_MEDIA_RESULT = "media_result"
SURFACE_MEDIA_UPLOAD = "media_upload"

# Худший-выигрывает при агрегации по частям запроса (§6).
_SEVERITY = {STATUS_UNCHECKED: 0, STATUS_PASSED: 1, STATUS_FLAGGED: 2, STATUS_BLOCKED: 3}

# SDK отдаёт поля категорий с подчёркиваниями (``self_harm_intent``), а BLOCK-набор в env записан
# в нотации API (``self-harm/intent``). Сопоставление задано явной таблицей, а не преобразованием
# строки: механический replace свёл бы ``self_harm`` и ``sexual_minors`` к разным разделителям и
# BLOCK-набор перестал бы матчиться — то есть блокировка молча выродилась бы в flagged.
_CATEGORY_NAMES = {
    "harassment": "harassment",
    "harassment_threatening": "harassment/threatening",
    "hate": "hate",
    "hate_threatening": "hate/threatening",
    "illicit": "illicit",
    "illicit_violent": "illicit/violent",
    "self_harm": "self-harm",
    "self_harm_intent": "self-harm/intent",
    "self_harm_instructions": "self-harm/instructions",
    "sexual": "sexual",
    "sexual_minors": "sexual/minors",
    "violence": "violence",
    "violence_graphic": "violence/graphic",
}


@dataclass(frozen=True)
class ModerationVerdict:
    """Вердикт модерации — то, что уходит клиенту и ложится в ``media_jobs.moderation``."""

    status: str
    stage: str | None = None
    categories: tuple[str, ...] = ()
    checked_at: datetime.datetime | None = None
    provider: str | None = None
    model: str | None = None

    @property
    def blocked(self) -> bool:
        return self.status == STATUS_BLOCKED

    def to_payload(self) -> dict[str, Any]:
        """JSONB-представление для ``media_jobs.moderation`` (§10)."""
        return {
            "status": self.status,
            "stage": self.stage,
            "categories": list(self.categories),
            "checkedAt": self.checked_at.isoformat() if self.checked_at else None,
            "provider": self.provider,
            "model": self.model,
        }


def unchecked_verdict() -> ModerationVerdict:
    """Вердикт «не проверялось» — честнее «тихого passed» о непроверенном контенте (§8)."""
    return ModerationVerdict(status=STATUS_UNCHECKED)


def _merge(verdicts: list[ModerationVerdict], *, stage: str) -> ModerationVerdict:
    """Вердикт запроса = худший по частям; categories = объединение сработавших (§6)."""
    if not verdicts:
        return unchecked_verdict()
    worst = max(verdicts, key=lambda v: _SEVERITY[v.status])
    categories = sorted({c for v in verdicts for c in v.categories})
    return ModerationVerdict(
        status=worst.status,
        stage=stage,
        categories=tuple(categories),
        checked_at=worst.checked_at,
        provider=worst.provider,
        model=worst.model,
    )


@dataclass
class ModerationService:
    """Клиент модерации. Один вызов на набор частей (текст + изображения)."""

    settings: Settings
    _client: openai.AsyncOpenAI | None = field(default=None, init=False, repr=False)

    @property
    def enabled(self) -> bool:
        return self.settings.moderation_enabled

    def _ensure_client(self) -> openai.AsyncOpenAI:
        if self._client is not None:
            return self._client
        api_key = self.settings.moderation_api_key_resolved()
        if not api_key:
            # Проблема оператора (ключ не доставлен), а не пользователя — 503, не 4xx.
            raise ModerationNotConfiguredError("moderation is enabled but no API key resolves")
        self._client = openai.AsyncOpenAI(
            api_key=api_key,
            base_url=self.settings.moderation_base_url,
            timeout=self.settings.moderation_timeout_seconds,
            max_retries=self.settings.moderation_max_retries,
        )
        return self._client

    async def check(
        self,
        *,
        surface: str,
        stage: str,
        text: str | None = None,
        image_urls: list[str] | None = None,
    ) -> ModerationVerdict:
        """Проверить набор частей одним вызовом. Возвращает агрегированный вердикт.

        ``image_urls`` принимает и https-URL, и data-URI — omni-moderation ест оба.
        Ничего не проверяем ⇒ ``unchecked``: врать «passed» о непроверенном нельзя (§8).
        """
        if not self.enabled:
            return unchecked_verdict()
        # Тип берём из SDK, чтобы mypy проверял форму частей, а не dict[str, Any].
        parts: list[Any] = []
        trimmed = (text or "").strip()[: self.settings.moderation_text_max_chars]
        if trimmed:
            parts.append({"type": "text", "text": trimmed})
        for url in image_urls or []:
            parts.append({"type": "image_url", "image_url": {"url": url}})
        if not parts:
            return unchecked_verdict()

        client = self._ensure_client()
        started = datetime.datetime.now(datetime.UTC)
        try:
            response = await client.moderations.create(
                model=self.settings.moderation_model, input=cast(Any, parts)
            )
            verdict = self._verdict_from(response, stage=stage, checked_at=started)
        except Exception as exc:  # noqa: BLE001 — любой сбой провайдера = единая политика §7
            return self._on_failure(exc, surface=surface, stage=stage)

        moderation_decisions_total.labels(
            surface=surface, stage=stage, decision=verdict.status
        ).inc()
        log_event(
            logger,
            logging.INFO,
            "moderation_outcome",
            surface=surface,
            stage=stage,
            decision=verdict.status,
            categories=list(verdict.categories),
            provider="openai",
            model=self.settings.moderation_model,
            latencyMs=int((datetime.datetime.now(datetime.UTC) - started).total_seconds() * 1000),
        )
        return verdict

    def _verdict_from(
        self, response: Any, *, stage: str, checked_at: datetime.datetime
    ) -> ModerationVerdict:
        block = self.settings.moderation_block_categories()
        per_part: list[ModerationVerdict] = []
        for result in getattr(response, "results", None) or []:
            categories = _flagged_categories(result)
            if categories & block:
                status = STATUS_BLOCKED
            elif bool(getattr(result, "flagged", False)) or categories:
                status = STATUS_FLAGGED
            else:
                status = STATUS_PASSED
            per_part.append(
                ModerationVerdict(
                    status=status,
                    stage=stage,
                    categories=tuple(sorted(categories)),
                    checked_at=checked_at,
                    provider="openai",
                    model=self.settings.moderation_model,
                )
            )
        if not per_part:
            # Ответ без результатов нечитаем — по §7 это сбой, а не «passed».
            raise ValueError("moderation response carried no results")
        return _merge(per_part, stage=stage)

    def _on_failure(self, exc: Exception, *, surface: str, stage: str) -> ModerationVerdict:
        if self.settings.moderation_fail_open:
            moderation_errors_total.labels(reason="fail_open").inc()
            log_event(
                logger,
                logging.WARNING,
                "moderation_fail_open",
                surface=surface,
                stage=stage,
                error=type(exc).__name__,
            )
            return unchecked_verdict()
        moderation_errors_total.labels(reason="provider_error").inc()
        log_event(
            logger,
            logging.ERROR,
            "moderation_unavailable",
            surface=surface,
            stage=stage,
            error=type(exc).__name__,
        )
        raise ModerationUnavailableError("moderation provider is unavailable") from exc


def _flagged_categories(result: Any) -> set[str]:
    """Сработавшие категории одного результата, независимо от формы SDK-объекта."""
    categories = getattr(result, "categories", None)
    if categories is None:
        return set()
    raw: dict[str, Any]
    if hasattr(categories, "model_dump"):
        raw = categories.model_dump()
    elif isinstance(categories, dict):
        raw = categories
    else:
        raw = {k: v for k, v in vars(categories).items() if not k.startswith("_")}
    flagged: set[str] = set()
    for key, value in raw.items():
        if value is not True:
            continue
        # Неизвестный ключ (провайдер добавил категорию) сохраняем как есть: он не попадёт в
        # BLOCK-набор, но даст flagged — тихо терять новую категорию нельзя.
        flagged.add(_CATEGORY_NAMES.get(key, key))
    return flagged
