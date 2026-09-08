"""Unit: озвучка ответа — чистка текста, выбор голоса и деньги (ADR-100)."""

from __future__ import annotations

import contextlib
import datetime
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from app.chat.speech import (
    SpeechSynthesisService,
    apply_speech_cap,
    assistant_text_of_step,
    to_spoken_text,
)
from app.chat.voices import resolve_voice
from app.config import get_settings
from app.errors import (
    InsufficientCreditsError,
    UpstreamError,
    ValidationFailedError,
    VoiceOutputDisabledError,
)
from app.instance_config.snapshot import (
    InstanceConfigSnapshot,
    SettingOverlay,
    install_snapshot,
    reset_snapshot,
)

# ---- чистка текста -------------------------------------------------------------------------


def test_markup_is_removed_from_spoken_text() -> None:
    out = to_spoken_text("# Заголовок\n\n- пункт **важный**\n\n[ссылка](https://x.dev) и `код`")
    for junk in ("#", "**", "](", "`", "https://"):
        assert junk not in out
    assert "важный" in out and "ссылка" in out


def test_code_block_does_not_reach_the_speaker() -> None:
    out = to_spoken_text("Вот решение:\n```python\nprint(1)\n```\nГотово.")
    assert "print" not in out and "```" not in out
    assert "Готово" in out


def test_unclosed_code_fence_is_eaten_to_the_end() -> None:
    # Незакрытый блок — обычный обрыв ответа. Без явного правила остаток кода ушёл бы в звук.
    out = to_spoken_text("Смотри:\n```\nimport os\nos.remove(path)\n")
    assert "import" not in out and "os.remove" not in out


def test_multiplication_is_not_mistaken_for_emphasis() -> None:
    # Наивная регулярка выделения склеивает числа: «5*7» превращалось бы в «57». Это чистка,
    # МЕНЯЮЩАЯ СМЫСЛ, — худший вид порчи, потому что звучит правдоподобно.
    out = to_spoken_text("2 * 3 = 6 и 5*7")
    assert "57" not in out


def test_cleaning_is_idempotent() -> None:
    src = "## Итог\n\n1. **раз**\n2. `два`\n\nhttps://example.com"
    once = to_spoken_text(src)
    assert to_spoken_text(once) == once


def test_cap_cuts_on_a_sentence_boundary() -> None:
    text = "Первое предложение. Второе предложение. Третье предложение."
    out, truncated = apply_speech_cap(text, 30)
    assert truncated is True
    assert "Третье" not in out


def test_cap_reports_when_nothing_was_cut() -> None:
    out, truncated = apply_speech_cap("Коротко.", 700)
    assert (out, truncated) == ("Коротко.", False)


def test_cleaning_runs_before_the_cap() -> None:
    # Порядок обязателен: сначала чистка, потом потолок. При обратном порядке лимит съела бы
    # разметка и до слов дело не дошло бы — прозвучала бы тишина на осмысленном ответе.
    text = "```\n" + ("x = 1\n" * 200) + "```\nА теперь по существу: ответ такой."
    spoken, _ = apply_speech_cap(to_spoken_text(text), 700)
    assert "по существу" in spoken


def test_assistant_text_is_read_from_the_stored_step() -> None:
    doc = assistant_text_of_step({"content": [{"type": "text", "text": "Привет!"}]})
    openai_shape = assistant_text_of_step(
        {"messages": [{"role": "assistant", "content": "Привет!"}]}
    )
    # Инстансы на разных провайдерах хранят шаг по-разному. Понимать одну форму значит молча
    # отвечать «нечего озвучивать» на целом классе инстансов.
    assert "Привет" in doc or "Привет" in openai_shape


# ---- выбор голоса --------------------------------------------------------------------------


@contextlib.contextmanager
def _characters_setting(monkeypatch: pytest.MonkeyPatch, *, operator: bool) -> Iterator[None]:
    """Выключатель персонажей задан ОПЕРАТОРОМ из панели, а env — противоположный (ADR-099 §8).

    Значения расходятся намеренно. Настройка объявлена управляемой из CRM, поэтому её
    потребитель обязан читать оверлей, а не сырой ``Settings``; при совпадающих значениях тест
    прошёл бы при ЛЮБОМ из двух способов чтения и стерёг бы пустоту. Расхождение делает кейс
    диф-стойким: возврат потребителя на ``get_settings()`` роняет его немедленно.
    """
    monkeypatch.setenv("CHARACTERS_ENABLED", "false" if operator else "true")
    get_settings.cache_clear()
    install_snapshot(
        InstanceConfigSnapshot(
            settings={
                "chat.characters_enabled": SettingOverlay(
                    setting_id="chat.characters_enabled",
                    value=operator,
                    updated_at=datetime.datetime.now(tz=datetime.UTC),
                )
            }
        )
    )
    try:
        yield
    finally:
        reset_snapshot()
        get_settings.cache_clear()


