"""Integration: настройки инстанса → потребители, которых не было в покрытии (ADR-099 §8.1).

⚠️ **Зачем отдельный файл.** Оба кейса здесь требуют окружения, которого нет в фикстуре
``test_admin_economics_wiring_adr099.py``: одному нужна сконфигурированная RU-касса
(``CLOUDPAYMENTS_APP_ID`` + токен), другому — включённая озвучка и ключ провайдера. Добавить эти
переменные в общую фикстуру значило бы поменять окружение двум десяткам действующих кейсов ради
двух новых.

Каждый кейс называет пару **producer → consumer**. Оба потребителя — «поздние»: они появились
ПОСЛЕ того, как соответствующая настройка уже была объявлена, и потому легко остаются вне цепи.
Именно эта форма — «величина доходит до одних потребителей и не доходит до других» — и стоит
отдельного файла: правка, применённая к пресетам и не применённая к озвучке, выглядит рабочей.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from tests.conftest import FakeAnthropicClient, FakeStoreKitVerifier, auth_headers, seed_user

_ADMIN_SECRET = "cross-consumers-admin-key-0123456789ab"
_H = {"X-Admin-Key": _ADMIN_SECRET}

_CHARACTER_ID = "vampire_lord"
_CHARACTER_VOICE_ID = "char_vampire_lord"
_INSTANCE_VOICE_ID = "default_female"


@pytest.fixture(autouse=True)
def _enable_instance_config_loggers() -> None:
    """Alembic-миграция выключает каждый `app.*`-логгер на весь процесс (см. `_migrated`)."""
    for name in ("app.instance_config", "app.instance_config.media_pricing"):
        logging.getLogger(name).disabled = False


class _CapturedCall:
    """Перехват ИСХОДЯЩЕГО вызова провайдера: тело — единственное место, где видно `locale`."""

    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []

    def client_factory(self, **_kwargs: Any) -> Any:
        captured = self

        class _Response:
            status_code = 200

            @staticmethod
            def json() -> dict[str, Any]:
                return {"segment": {"code": "a", "isControl": False}, "created": True}

        class _Client:
            async def __aenter__(self) -> _Client:
                return self

            async def __aexit__(self, *_a: Any) -> None:
                return None

            async def post(self, _url: str, *, json: dict[str, Any], headers: Any) -> _Response:
                _ = headers
                captured.payloads.append(json)
                return _Response()

        return _Client()


@pytest.fixture
def captured() -> _CapturedCall:
    return _CapturedCall()


@pytest.fixture
async def crossed(
    monkeypatch: pytest.MonkeyPatch,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    fake_storekit: FakeStoreKitVerifier,
    captured: _CapturedCall,
) -> AsyncIterator[AsyncClient]:
    """ОДНО приложение: admin-поверхность и оба «поздних» потребителя в одном процессе.

    Снимок оверлеев живёт в процессе, поэтому два клиента разошлись бы состоянием, и кейс
    проверял бы собственную фикстуру, а не цепь.
    """
    from app import deps
    from app.api_gateway import rate_limit
    from app.api_gateway.routers import billing_cloudpayments as cp_router
    from app.api_gateway.routers import chat as chat_router
    from app.api_gateway.routers import voices as voices_router
    from app.billing_cloudpayments import experiments as experiments_mod
    from app.chat import anthropic_client as anthropic_mod
    from app.chat.speech import SpeechClient
    from app.main import create_app
    from app.subscription import storekit as storekit_mod

    monkeypatch.setenv("ADMIN_API_SECRET", _ADMIN_SECRET)
    monkeypatch.setenv("ADMIN_API_SECRET_PREV", "")
    monkeypatch.setenv("ADMIN_API_KEY", "")
    monkeypatch.setenv("CHARACTERS_ENABLED", "true")
    monkeypatch.setenv("VOICE_OUTPUT_ENABLED", "true")
    monkeypatch.setenv("TTS_DEFAULT_VOICE_ID", _INSTANCE_VOICE_ID)
    monkeypatch.setenv("PRESETS_DEFAULT_LOCALE", "en")
    monkeypatch.setenv("CHAT_CREDIT_COST_GENERAL", "1")
    # Ключ провайдера НЕ используется для сети: `SpeechClient.synthesize` подменён ниже. Он нужен
    # только затем, чтобы `configured` был True — иначе ручка отвечает «не настроено» и кейс
    # проверял бы отказ, а не резолв голоса.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-placeholder")
    # RU-касса: без обеих переменных ручки экспериментов отвечают `503`.
    monkeypatch.setenv("CLOUDPAYMENTS_APP_ID", "app-cross")
    monkeypatch.setenv("CLOUDPAYMENTS_API_TOKEN", "token-cross")
    get_settings.cache_clear()

    anthropic_mod._anthropic_singleton = fake_anthropic  # type: ignore[assignment]
    storekit_mod._verifier_singleton = fake_storekit  # type: ignore[assignment]

    async def _fake_synthesize(_self: Any, *, text: str, voice: Any) -> bytes:
        _ = (text, voice)
        return b"fake-audio"

    monkeypatch.setattr(SpeechClient, "synthesize", _fake_synthesize)
    monkeypatch.setattr(experiments_mod.httpx, "AsyncClient", captured.client_factory)

    async def _allow(**_kwargs: Any) -> bool:
        return True

    for module, name in (
        (rate_limit, "enforce_chat_limits"),
        (rate_limit, "enforce_other_limits"),
        (rate_limit, "enforce_experiment_limits"),
        (chat_router, "enforce_chat_limits"),
        (voices_router, "enforce_other_limits"),
        (voices_router, "enforce_speech_limits"),
        (cp_router, "enforce_experiment_limits"),
    ):
        monkeypatch.setattr(module, name, _allow)

    async def _override_db() -> AsyncIterator[AsyncSession]:
        async with db_sessionmaker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app = create_app()
    app.dependency_overrides[deps.get_db] = _override_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac

    get_settings.cache_clear()


async def _turn_with_character(
    client: AsyncClient, fake: FakeAnthropicClient, uid: uuid.UUID
) -> tuple[str, str]:
    """Реальный ход с персонажем: возвращает `(sessionId, stepId)` озвучиваемого ответа."""
    fake.responses = [fake.text_result("Добро пожаловать в мой замок.")]
    response = await client.post(
        "/v1/chat/v2/run",
        json={
            "userId": str(uid),
            "message": "привет",
            "mode": "credits",
            "generationMode": "general",
            "characterId": _CHARACTER_ID,
        },
        headers=auth_headers(uid),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["stepId"] is not None, body
    return body["sessionId"], body["stepId"]


# ============================== §8.1: персонажи → голос озвучки =============================
@pytest.mark.asyncio
async def test_disabling_characters_from_the_panel_silences_the_character_voice(
    crossed: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """producer: `PATCH chat.characters_enabled` → consumer: `voiceId` в `POST /v1/chat/speech`.

    ⚠️ Подпись строки объявляет ТРИ поверхности («показывать каталог, принимать выбор
    собеседника И озвучивать ответ голосом персонажа»), и третья — САМАЯ ПОЗДНЯЯ: она пришла с
    ADR-100 уже после того, как настройка была объявлена. Кейс на каталоге персонажей её не
    покрывает: гейт можно снять с каталога и забыть на резолве голоса, и тогда чат отвечает
    обычным ассистентом, но ЗВУЧИТ Повелителем вампиров — ровно тот половинчатый выключатель,
    который ADR-097 §7 и ADR-100 §4 запрещают.

    Кейс диф-стойкий по построению: голос персонажа и голос инстанса — РАЗНЫЕ значения, и до
    правки ответ звучит первым, после — вторым. При совпадающих голосах он проходил бы при любом
    поведении гейта.
    """
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=50)
    session_id, step_id = await _turn_with_character(crossed, fake_anthropic, uid)

    with_character = await crossed.post(
        "/v1/chat/speech",
        json={"userId": str(uid), "sessionId": session_id, "stepId": step_id},
        headers=auth_headers(uid),
    )
    assert with_character.status_code == 200, with_character.text
    # предусловие: до правки озвучка ДЕЙСТВИТЕЛЬНО говорит голосом персонажа
    assert with_character.json()["voiceId"] == _CHARACTER_VOICE_ID

    assert (
        await crossed.patch(
            "/v1/admin/settings/chat.characters_enabled", json={"value": False}, headers=_H
        )
    ).status_code == 200

    after = await crossed.post(
        "/v1/chat/speech",
        json={"userId": str(uid), "sessionId": session_id, "stepId": step_id},
        headers=auth_headers(uid),
    )

    assert after.status_code == 200, after.text
    # Тот же сохранённый `character_id` в сессии — и всё равно НЕ голос персонажа: выключатель
    # выключает персонажа целиком, а не наполовину.
    assert after.json()["voiceId"] != _CHARACTER_VOICE_ID
    assert after.json()["voiceId"] == _INSTANCE_VOICE_ID


# ============================== §8.1: локаль каталогов → ручки экспериментов ================
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/v1/billing/cloudpayments/experiments/assign",
        "/v1/billing/cloudpayments/experiments/paywall-shown",
    ],
    ids=["assign", "paywall_shown"],
)
async def test_the_catalog_locale_setting_reaches_the_experiment_dimension(
    crossed: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    captured: _CapturedCall,
    path: str,
) -> None:
    """producer: `PATCH catalog.presets_default_locale` → consumer: `locale` ИСХОДЯЩЕГО вызова.

    ⚠️ Потребитель не наш ответ, а ТЕЛО запроса к провайдеру: аналитическое измерение пейволла
    (ADR-098 §3). Наружу оно не видно ни в одном ответе, поэтому кейс на каталогах его не
    покрывает — величина может доходить до трёх каталогов и не доходить сюда.

    ⚠️ Локаль экспериментов НАМЕРЕННО не зажата набором наших переводов: там она выбирает НАШ
    текст, здесь — измерение провайдера. Обе ручки проверяются отдельно: гейт можно провести на
    одной и забыть на второй.
    """
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    body = {"experimentCode": "exp", "segmentCode": "a", "placement": "onboarding"}

    before = await crossed.post(path, json=body, headers=auth_headers(uid))
    assert before.status_code == 200, before.text
    assert captured.payloads[-1]["context"]["locale"] == "en"  # предусловие: env-значение

    assert (
        await crossed.patch(
            "/v1/admin/settings/catalog.presets_default_locale",
            json={"value": "ru"},
            headers=_H,
        )
    ).status_code == 200

    after = await crossed.post(path, json=body, headers=auth_headers(uid))

    assert after.status_code == 200, after.text
    assert captured.payloads[-1]["context"]["locale"] == "ru"
