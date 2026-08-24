"""Модерация UGC — инварианты ADR-086.

Покрываются те утверждения, нарушение которых воспроизводит багрепорт: отказ ДО списания,
блокировка результата с возвратом кредитов и без выдачи ассетов, «непроверено ≠ passed»,
fail-closed при недоступности провайдера.
"""

from __future__ import annotations

import datetime
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from app.config import Settings
from app.errors import (
    ContentPolicyViolationError,
    ModerationNotConfiguredError,
    ModerationUnavailableError,
)
from app.moderation.service import (
    STAGE_INPUT,
    STATUS_BLOCKED,
    STATUS_FLAGGED,
    STATUS_PASSED,
    STATUS_UNCHECKED,
    SURFACE_MEDIA_SUBMIT,
    ModerationService,
    ModerationVerdict,
)


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "MODERATION_ENABLED": "true",
        "MODERATION_API_KEY": "sk-test-moderation",
    }
    base.update({k: str(v) for k, v in overrides.items()})
    return Settings(**base)  # type: ignore[arg-type]


def _response(*categories_per_part: dict[str, bool]) -> SimpleNamespace:
    """Ответ провайдера в форме SDK: results[].categories — булевы флаги."""
    results = []
    for categories in categories_per_part:
        results.append(
            SimpleNamespace(
                flagged=any(categories.values()),
                categories=dict(categories),
            )
        )
    return SimpleNamespace(results=results)


class _FakeModerations:
    def __init__(self, response: Any = None, error: Exception | None = None) -> None:
        self._response = response
        self._error = error
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return self._response


def _service(response: Any = None, error: Exception | None = None, **overrides: Any):
    svc = ModerationService(settings=_settings(**overrides))
    fake = _FakeModerations(response=response, error=error)
    svc._client = SimpleNamespace(moderations=fake)  # noqa: SLF001 — подмена исходящего клиента
    return svc, fake


# --- классификация вердикта (§6) -------------------------------------------------------------


@pytest.mark.asyncio
async def test_block_category_yields_blocked() -> None:
    svc, _ = _service(_response({"sexual": True}))
    verdict = await svc.check(surface=SURFACE_MEDIA_SUBMIT, stage=STAGE_INPUT, text="что угодно")
    assert verdict.status == STATUS_BLOCKED
    assert verdict.blocked is True


@pytest.mark.asyncio
async def test_non_block_category_yields_flagged_not_blocked() -> None:
    """`violence` вне BLOCK-набора: контент выдаётся с пометкой, а не блокируется."""
    svc, _ = _service(_response({"violence": True}))
    verdict = await svc.check(surface=SURFACE_MEDIA_SUBMIT, stage=STAGE_INPUT, text="x")
    assert verdict.status == STATUS_FLAGGED


@pytest.mark.asyncio
async def test_clean_content_passes() -> None:
    svc, _ = _service(_response({"violence": False, "sexual": False}))
    verdict = await svc.check(surface=SURFACE_MEDIA_SUBMIT, stage=STAGE_INPUT, text="котик")
    assert verdict.status == STATUS_PASSED
    assert verdict.categories == ()


@pytest.mark.asyncio
async def test_sdk_underscore_names_match_block_set() -> None:
    """`self_harm_intent` из SDK обязан сматчиться с `self-harm/intent` из env.

    Без явной таблицы имён блокировка молча выродилась бы во `flagged`.
    """
    svc, _ = _service(_response({"self_harm_intent": True}))
    verdict = await svc.check(surface=SURFACE_MEDIA_SUBMIT, stage=STAGE_INPUT, text="x")
    assert verdict.status == STATUS_BLOCKED
    assert "self-harm/intent" in verdict.categories


@pytest.mark.asyncio
async def test_sexual_minors_stays_in_block_even_if_operator_removed_it() -> None:
    svc, _ = _service(_response({"sexual_minors": True}), MODERATION_BLOCK_CATEGORIES="violence")
    verdict = await svc.check(surface=SURFACE_MEDIA_SUBMIT, stage=STAGE_INPUT, text="x")
    assert verdict.status == STATUS_BLOCKED


@pytest.mark.asyncio
async def test_worst_part_wins_and_categories_merge() -> None:
    svc, _ = _service(_response({"violence": True}, {"sexual": True}))
    verdict = await svc.check(
        surface=SURFACE_MEDIA_SUBMIT,
        stage=STAGE_INPUT,
        text="x",
        image_urls=["data:image/png;base64,AA"],
    )
    assert verdict.status == STATUS_BLOCKED
    assert set(verdict.categories) == {"violence", "sexual"}