def test_character_voice_wins_over_user_default(monkeypatch: pytest.MonkeyPatch) -> None:
    with _characters_setting(monkeypatch, operator=True):
        voice = resolve_voice(character_id="vampire_lord", user_default_voice_id="default_female")
        assert voice.id != "default_female"


def test_with_characters_off_the_session_character_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _characters_setting(monkeypatch, operator=False):
        # Иначе чат ОТВЕЧАЛ бы обычным ассистентом, но ЗВУЧАЛ бы персонажем.
        voice = resolve_voice(character_id="vampire_lord", user_default_voice_id="default_male")
        assert voice.id == "default_male"


def test_retired_stored_default_degrades_instead_of_failing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CHARACTERS_ENABLED", "true")
    get_settings.cache_clear()
    voice = resolve_voice(character_id=None, user_default_voice_id="voice_that_was_retired")
    assert voice is not None and voice.id
    get_settings.cache_clear()


# ---- деньги --------------------------------------------------------------------------------


@dataclass
class _Step:
    id: uuid.UUID
    payload: dict[str, Any]
    message_step_id: uuid.UUID


class _Repo:
    def __init__(
        self,
        character_id: str | None = None,
        text: str = "Всё готово.",
        quiz_result: dict[str, Any] | None = None,
    ) -> None:
        self.session = SimpleNamespace(id=uuid.uuid4(), character_id=character_id)
        self.step = _Step(
            id=uuid.uuid4(),
            payload={"content": [{"type": "text", "text": text}]},
            message_step_id=uuid.uuid4(),
        )
        self._quiz = quiz_result

    async def get_session(self, session_id: uuid.UUID, user_id: uuid.UUID) -> Any:
        return self.session

    async def get_assistant_step(self, session_id: uuid.UUID, step_id: uuid.UUID) -> Any:
        return self.step

    async def last_tool_result_for_message_step(
        self, session_id: uuid.UUID, message_step_id: uuid.UUID, tool_name: str
    ) -> Any:
        return self._quiz


class _Prefs:
    def __init__(self, voice_id: str | None = None) -> None:
        self._voice = voice_id

    async def get_default_voice_id(self, user_id: uuid.UUID) -> str | None:
        return self._voice


class _Wallet:
    """Журнал, помнящий ключи идемпотентности — как настоящий."""

    def __init__(self, balance: int = 100) -> None:
        self.balance = balance
        self.keys: list[str] = []

    async def has_idempotency_key(self, user_id: uuid.UUID, key: str) -> bool:
        return key in self.keys

    async def current_balance(self, user_id: uuid.UUID) -> int:
        return self.balance

    async def consume(self, **kwargs: Any) -> Any:
        key = kwargs["idempotency_key"]
        replay = key in self.keys
        if not replay:
            self.keys.append(key)
            self.balance -= kwargs["amount"]
        return SimpleNamespace(new_balance=self.balance, idempotent_replay=replay)


class _Client:
    configured = True

    def __init__(self, raise_exc: Exception | None = None) -> None:
        self.calls = 0
        self._raise = raise_exc

    async def synthesize(self, *, text: str, voice: Any) -> bytes:
        self.calls += 1
        if self._raise is not None:
            raise self._raise
        return b"\xff\xf3audio"


def _service(
    repo: Any, prefs: Any, wallet: Any, client: Any, monkeypatch: pytest.MonkeyPatch
) -> SpeechSynthesisService:
    monkeypatch.setenv("VOICE_OUTPUT_ENABLED", "true")
    monkeypatch.setenv("CHARACTERS_ENABLED", "true")
    get_settings.cache_clear()
    return SpeechSynthesisService(
        repo=repo, preferences=prefs, wallet=wallet, client=client, settings=get_settings()
    )


