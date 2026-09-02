"""Распознавание голосовых сообщений (ADR-095).

Аудио НЕ уходит в языковую модель. Оно распознаётся здесь, и дальше по конвейеру идёт обычный
текст. Причина не в экономии: провайдеры принимают звук по-разному и не все — вовсе, поэтому
передача аудио вглубь сделала бы работу голоса зависящей от того, какой провайдер настроен на
инстансе. После распознавания голосовой ход неотличим от набранного руками: те же модерация,
инструменты, история и тарификация.

Провайдер распознавания — OpenAI: его ключ уже есть на каждом инстансе, отдельной интеграции и
отдельного секрета не заводится.
"""

from __future__ import annotations

import logging

import openai

from app.config import get_settings
from app.errors import UpstreamError, ValidationFailedError
from app.observability.logging import get_logger, log_event

logger = get_logger(__name__)

# Расширение файла, которое отдаём распознавателю. Он выбирает декодер по имени, а не по
# содержимому: без осмысленного расширения корректный m4a отвергается как «unrecognized format».
_EXTENSION_BY_MEDIA_TYPE = {
    "audio/mp4": "m4a",
    "audio/m4a": "m4a",
    "audio/mpeg": "mp3",
    "audio/wav": "wav",
    "audio/webm": "webm",
    "audio/ogg": "ogg",
}


class TranscriptionClient:
    """Тонкая обёртка над OpenAI audio.transcriptions."""

    def __init__(self) -> None:
        settings = get_settings()
        self._model = settings.transcription_model
        self._client = openai.AsyncOpenAI(
            api_key=settings.openai_api_key or "placeholder",
            timeout=settings.transcription_timeout_seconds,
            max_retries=0,
        )

    async def transcribe(self, audio: bytes, media_type: str) -> str:
        """Вернуть распознанный текст. Пустая запись даёт пустую строку, а не ошибку.

        Молчаливая запись — обычное дело (палец соскользнул), и 422 на неё выглядел бы поломкой
        приложения. Пустую строку вызывающий отличает от текста и решает сам.
        """
        extension = _EXTENSION_BY_MEDIA_TYPE.get(media_type)
        if extension is None:  # pragma: no cover — тип уже проверен allowlist'ом вложений
            raise ValidationFailedError(f"unsupported audio media type: {media_type}")
        try:
            result = await self._client.audio.transcriptions.create(
                model=self._model,
                file=(f"voice.{extension}", audio, media_type),
                response_format="text",
            )
        except openai.APIError as exc:
            # Наружу — 502, а не 500: отказ распознавателя это отказ ВЫШЕСТОЯЩЕЙ службы, и по
            # коду видно, что чинить не у нас. Текст исключения не пробрасываем: он цитирует
            # запрос целиком.
            log_event(
                logger,
                logging.WARNING,
                "transcription_failed",
                model=self._model,
                mediaType=media_type,
                errorType=type(exc).__name__,
            )
            raise UpstreamError("transcription provider error") from exc
        text = result if isinstance(result, str) else getattr(result, "text", "")
        return (text or "").strip()
