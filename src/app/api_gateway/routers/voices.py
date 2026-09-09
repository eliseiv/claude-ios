"""Speech-output routes: GET /v1/voices and POST /v1/chat/speech (ADR-100).

Both live on ONE router under ONE tag despite the different prefixes: the tag groups by user
scenario (08-api-documentation R4), and «выбрать голос → послушать ответ» is one scenario.
``POST /v1/chat/speech`` executes no chat turn, so filing it under `Chat` would mislead a tester
reading the docs.

The instance flag ``VOICE_OUTPUT_ENABLED`` gates the catalog, the endpoint, the preference and
the prompt layer on ONE axis (ADR-100 §8, the ADR-097 §7 pattern): with it off the catalog answers
``200 {enabled:false}`` — never ``404`` (indistinguishable from "old backend / wrong path") and
never ``503`` (this is a normal default, not a mis-configuration).
"""

from __future__ import annotations

import base64
import logging
import time
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Request

from app import instance_config
from app.api_gateway.rate_limit import enforce_other_limits, enforce_speech_limits
from app.api_gateway.routers.presets import resolve_presets_locale
from app.chat.speech import SpeechSynthesisService
from app.chat.voice_mode import voice_mode_available
from app.chat.voices import resolve_default_voice_id, voice_catalog
from app.config import get_settings
from app.deps import CurrentUser, get_preferences_service, get_speech_service, require_owner
from app.errors import AppError, RateLimitedError
from app.observability.context import set_session_id
from app.observability.logging import get_logger, log_event
from app.observability.metrics import speech_synthesis_total
from app.preferences.service import PreferencesService
from app.schemas.voices import ChatSpeechRequest, ChatSpeechResponse, VoicesResponse

logger = get_logger(__name__)

router = APIRouter(tags=["Voices"])

# ADR-100 §10: отображение отказа в метку исхода. Ключ — машиночитаемый `code` ошибки, то есть то
# самое значение, которое видит клиент, а не имя класса: разойдись они, метрика считала бы одно, а
# приложение получало другое.
#
# Здесь ТОЛЬКО исходы, названные предикатом «что случилось с деньгами пользователя». Балансовых и
# авторизационных отказов (409/404/403/429) в таблице НЕТ намеренно, и это симметричный критерий
# против ПЕРЕОЦЕНКИ: они не говорят ничего ни об аварии поставщика, ни о наших тратах, а шумная
# причина внутри класса приучает игнорировать весь класс — и вместе с шумом перестают реагировать
# на `upstream_error`, за которым стоит реальная поломка синтеза.
_OUTCOME_BY_ERROR_CODE: dict[str, str] = {
    "voice_output_disabled": "disabled",
    "voice_output_not_configured": "not_configured",
    "nothing_to_speak": "nothing_to_speak",
    "upstream_error": "upstream_error",
    "gateway_timeout": "timeout",
}


@router.get(
    "/v1/voices",
    response_model=VoicesResponse,
    summary="Каталог голосов озвучки",
    description=(
        "Возвращает голоса для экрана «голос по умолчанию»: `id` (стабильный slug), `name` и "
        "`gender`. Выбранный `id` сохраняется через `PATCH /v1/preferences` в поле "
        "`defaultVoiceId`. Поле `enabled` сообщает, включена ли озвучка на инстансе: при `false` "
        "список пуст, `defaultVoiceId` равен `null`, и по этому же полю приложение прячет кнопку "
        "воспроизведения. Поле `defaultVoiceId` — голос, который прозвучит у этого пользователя "
        "в чате без персонажа; используйте как предвыбранную строку. Голоса персонажей в каталог "
        "не входят: они закреплены за персонажем и пользователем не выбираются. Поле "
        "`voiceModeEnabled` сообщает отдельно, доступен ли на инстансе живой голосовой диалог "
        "(WebSocket `/v1/chat/voice`): по нему прячется кнопка голосового режима, и оно не "
        "выводится из `enabled`. Имена отдаются на "
        "выбранном языке: приоритет у параметра `locale`, затем заголовок `Accept-Language`, "
        "затем язык по умолчанию для инстанса. Read-only, без состояния."
    ),
)
async def list_voices(
    request: Request,
    current: CurrentUser,
    prefs: Annotated[PreferencesService, Depends(get_preferences_service)],
    locale: str | None = Query(
        default=None,
        description=(
            "Желаемый язык каталога (например `en`, `ru` или `zh-Hans`). Если не указан — язык "
            "определяется по заголовку `Accept-Language`, иначе используется язык по умолчанию "
            "для инстанса. Недопустимое значение возвращает ошибку 422."
        ),
        examples=["ru", "zh-Hans"],
    ),
    accept_language: str | None = Header(default=None),
) -> VoicesResponse:
    if not await enforce_other_limits(user_id=current.user_id):
        raise RateLimitedError("rate limit exceeded")
    settings = get_settings()
    # Локаль резолвится и при выключенной фиче: явный `?locale=` вне набора остаётся 422 на любом
    # инстансе, и форма ответа одинакова в обоих случаях. Третьей переменной под язык каталога не
    # заводится — тот же PRESETS_DEFAULT_LOCALE, что у пресетов и персонажей (TD-035).
    resolved = resolve_presets_locale(
        query_locale=locale,
        accept_language=accept_language,
        default_locale=instance_config.presets_default_locale(),
    )
    enabled = settings.voice_output_enabled
    # Каталог отвечает по ФЛАГУ, а не по ключу (ADR-100 §8): он не выполняет работы, для которой
    # нужен OPENAI_API_KEY, поэтому 503 здесь был бы отказом на ровном месте.
    default_voice_id = (
        resolve_default_voice_id(await prefs.get_default_voice_id(current.user_id))
        if enabled
        else None
    )
    return VoicesResponse.model_validate(
        {
            "enabled": enabled,
            "locale": resolved,
            "defaultVoiceId": default_voice_id,
            "voices": voice_catalog(resolved) if enabled else [],
            # ADR-104: ось голосового РЕЖИМА отдельна от оси озвучки и вычисляется в одной точке
            # (составное условие из трёх флагов). Поле присутствует всегда — в том числе при
            # выключенной озвучке, где оно по построению `false`: приложение обязано отличать
            # «инстанс не умеет живой диалог» от «бэкенд старее фичи», а отсутствующее поле
            # неотличимо от второго.
            "voiceModeEnabled": voice_mode_available(settings=settings),
        }
    )