@pytest.mark.asyncio
async def test_repeat_of_the_same_pair_is_free(monkeypatch: pytest.MonkeyPatch) -> None:
    repo, wallet, client = _Repo(), _Wallet(), _Client()
    svc = _service(repo, _Prefs(), wallet, client, monkeypatch)
    uid = uuid.uuid4()
    first = await svc.synthesize(user_id=uid, session_id=repo.session.id, step_id=repo.step.id)
    second = await svc.synthesize(user_id=uid, session_id=repo.session.id, step_id=repo.step.id)
    assert first.credits_charged > 0
    assert second.credits_charged == 0
    # Списание одно, а синтез — оба раза: звук не хранится, но дважды за него не платят.
    assert len(wallet.keys) == 1
    assert client.calls == 2
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_a_different_voice_is_new_work_and_is_charged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, wallet, client = _Repo(), _Wallet(), _Client()
    uid = uuid.uuid4()
    svc = _service(repo, _Prefs("default_male"), wallet, client, monkeypatch)
    await svc.synthesize(user_id=uid, session_id=repo.session.id, step_id=repo.step.id)
    svc2 = _service(repo, _Prefs("default_female"), wallet, client, monkeypatch)
    second = await svc2.synthesize(user_id=uid, session_id=repo.session.id, step_id=repo.step.id)
    # Парный к предыдущему: без него тест проходит и на реализации, которая схлопывает всё в
    # один ключ и не берёт денег за законную переозвучку другим голосом.
    assert second.credits_charged > 0
    assert len(set(wallet.keys)) == 2
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_paid_repeat_works_at_zero_balance(monkeypatch: pytest.MonkeyPatch) -> None:
    repo, wallet, client = _Repo(), _Wallet(), _Client()
    uid = uuid.uuid4()
    svc = _service(repo, _Prefs(), wallet, client, monkeypatch)
    await svc.synthesize(user_id=uid, session_id=repo.session.id, step_id=repo.step.id)
    wallet.balance = 0
    # Уже оплаченный звук обязан отдаваться и на нуле: иначе человек платит второй раз за то,
    # что у него уже куплено, — или не получает вовсе.
    again = await svc.synthesize(user_id=uid, session_id=repo.session.id, step_id=repo.step.id)
    assert again.credits_charged == 0
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_provider_failure_leaves_no_ledger_row(monkeypatch: pytest.MonkeyPatch) -> None:
    repo, wallet = _Repo(), _Wallet()
    client = _Client(raise_exc=UpstreamError("провайдер недоступен"))
    svc = _service(repo, _Prefs(), wallet, client, monkeypatch)
    with pytest.raises(UpstreamError):
        await svc.synthesize(user_id=uuid.uuid4(), session_id=repo.session.id, step_id=repo.step.id)
    # Списание идёт ПОСЛЕ успеха, поэтому ветки возврата не существует и исход «списано, но не
    # доставлено» невозможен по построению.
    assert wallet.keys == []
    assert wallet.balance == 100
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_insufficient_balance_does_not_call_the_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, client = _Repo(), _Client()
    wallet = _Wallet(balance=0)
    svc = _service(repo, _Prefs(), wallet, client, monkeypatch)
    with pytest.raises(InsufficientCreditsError):
        await svc.synthesize(user_id=uuid.uuid4(), session_id=repo.session.id, step_id=repo.step.id)
    # Платить поставщику за работу, которую мы не сможем провести по счёту, нельзя.
    assert client.calls == 0
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_nothing_to_speak_costs_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _Repo(text="```\nprint(1)\n```")
    wallet, client = _Wallet(), _Client()
    svc = _service(repo, _Prefs(), wallet, client, monkeypatch)
    with pytest.raises(ValidationFailedError):
        await svc.synthesize(user_id=uuid.uuid4(), session_id=repo.session.id, step_id=repo.step.id)
    assert client.calls == 0
    assert wallet.keys == []
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_disabled_instance_refuses_before_any_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, wallet, client = _Repo(), _Wallet(), _Client()
    monkeypatch.setenv("VOICE_OUTPUT_ENABLED", "false")
    get_settings.cache_clear()
    svc = SpeechSynthesisService(
        repo=repo, preferences=_Prefs(), wallet=wallet, client=client, settings=get_settings()
    )
    with pytest.raises(VoiceOutputDisabledError):
        await svc.synthesize(user_id=uuid.uuid4(), session_id=repo.session.id, step_id=repo.step.id)
    assert client.calls == 0
    assert wallet.keys == []
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_quiz_answers_are_never_spoken_aloud(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ход с квизом не озвучивается, даже когда текст в нём есть.

    История уже срезает квиз, чтобы не показать правильные ответы раньше времени. Звук — вторая
    поверхность того же спойлера: без явной проверки сервис прочитал бы вопросы и ответы вслух.
    """
    repo = _Repo(text="Вопрос 1. Столица Франции?", quiz_result={"questions": [{"a": "Париж"}]})
    wallet, client = _Wallet(), _Client()
    svc = _service(repo, _Prefs(), wallet, client, monkeypatch)
    with pytest.raises(ValidationFailedError):
        await svc.synthesize(user_id=uuid.uuid4(), session_id=repo.session.id, step_id=repo.step.id)
    assert client.calls == 0
    assert wallet.keys == []
    get_settings.cache_clear()