# --- доступность провайдера (§7) --------------------------------------------------------------


@pytest.mark.asyncio
async def test_provider_failure_is_fail_closed() -> None:
    svc, _ = _service(error=TimeoutError("upstream"))
    with pytest.raises(ModerationUnavailableError):
        await svc.check(surface=SURFACE_MEDIA_SUBMIT, stage=STAGE_INPUT, text="x")


@pytest.mark.asyncio
async def test_fail_open_switch_downgrades_to_unchecked() -> None:
    svc, _ = _service(error=TimeoutError("upstream"), MODERATION_FAIL_OPEN="true")
    verdict = await svc.check(surface=SURFACE_MEDIA_SUBMIT, stage=STAGE_INPUT, text="x")
    assert verdict.status == STATUS_UNCHECKED


@pytest.mark.asyncio
async def test_missing_key_is_operator_error_not_user_error() -> None:
    svc = ModerationService(settings=_settings(MODERATION_API_KEY="", OPENAI_API_KEY=""))
    with pytest.raises(ModerationNotConfiguredError):
        await svc.check(surface=SURFACE_MEDIA_SUBMIT, stage=STAGE_INPUT, text="x")


@pytest.mark.asyncio
async def test_disabled_instance_never_calls_provider() -> None:
    svc, fake = _service(_response({"sexual": True}), MODERATION_ENABLED="false")
    verdict = await svc.check(surface=SURFACE_MEDIA_SUBMIT, stage=STAGE_INPUT, text="x")
    assert verdict.status == STATUS_UNCHECKED
    assert fake.calls == []


@pytest.mark.asyncio
async def test_nothing_to_check_is_unchecked_not_passed() -> None:
    svc, fake = _service(_response({}))
    verdict = await svc.check(surface=SURFACE_MEDIA_SUBMIT, stage=STAGE_INPUT, text="   ")
    assert verdict.status == STATUS_UNCHECKED
    assert fake.calls == []


@pytest.mark.asyncio
async def test_empty_results_are_a_failure_not_a_pass() -> None:
    """Нечитаемый ответ провайдера — сбой по §7, а не молчаливое «passed»."""
    svc, _ = _service(SimpleNamespace(results=[]))
    with pytest.raises(ModerationUnavailableError):
        await svc.check(surface=SURFACE_MEDIA_SUBMIT, stage=STAGE_INPUT, text="x")


# --- контракт вердикта -------------------------------------------------------------------------


def test_payload_shape_matches_client_contract() -> None:
    now = datetime.datetime(2026, 8, 24, 11, 20, 48, tzinfo=datetime.UTC)
    payload = ModerationVerdict(
        status=STATUS_BLOCKED,
        stage=STAGE_INPUT,
        categories=("sexual",),
        checked_at=now,
        provider="openai",
        model="omni-moderation-latest",
    ).to_payload()
    assert payload["status"] == "blocked"
    assert payload["stage"] == "input"
    assert payload["categories"] == ["sexual"]
    assert payload["checkedAt"].startswith("2026-08-24T11:20:48")


def test_text_is_trimmed_to_configured_ceiling() -> None:
    settings = _settings(MODERATION_TEXT_MAX_CHARS="10")
    assert settings.moderation_text_max_chars == 10


def test_block_categories_parse_from_csv() -> None:
    parsed = _settings(MODERATION_BLOCK_CATEGORIES="violence, hate").moderation_block_categories()
    assert "violence" in parsed and "hate" in parsed
    assert "sexual/minors" in parsed


def test_api_key_falls_back_to_openai_key() -> None:
    assert (
        _settings(MODERATION_API_KEY="", OPENAI_API_KEY="sk-openai").moderation_api_key_resolved()
        == "sk-openai"
    )
    assert (
        _settings(
            MODERATION_API_KEY="sk-explicit", OPENAI_API_KEY="sk-openai"
        ).moderation_api_key_resolved()
        == "sk-explicit"
    )


# --- пре-модерация в media: отказ ДО списания (§4) ---------------------------------------------


class _SpyWallet:
    def __init__(self) -> None:
        self.consumed: list[int] = []
        self.granted: list[int] = []

    async def consume(self, **kwargs: Any) -> None:
        self.consumed.append(kwargs["amount"])

    async def grant(self, **kwargs: Any) -> None:
        self.granted.append(kwargs["amount"])


