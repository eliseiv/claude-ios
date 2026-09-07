"""Schemas for GET /v1/voices and POST /v1/chat/speech (ADR-100).

Read-only catalog + one synthesis call. The synthesis triple of a voice entry (``provider``,
``provider_voice_id``, ``instructions``) is deliberately absent from BOTH contracts: those are
internal parameters tuned by ear, and handing them out would turn a wording fix into an app
release (the same reasoning that keeps ``persona`` off the characters contract, ADR-097 §3).
"""

from __future__ import annotations

import uuid
from typing import Literal

from pydantic import Field

from app.schemas.common import StrictModel


class VoiceInfo(StrictModel):
    id: str = Field(
        description=(
            "Стабильный slug голоса. Сохраняется как `defaultVoiceId` в `/v1/preferences` и "
            "участвует в ключе локального кэша аудио вместе с `stepId`. Не зависит от языка."
        )
    )
    name: str = Field(description="Отображаемое имя голоса в настройках (на языке ответа).")
    gender: Literal["male", "female"] = Field(
        description="Мужской или женский голос — ось, по которой выбирают в настройках."
    )


class VoicesResponse(StrictModel):
    enabled: bool = Field(
        description=(
            "Включена ли озвучка ответа на этом инстансе. При `false` список пуст, "
            "`defaultVoiceId` равен `null`, а запрос озвучки возвращает ошибку 422. Прячьте и "
            "настройку голоса, и кнопку воспроизведения по этому полю."
        )
    )
    locale: str = Field(
        description=(
            "Язык, фактически применённый к именам голосов (из числа поддерживаемых, "
            "например `en` или `ru`)."
        ),
        examples=["en"],
    )
    defaultVoiceId: str | None = Field(
        description=(
            "Голос, которым прозвучит ответ у ЭТОГО пользователя в чате без персонажа: его "
            "собственная настройка либо голос инстанса. Используйте как предвыбранную строку "
            "списка. `null` только когда озвучка на инстансе выключена."
        )
    )
    voices: list[VoiceInfo] = Field(
        description=(
            "Голоса, доступные для выбора; порядок элементов = порядок на экране настроек. "
            "Голоса персонажей в список не входят: они закреплены за персонажем и не выбираются."
        )
    )


class ChatSpeechRequest(StrictModel):
    userId: uuid.UUID = Field(description="Идентификатор пользователя; должен совпадать с `sub`.")
    sessionId: uuid.UUID = Field(description="Чат, которому принадлежит озвучиваемый ответ.")
    stepId: uuid.UUID = Field(
        description=(
            "Идентификатор шага с ответом ассистента — то же значение, что пришло в `stepId` "
            "ответа чата и видно в `steps[].id` истории чата. Адресуется конкретный ответ, а не "
            "«последний»: на двух быстрых ходах подряд «последний» разошёлся бы с тем "
            "сообщением, на котором пользователь нажал кнопку."
        )
    )


class ChatSpeechResponse(StrictModel):
    stepId: uuid.UUID = Field(description="Шаг, который озвучен (эхо запроса).")
    voiceId: str = Field(
        description=(
            "Голос, которым фактически озвучен ответ. Ключ локального кэша аудио — пара "
            "`stepId` + `voiceId`: после смены голоса в настройках запросите шаг заново и "
            "получите новую пару."
        )
    )
    mediaType: str = Field(
        description="MIME синтезированного файла (например `audio/mpeg`) — по нему выбирайте "
        "декодер.",
        examples=["audio/mpeg"],
    )
    audio: str = Field(
        description=(
            "Синтезированный файл целиком в base64. Длина ограничена сверху потолком длины "
            "речи, поэтому файл рассчитан на проигрывание из памяти, а не на потоковую загрузку."
        )
    )
    truncated: bool = Field(
        description=(
            "`true` — прозвучал не весь ответ: озвучка обрезана по потолку длины. Текст ответа "
            "при этом полный и не изменён; покажите пользователю, что озвучено только начало."
        )
    )
    creditsCharged: int = Field(
        description=(
            "Сколько кредитов списал ЭТОТ вызов. `0` — за этот ответ этим голосом уже "
            "заплачено раньше (повтор, второе устройство, переустановка приложения): повтор "
            "бесплатен навсегда. Обновляйте баланс по этому полю."
        )
    )