@router.post(
    "/v1/chat/speech",
    response_model=ChatSpeechResponse,
    summary="Озвучить ответ ассистента",
    description=(
        "Синтезирует речь по уже полученному ответу ассистента и возвращает звуковой файл в "
        "base64. Ход чата этой ручкой не выполняется и не изменяется: ответы `/v1/chat/run` и "
        "`/v1/chat/v2/run`, включая потоковые, остаются прежними. Адресуется конкретный шаг "
        "(`stepId`), поэтому озвучить можно и вчерашний ответ при холодном старте. Голос выбирает "
        "сервер: у чата с персонажем — голос персонажа, иначе настройка пользователя "
        "(`defaultVoiceId`), иначе голос инстанса; в запросе голос не передаётся. Текст перед "
        "синтезом приводится к произносимому виду (уходят блоки кода, таблицы, ссылки, разметка и "
        "эмодзи) и ограничивается по длине — при срабатывании ограничения приходит "
        "`truncated: true`, а сам текст ответа остаётся полным. Озвучка тарифицируется отдельно "
        "от хода; повторная озвучка того же ответа тем же голосом бесплатна навсегда "
        "(`creditsCharged: 0`), смена голоса — новая озвучка. Сервер звук не хранит: кэшируйте "
        "файл у себя по паре `stepId` + `voiceId`. Свой лимит частоты."
    ),
)
async def synthesize_speech(
    body: ChatSpeechRequest,
    request: Request,
    current: CurrentUser,
    service: Annotated[SpeechSynthesisService, Depends(get_speech_service)],
) -> ChatSpeechResponse:
    require_owner(body.userId, current)
    if not await enforce_speech_limits(user_id=current.user_id):
        raise RateLimitedError("rate limit exceeded")
    set_session_id(str(body.sessionId))
    started = time.monotonic()
    try:
        result = await service.synthesize(
            user_id=current.user_id, session_id=body.sessionId, step_id=body.stepId
        )
    except AppError as exc:
        # PRODUCER метрики — здесь, на рабочем пути ручки, а не в хелпере синтеза: инкремент в
        # хелпере не покрыл бы отказы, до него не дошедшие (выключенный флаг, пустой ключ, пустой
        # очищенный текст) — ровно те, ради различения которых счётчик и заведён.
        outcome = _OUTCOME_BY_ERROR_CODE.get(exc.code)
        if outcome is not None:
            speech_synthesis_total.labels(outcome=outcome).inc()
            _log_outcome(
                outcome=outcome,
                step_id=str(body.stepId),
                voice_id=None,
                started=started,
            )
        raise
    outcome = "repeat" if result.repeat else "ok"
    speech_synthesis_total.labels(outcome=outcome).inc()
    _log_outcome(
        outcome=outcome,
        step_id=str(result.step_id),
        voice_id=result.voice_id,
        started=started,
        source_chars=result.source_chars,
        spoken_chars=result.spoken_chars,
        truncated=result.truncated,
    )
    return ChatSpeechResponse(
        stepId=result.step_id,
        voiceId=result.voice_id,
        mediaType=result.media_type,
        # base64 в JSON, а не бинарное тело: единый конверт ошибки `{error:{code,message,
        # requestId}}` существует только для JSON, и симметрия со входом — голос В сервис тоже
        # приходит inline base64 (ADR-020, ADR-095). Цена решения названа в ADR-100 §2: +33 %
        # байт, ограниченные сверху тем же потолком длины, что и счёт поставщика.
        audio=base64.b64encode(result.audio).decode("ascii"),
        truncated=result.truncated,
        creditsCharged=result.credits_charged,
    )


def _log_outcome(
    *,
    outcome: str,
    step_id: str,
    voice_id: str | None,
    started: float,
    source_chars: int | None = None,
    spoken_chars: int | None = None,
    truncated: bool | None = None,
) -> None:
    """Структурированная запись исхода озвучки — БЕЗ текста и без байтов аудио (05-security).

    В логе только ограниченные значения и длины: текст шага, очищенный текст и звук относятся к
    пользовательскому контенту наравне с вложениями и не логируются ни в каком виде.
    """
    log_event(
        logger,
        logging.INFO,
        "speech_synthesis",
        outcome=outcome,
        stepId=step_id,
        voiceId=voice_id,
        sourceChars=source_chars,
        spokenChars=spoken_chars,
        truncated=truncated,
        latencyMs=int((time.monotonic() - started) * 1000),
    )