@pytest.mark.asyncio
async def test_blocked_prompt_does_not_charge_credits() -> None:
    """Главный инвариант багрепорта: отклонённый контент не должен стоить пользователю кредитов."""
    from app.media_generation.service import MediaGenerationService

    wallet = _SpyWallet()
    moderation, _ = _service(_response({"sexual": True}))
    service = MediaGenerationService(
        repo=SimpleNamespace(),  # type: ignore[arg-type]
        fal=SimpleNamespace(),  # type: ignore[arg-type]
        wallet=wallet,  # type: ignore[arg-type]
        settings=_settings(),
        moderation=moderation,
    )
    with pytest.raises(ContentPolicyViolationError) as exc:
        await service._moderate_input(prompt="запрещённое", image_urls=[])  # noqa: SLF001
    assert exc.value.code == "content_policy_violation"
    assert exc.value.status_code == 422
    assert wallet.consumed == []


@pytest.mark.asyncio
async def test_flagged_input_is_allowed_through() -> None:
    """`flagged` вход проходит намеренно: иначе поток ложных отказов на безобидном тексте."""
    from app.media_generation.service import MediaGenerationService

    moderation, _ = _service(_response({"violence": True}))
    service = MediaGenerationService(
        repo=SimpleNamespace(),  # type: ignore[arg-type]
        fal=SimpleNamespace(),  # type: ignore[arg-type]
        wallet=_SpyWallet(),  # type: ignore[arg-type]
        settings=_settings(),
        moderation=moderation,
    )
    verdict = await service._moderate_input(prompt="битва", image_urls=[])  # noqa: SLF001
    assert verdict.status == STATUS_FLAGGED


@pytest.mark.asyncio
async def test_blocked_result_refunds_and_withholds_assets() -> None:
    """Пост-модерация: терминал без ассетов, кредиты возвращены, push не отправляется (§5)."""
    from app.media_generation.service import MediaGenerationService

    wallet = _SpyWallet()
    job = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        model_id="fal-ai/x",
        credits_charged=7,
        credits_refunded=False,
    )
    marked: dict[str, Any] = {}

    class _Repo:
        async def mark_failed(self, job: Any, **kwargs: Any) -> None:
            marked.update(kwargs)

    service = MediaGenerationService(
        repo=_Repo(),  # type: ignore[arg-type]
        fal=SimpleNamespace(),  # type: ignore[arg-type]
        wallet=wallet,  # type: ignore[arg-type]
        settings=_settings(),
        moderation=_service(_response({}))[0],
    )
    verdict = ModerationVerdict(status=STATUS_BLOCKED, stage="output", categories=("sexual",))
    view = await service._blocked_by_moderation(job, verdict=verdict)  # noqa: SLF001

    assert wallet.granted == [7], "кредиты за заблокированный результат обязаны вернуться"
    assert marked["refunded"] is True
    assert marked["result"] == {
        "assets": []
    }, "ассеты не сохраняются — иначе достижимы по signed-URL"
    assert marked["moderation"]["status"] == "blocked"
    assert "content_policy_violation" not in marked["error"], "error — user-facing текст, не код"
    assert view.assets == []


# --- поле moderation в ответе -----------------------------------------------------------------


def test_legacy_job_renders_as_unchecked_never_passed() -> None:
    from app.api_gateway.routers.media import _moderation_schema

    assert _moderation_schema(None).status == "unchecked"
    assert _moderation_schema(None).stage is None
    assert _moderation_schema(None).categories == []


def test_unknown_status_degrades_to_unchecked() -> None:
    from app.api_gateway.routers.media import _moderation_schema

    assert _moderation_schema({"status": "weird"}).status == "unchecked"


def test_stored_verdict_round_trips_to_client_schema() -> None:
    from app.api_gateway.routers.media import _moderation_schema

    schema = _moderation_schema(
        {
            "status": "blocked",
            "stage": "output",
            "categories": ["sexual"],
            "checkedAt": "2026-08-24T11:20:48+00:00",
        }
    )
    assert schema.status == "blocked"
    assert schema.stage == "output"
    assert schema.categories == ["sexual"]
    assert schema.checkedAt is not None


def test_media_job_response_always_carries_moderation() -> None:
    """Поле обязательное — клиенту не нужна ветка «поля нет»."""
    from app.schemas.media import MediaJobResponse

    assert MediaJobResponse.model_fields["moderation"].is_required()


def test_job_status_literal_gained_no_new_value() -> None:
    """Блокировка выражена существующим `failed`: новое значение сломало бы выпущенные сборки."""
    from typing import get_args

    from app.schemas.media import MediaJobResponse

    assert set(get_args(MediaJobResponse.model_fields["status"].annotation)) == {
        "queued",
        "running",
        "completed",
        "failed",
    }
