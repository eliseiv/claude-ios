"""Integration: прикладные close-коды голосового режима (ADR-104 §9).

Норма — `docs/modules/chat-orchestrator/09-testing.md §Голосовой режим`, строка «Каждый
объявленный close-код имеет производящий кейс (diff полноты)».

Кодов РОВНО ПЯТЬ, и у каждого здесь производящее событие. Обратная сторона («ни один тест не
наблюдает close-кода вне этих пяти») проверяется не отдельным кейсом, а самим стендом:
`VoiceSocket.next` отвергает любой код вне объявленных пяти в КАЖДОМ тесте набора — реализация,
завёдшая шестой код, уронит все кейсы, где он появится.

`4400` производится кейсами отказа `start` в `test_voice_mode_failures_adr104.py`
(чужой `sessionId`, `assistantMode="code"`), `4408` — кейсом idle-таймаута в
`test_voice_mode_metrics_adr104.py`. Здесь — оставшиеся три.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.integration.test_voice_mode_session_adr104 import seed_voice_user
from tests.voice_harness import (
    ALLOWED_CLOSE_CODES,
    first_of,
)


async def test_expired_token_mid_session_closes_with_4401(
    voice_stand: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Истёкший JWT посреди сеанса → `error unauthorized` + close `4401`.

    Свежесть токена переспрашивается на КАЖДОМ ходе, но заголовки рукопожатия неизменны, поэтому
    истечение вносится в точку проверки: первый вызов (рукопожатие) идёт настоящей функцией,
    последующие отвечают так, как ответила бы она на протухшем токене. Прикладной код обязателен,
    чтобы приложение отличило «нас выгнали» от «сеть пропала».
    """
    from app.api_gateway.routers import chat_voice
    from app.errors import UnauthorizedError

    stand = await voice_stand()
    uid = await seed_voice_user(stand)
    original = chat_voice.verify_bearer_token
    calls = {"n": 0}

    def _expiring(authorization: str | None) -> Any:
        calls["n"] += 1
        if calls["n"] > 1:
            raise UnauthorizedError("token expired")
        return original(authorization)

    monkeypatch.setattr(chat_voice, "verify_bearer_token", _expiring)

    socket, _ = await stand.session(uid)
    await socket.begin_utterance()
    error = await socket.next()
    closed = await socket.next()

    assert error["type"] == "error"
    assert (error["code"], error["scope"]) == ("unauthorized", "session")
    assert closed["code"] == 4401
    assert stand.llm.calls == [], "ход не начинается"


async def test_unexpected_handler_failure_closes_with_4500(
    voice_stand: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Непредвиденная ошибка обработчика сокета → close `4500`.

    Сбой вносится в РЕАЛЬНУЮ точку пути `start` (резолв голоса читает настройку пользователя), а
    не подменой самого обработчика: проверяется его ветка закрытия, а не заглушка.
    """
    from app.preferences.service import PreferencesService

    stand = await voice_stand()
    uid = await seed_voice_user(stand)

    async def _boom(_self: Any, _user_id: Any) -> str | None:
        raise RuntimeError("unexpected failure inside the socket handler")

    monkeypatch.setattr(PreferencesService, "get_default_voice_id", _boom)

    socket = await stand.connect(uid)
    await socket.send_frame({"type": "start"})
    closed = await socket.next()

    assert closed["type"] == "__close__"
    assert closed["code"] == 4500


async def test_client_closing_the_socket_is_a_normal_1000(voice_stand: Any) -> None:
    """Штатное завершение клиентом → close `1000`."""
    stand = await voice_stand()
    uid = await seed_voice_user(stand)
    stand.script("Ответ перед штатным закрытием. ")

    socket, _ = await stand.session(uid)
    await socket.turn()
    await socket.disconnect()
    closed = await socket.next()

    assert closed["type"] == "__close__"
    assert closed["code"] == 1000


async def test_axis_dropped_mid_session_ends_the_session_normally(
    voice_stand: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ось режима снята из панели посреди сеанса → `voice_mode_disabled` + close `1000`.

    Ось переспрашивается на КАЖДОМ ходе, а не только в рукопожатии: половина `VOICE_INPUT_ENABLED`
    меняется на лету, и обещание «оператор гасит режим немедленно» иначе не выполнялось бы.
    Шестого close-кода под это событие не заводится: сеанс закончился не сбоем, а тем, что услуги
    на инстансе больше нет.
    """
    from app.config import get_settings

    stand = await voice_stand()
    uid = await seed_voice_user(stand)
    stand.script("Ответ до снятия флага. ")

    socket, _ = await stand.session(uid)
    frames = await socket.turn()
    assert first_of(frames, "done")["response"]["status"] == "assistant_message"

    monkeypatch.setenv("VOICE_INPUT_ENABLED", "false")
    get_settings.cache_clear()

    await socket.begin_utterance()
    error = await socket.next()
    closed = await socket.next()

    assert (error["code"], error["scope"]) == ("voice_mode_disabled", "session")
    assert closed["code"] == 1000
    assert closed["code"] in ALLOWED_CLOSE_CODES


def test_the_router_declares_exactly_five_application_close_codes() -> None:
    """Полнота с другой стороны: объявленных прикладных кодов ровно пять, и `4429` среди них нет.

    Кейс дополняет производящие: он ловит ОБЪЯВЛЕНИЕ шестого кода даже раньше, чем найдётся путь,
    который его отправляет, — а мёртвое объявление и есть та форма, из-за которой запрет на
    `4429` был записан в контракт прямым текстом.
    """
    from app.api_gateway.routers import chat_voice

    declared = {
        value
        for name, value in vars(chat_voice).items()
        if name.startswith("CLOSE_") and isinstance(value, int)
    }
    assert declared == set(ALLOWED_CLOSE_CODES)
    assert 4429 not in declared
