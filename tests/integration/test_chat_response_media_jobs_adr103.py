"""Integration: `ChatResponse.mediaJobs` собирается за ХОД (ADR-103).

Реальный PostgreSQL (testcontainers), Anthropic — фейк, submit в fal — фейк. Перечень кейсов
нормативен: `docs/modules/chat-orchestrator/09-testing.md`, раздел «Integration — `mediaJobs`
собирается за ХОД (ADR-103)».

Главный кейс гарантии §4 — `test_turn_scope_nonempty_continuation_accumulator_keeps_earlier_job`:
он обязан ПАДАТЬ, если производителя 2 (восстановление по ходу) снова загейтить пустым
аккумулятором. Кейс идемпотентного реплея его НЕ закрывает: там аккумулятор пуст и прежний
механизм тоже отдавал обе задачи.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.media_generation.fal_client import FalSubmission
from tests.conftest import FakeAnthropicClient, auth_headers, seed_user

_QUEUE = "https://queue.fal.run"
# nano-banana-2 @ 1K — catalog.py: default_credits=4. Цена задачи заведомо ОТЛИЧНА от цены хода
# (CHAT_CREDIT_COST_GENERAL=1), поэтому суммы медиа и хода в леджере не схлопываются и тест их
# различает: с равными ценами кейс против переоценки не отличил бы одну сумму от другой.
_IMAGE_MODEL = "nano-banana-2"


@pytest.fixture
def fal_ready(monkeypatch: pytest.MonkeyPatch) -> Any:
    """fal отвечает на submit, ключ задан. Каждый submit — новая строка `media_jobs` (новый id)."""
    monkeypatch.setenv("FAL_API_KEY", "test-fal-key")
    monkeypatch.setenv("FAL_QUEUE_BASE", _QUEUE)
    get_settings.cache_clear()

    counter = {"n": 0}

    async def _submit(self: object, *, endpoint: str, payload: dict[str, object]) -> FalSubmission:
        counter["n"] += 1
        rid = f"req_adr103_{counter['n']:02d}"
        return FalSubmission(
            request_id=rid,
            status="IN_QUEUE",
            status_url=f"{_QUEUE}/{endpoint}/requests/{rid}/status",
            response_url=f"{_QUEUE}/{endpoint}/requests/{rid}",
            queue_position=0,
        )

    async def _rehost(self: object, url: str) -> str:
        return url

    monkeypatch.setattr("app.media_generation.fal_client.FalClient.submit", _submit)
    monkeypatch.setattr("app.media_generation.fal_client.FalClient.rehost_reference_image", _rehost)
    yield
    get_settings.cache_clear()


# --- скрипты фейкового провайдера ---------------------------------------------------------------


def _gen_image(fake: FakeAnthropicClient, *, prompt: str, tool_id: str) -> Any:
    return fake.tool_result(
        "media.generate_image",
        {"model": _IMAGE_MODEL, "prompt": prompt, "resolution": "1K"},
        tool_id=tool_id,
    )


def _handoff(fake: FakeAnthropicClient, *, tool_id: str = "toolu_cs1") -> Any:
    """Client-side инструмент: ход уходит на hand-off, не закрываясь."""
    return fake.tool_result("files.read", {"path": "a.txt"}, tool_id=tool_id)


# --- вспомогательные вызовы ---------------------------------------------------------------------


async def _run(
    client: AsyncClient, uid: uuid.UUID, *, session_id: str | None = None, message: str = "go"
) -> dict[str, Any]:
    body: dict[str, Any] = {"userId": str(uid), "message": message, "mode": "credits"}
    if session_id is not None:
        body["sessionId"] = session_id
    r = await client.post("/v1/chat/run", json=body, headers=auth_headers(uid))
    assert r.status_code == 200, r.text
    return r.json()


async def _tool_result(
    client: AsyncClient, uid: uuid.UUID, *, session_id: str, tool_call_id: str
) -> dict[str, Any]:
    r = await client.post(
        "/v1/chat/tool-result",
        json={
            "userId": str(uid),
            "sessionId": session_id,
            "toolCallId": tool_call_id,
            "result": {"ok": 1},
        },
        headers=auth_headers(uid),
    )
    assert r.status_code == 200, r.text
    return r.json()


def _ids(body: dict[str, Any]) -> list[str]:
    return [j["jobId"] for j in body["mediaJobs"]]


async def _media_ledger(
    sessionmaker: async_sessionmaker[AsyncSession], uid: uuid.UUID
) -> dict[str, int]:
    """`media-gen:{jobId}` → списанная сумма. Источник истины о фактически взятых деньгах."""
    async with sessionmaker() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT idempotency_key, amount FROM ledger_transactions "
                    "WHERE user_id = :u AND type = 'debit' AND idempotency_key LIKE 'media-gen:%'"
                ),
                {"u": str(uid)},
            )
        ).all()
    return {key.removeprefix("media-gen:"): amount for key, amount in rows}


async def _balance(client: AsyncClient, uid: uuid.UUID) -> int:
    r = await client.get("/v1/wallet", headers=auth_headers(uid))
    assert r.status_code == 200, r.text
    return int(r.json()["balance"])


async def _two_job_turn(
    client: AsyncClient, uid: uuid.UUID, fake: FakeAnthropicClient
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Ход из трёх ног: A до hand-off, B на continuation'е, затем идемпотентный реплей."""
    fake.responses = [
        _gen_image(fake, prompt="a cat", tool_id="toolu_a1"),
        _handoff(fake),
        _gen_image(fake, prompt="a dog", tool_id="toolu_b1"),
        fake.text_result("обе картинки поставлены"),
    ]
    run = await _run(client, uid)
    assert run["status"] == "tool_call", run
    sid, tcid = run["sessionId"], run["toolCalls"][0]["id"]
    cont = await _tool_result(client, uid, session_id=sid, tool_call_id=tcid)
    replay = await _tool_result(client, uid, session_id=sid, tool_call_id=tcid)
    return run, cont, replay


# ==============================================================================================
# Скоуп — ХОД, а не HTTP-вызов (§1, §4)
# ==============================================================================================


@pytest.mark.asyncio
async def test_turn_scope_nonempty_continuation_accumulator_keeps_earlier_job(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    fal_ready: None,
) -> None:
    """ГЛАВНЫЙ кейс гарантии ADR-103 §4 при НЕПУСТОМ аккумуляторе continuation'а.

    Задача A поставлена на витке до hand-off, задача B — на `/chat/tool-result` того же хода.
    Ответ continuation'а обязан нести ОБЕ записи в порядке `A, B`. На прежнем механизме
    («восстанавливать только при пустом аккумуляторе») эта нога отдаёт одну `B` и тест падает —
    в этом его смысл; кейс реплея его не закрывает, там аккумулятор пуст.
    """
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=50)

    run, cont, _ = await _two_job_turn(client, uid, fake_anthropic)
    job_a = run["mediaJobs"][0]["jobId"]
    assert len(run["mediaJobs"]) == 1, run["mediaJobs"]

    assert cont["status"] == "assistant_message", cont
    assert cont["messageStepId"] == run["messageStepId"], "тот же ХОД"
    assert len(cont["mediaJobs"]) == 2, cont["mediaJobs"]
    assert _ids(cont)[0] == job_a, "задача раннего витка обязана остаться и стоять ПЕРВОЙ"
    assert _ids(cont)[1] != job_a


@pytest.mark.asyncio
async def test_final_leg_and_replay_agree_when_continuation_added_a_job(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    fal_ready: None,
) -> None:
    """Один ход не может отдавать два разных ответа на разных ногах (ADR-103 §4, следствие).

    Сравниваются финальная нога и её идемпотентный реплей — именно та пара, что расходилась на
    прежнем механизме: аккумулятор непуст на первой и пуст на второй.
    """
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=50)

    _, cont, replay = await _two_job_turn(client, uid, fake_anthropic)

    assert _ids(cont) == _ids(replay), "расхождение ног одного хода"
    assert len(_ids(cont)) == 2


@pytest.mark.asyncio
async def test_all_three_legs_agree_when_every_job_submitted_before_handoff(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    fal_ready: None,
) -> None:
    """Множества `jobId` hand-off-ноги, финальной ноги и реплея сравниваются МЕЖДУ СОБОЙ.

    Обе задачи поставлены до hand-off, поэтому все три ноги обязаны нести ОДНО И ТО ЖЕ множество:
    расхождение любой пары роняет тест.
    """
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=50)

    fake_anthropic.responses = [
        _gen_image(fake_anthropic, prompt="a cat", tool_id="toolu_a1"),
        _gen_image(fake_anthropic, prompt="a dog", tool_id="toolu_b1"),
        _handoff(fake_anthropic),
        fake_anthropic.text_result("готово"),
    ]
    run = await _run(client, uid)
    assert run["status"] == "tool_call", run
    sid, tcid = run["sessionId"], run["toolCalls"][0]["id"]
    cont = await _tool_result(client, uid, session_id=sid, tool_call_id=tcid)
    replay = await _tool_result(client, uid, session_id=sid, tool_call_id=tcid)

    assert len(set(_ids(run))) == 2, run["mediaJobs"]
    assert set(_ids(run)) == set(_ids(cont)) == set(_ids(replay))
    assert cont["messageStepId"] == run["messageStepId"] == replay["messageStepId"]


@pytest.mark.asyncio
async def test_no_duplicate_job_id_on_any_leg_when_sources_overlap(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    fal_ready: None,
) -> None:
    """Свёртка по `jobId` (ADR-103 §2): задача из ОБОИХ источников даёт ОДНУ запись.

    На реплее каждая задача приходит из нескольких шагов хода сразу — tool-результат
    `media.generate_*` и `payload.mediaJobs` закрывающего assistant-шага, — то есть источники
    заведомо пересекаются. Без свёртки список удвоился бы, и клиент показал бы вторую карточку
    одной задачи.
    """
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=50)

    run, cont, replay = await _two_job_turn(client, uid, fake_anthropic)

    for leg_name, leg in (("run", run), ("cont", cont), ("replay", replay)):
        ids = _ids(leg)
        assert len(ids) == len(set(ids)), f"дубль jobId на ноге {leg_name}: {ids}"
    assert len(_ids(replay)) == 2, replay["mediaJobs"]


@pytest.mark.asyncio
async def test_max_tokens_blocked_leg_still_carries_turn_media_jobs(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    fal_ready: None,
) -> None:
    """Обрыв по потолку токенов — не policy-block: у хода есть id, задача доезжает (§6)."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=50)

    fake_anthropic.responses = [
        _gen_image(fake_anthropic, prompt="a cat", tool_id="toolu_a1"),
        fake_anthropic.max_tokens_result(text="частичный ответ...", output_tokens=16000),
    ]
    body = await _run(client, uid)

    assert body["status"] == "blocked"
    assert body["blockReason"] == "max_tokens"
    assert body["messageStepId"] is not None
    assert len(body["mediaJobs"]) == 1, body


# ==============================================================================================
# `creditsCharged` — величина ВЫЗОВА (§3), оба края предиката
# ==============================================================================================


@pytest.mark.asyncio
async def test_submitting_leg_reports_actual_charge_recovered_leg_reports_zero(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    fal_ready: None,
) -> None:
    """Против НЕДООЦЕНКИ: нога, на которой сабмит произошёл, сообщает фактически списанное.

    Против ПЕРЕОЦЕНКИ в той же паре: на следующей ноге та же задача только восстановлена и несёт
    `0`, тогда как поставленная ЗДЕСЬ B несёт свою фактическую сумму.
    """
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=50)

    run, cont, _ = await _two_job_turn(client, uid, fake_anthropic)
    charged = await _media_ledger(db_sessionmaker, uid)

    job_a = run["mediaJobs"][0]
    assert job_a["creditsCharged"] == charged[job_a["jobId"]] > 0, "нога сабмита A: фактическое"

    by_id = {j["jobId"]: j for j in cont["mediaJobs"]}
    assert by_id[job_a["jobId"]]["creditsCharged"] == 0, "A здесь только восстановлена → 0"
    job_b_id = next(i for i in by_id if i != job_a["jobId"])
    assert by_id[job_b_id]["creditsCharged"] == charged[job_b_id] > 0, "B поставлена ЗДЕСЬ"


@pytest.mark.asyncio
async def test_idempotent_replay_reports_zero_for_every_job(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    fal_ready: None,
) -> None:
    """Реплей не списал ничего — значит ни одна его запись не вправе объявлять списание (§3)."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=50)

    _, _, replay = await _two_job_turn(client, uid, fake_anthropic)

    assert len(replay["mediaJobs"]) == 2, replay["mediaJobs"]
    assert [j["creditsCharged"] for j in replay["mediaJobs"]] == [0, 0], replay["mediaJobs"]


@pytest.mark.asyncio
async def test_sum_of_credits_charged_over_legs_equals_actual_media_debit(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    fal_ready: None,
) -> None:
    """Против ПЕРЕОЦЕНКИ, отдельным тестом: клиент обновляет баланс СУММОЙ поля по ногам.

    Сумма `creditsCharged` по всем ногам одного хода обязана равняться фактически списанному за
    медиа — и по строкам леджера, и по изменению баланса `GET /v1/wallet`. Реализация, где
    восстановленная запись несёт сохранённую сумму, объявила бы задачу A трижды.
    """
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=50)
    before = await _balance(client, uid)

    run, cont, replay = await _two_job_turn(client, uid, fake_anthropic)
    after = await _balance(client, uid)
    charged = await _media_ledger(db_sessionmaker, uid)

    assert len(charged) == 2, "две задачи — ровно две строки `media-gen:` в леджере"
    declared = sum(
        j["creditsCharged"] for leg in (run, cont, replay) for j in leg["mediaJobs"] or []
    )
    assert declared == sum(charged.values()), "объявлено по ногам ≠ списано за медиа"

    chat_debit = get_settings().chat_credit_cost_general
    assert before - after == sum(charged.values()) + chat_debit, "ход списан ровно один раз"


@pytest.mark.asyncio
async def test_history_anchor_keeps_job_price_while_replay_response_zeroes_it(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    fal_ready: None,
) -> None:
    """Обе стороны контраста ADR-103 §3 одним кейсом: якорь истории НЕ обнуляется.

    Правило `creditsCharged = 0` живёт только в проекции ответа хода. В `steps[].payload.mediaJobs`
    то же поле означает СТОИМОСТЬ задачи, и холодный старт обязан видеть цену запуска. Тест падает
    и если правило проекции перенесли в историю, и если правило истории оставили в проекции.
    """
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=50)

    _, cont, replay = await _two_job_turn(client, uid, fake_anthropic)
    charged = await _media_ledger(db_sessionmaker, uid)

    # Сторона проекции: реплей ничего не списал → все записи ответа несут 0.
    assert [j["creditsCharged"] for j in replay["mediaJobs"]] == [0, 0], replay["mediaJobs"]

    # Сторона истории: тот же ход, тот же jobId — цена задачи сохранена.
    hist = await client.get(f"/v1/chats/{cont['sessionId']}", headers=auth_headers(uid))
    assert hist.status_code == 200, hist.text
    anchored = [
        job
        for step in hist.json()["steps"]
        if step["role"] == "assistant"
        for job in ((step.get("payload") or {}).get("mediaJobs") or [])
    ]
    assert anchored, "закрывающий assistant-шаг обязан нести якорь `payload.mediaJobs`"
    for job in anchored:
        assert job["creditsCharged"] == charged[job["jobId"]] > 0, job


# ==============================================================================================
# Визардный путь постановки (§1: перечень источников не уже якоря истории)
# ==============================================================================================


@pytest.mark.asyncio
async def test_wizard_submitted_job_is_recoverable_from_turn_steps(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    fal_ready: None,
) -> None:
    """Восстановление находит ВИЗАРДНУЮ задачу, у которой tool-шага `media.generate_*` нет вовсе.

    Финальный сабмит визарда (ADR-070 §3) идёт ДО модели, поэтому шага `media.generate_*` его ход
    не содержит: тест это ПРОВЕРЯЕТ, а не предполагает. Восстановление, построенное только на
    tool-результатах, вернуло бы для такого хода пустой список — и гарантия §4 отказывала бы на
    целом пути постановки.
    """
    from app.chat.orchestrator import _MEDIA_TOOL_NAMES
    from app.chat.repository import ChatRepository

    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=80)

    fake_anthropic.responses = [
        fake_anthropic.tool_result(
            "media.ask_params",
            {"kind": "image", "prompt": "a fluffy cat"},
            tool_id="toolu_ask01",
        ),
        fake_anthropic.text_result("Выберите модель."),
    ]
    r1 = await client.post(
        "/v1/chat/v2/run",
        json={"userId": str(uid), "message": "нарисуй кота", "mode": "credits"},
        headers=auth_headers(uid),
    )
    assert r1.status_code == 200, r1.text
    body = r1.json()
    session_id = body["sessionId"]
    selection_id = body["mediaChoices"]["selectionId"]

    answers: dict[str, str] = {}
    for _ in range(8):
        r = await client.post(
            "/v1/chat/v2/run",
            json={
                "userId": str(uid),
                "sessionId": session_id,
                "message": "",
                "mode": "credits",
                "mediaSelection": {"selectionId": selection_id, "answers": answers},
            },
            headers=auth_headers(uid),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        if body.get("mediaJobs"):
            break
        choices = body["mediaChoices"]
        answers[choices["step"]] = choices["questions"][0]["options"][0]["value"]
    else:
        raise AssertionError("визард не дошёл до сабмита")

    job = body["mediaJobs"][0]
    turn_id = uuid.UUID(body["messageStepId"])
    session_uuid = uuid.UUID(session_id)

    async with db_sessionmaker() as s:
        repo = ChatRepository(s)
        # Предпосылка кейса, а не допущение: tool-шага `media.generate_*` у этого хода НЕТ.
        assert (
            await repo.tool_results_for_message_step(session_uuid, turn_id, _MEDIA_TOOL_NAMES) == []
        ), "визардный сабмит не оставляет tool-шага — иначе кейс проверял бы не тот путь"
        recovered = await repo.media_job_refs_for_message_step(session_uuid, turn_id)

    assert job["jobId"] in {ref["jobId"] for ref in recovered}, recovered
    by_id = {ref["jobId"]: ref for ref in recovered}[job["jobId"]]
    assert by_id["kind"] == "image"
    assert by_id["status"] == "queued", "снимок на момент постановки, восстановление не освежает"


@pytest.mark.asyncio
async def test_wizard_tap_leg_of_the_same_turn_keeps_the_turns_job(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    fal_ready: None,
) -> None:
    """Промежуточный тап визарда — ТЕРМИНАЛЬНАЯ нога ТОГО ЖЕ хода, и §4 действует и на ней.

    Ход ставит задачу A и тут же открывает визард. Тап по карточке отвечает `assistant_message`
    и ТЕМ ЖЕ `messageStepId` (визард патчит tool-шаг `media.ask_params`, а не открывает новый ход),
    то есть клиент видит вторую ногу одного хода. Гарантия §4 («каждая задача хода — на КАЖДОЙ
    терминальной ноге») и её следствие («все ноги одного хода отдают один и тот же список») на этой
    ноге обязаны выполняться так же, как на continuation'е.
    """
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=80)

    fake_anthropic.responses = [
        _gen_image(fake_anthropic, prompt="a cat", tool_id="toolu_a1"),
        fake_anthropic.tool_result(
            "media.ask_params", {"kind": "image", "prompt": "a dog"}, tool_id="toolu_ask01"
        ),
        fake_anthropic.text_result("выберите модель"),
    ]
    r1 = await client.post(
        "/v1/chat/v2/run",
        json={"userId": str(uid), "message": "нарисуй кота и спроси про собаку", "mode": "credits"},
        headers=auth_headers(uid),
    )
    assert r1.status_code == 200, r1.text
    first = r1.json()
    assert first["status"] == "assistant_message", first
    assert len(first["mediaJobs"]) == 1, first
    job_a = first["mediaJobs"][0]["jobId"]
    choices = first["mediaChoices"]

    r2 = await client.post(
        "/v1/chat/v2/run",
        json={
            "userId": str(uid),
            "sessionId": first["sessionId"],
            "message": "",
            "mode": "credits",
            "mediaSelection": {
                "selectionId": choices["selectionId"],
                "answers": {choices["step"]: choices["questions"][0]["options"][0]["value"]},
            },
        },
        headers=auth_headers(uid),
    )
    assert r2.status_code == 200, r2.text
    tap = r2.json()

    assert tap["status"] == "assistant_message", "нога терминальная"
    assert tap["messageStepId"] == first["messageStepId"], "предпосылка кейса: ТОТ ЖЕ ход"
    assert tap["mediaJobs"] is not None, "задача хода выпала из ноги — §4 нарушена"
    assert _ids(tap) == [job_a], tap["mediaJobs"]


# ==============================================================================================
# Против переоценки: в список не попадает ничего, кроме успешных постановок (§4)
# ==============================================================================================


@pytest.mark.asyncio
async def test_media_not_configured_soft_error_keeps_media_jobs_null_on_all_legs(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Soft-отказ задачи не создаёт — ни на одной ноге хода поле не наполняется."""
    monkeypatch.setenv("FAL_API_KEY", "")
    get_settings.cache_clear()
    try:
        async with db_sessionmaker() as s:
            uid = await seed_user(s, subscription="active", balance=50)

        fake_anthropic.responses = [
            _gen_image(fake_anthropic, prompt="a cat", tool_id="toolu_a1"),
            _handoff(fake_anthropic),
            fake_anthropic.text_result("генерация недоступна"),
        ]
        run = await _run(client, uid)
        assert run["status"] == "tool_call", run
        sid, tcid = run["sessionId"], run["toolCalls"][0]["id"]
        cont = await _tool_result(client, uid, session_id=sid, tool_call_id=tcid)
        replay = await _tool_result(client, uid, session_id=sid, tool_call_id=tcid)

        for leg_name, leg in (("run", run), ("cont", cont), ("replay", replay)):
            assert leg["mediaJobs"] is None, f"нога {leg_name}: {leg['mediaJobs']}"
        assert any(
            st["toolName"] == "media.generate_image" and st["status"] == "errored"
            for st in run["serverTools"]
        ), "факт отказа виден в serverTools, а не в mediaJobs"
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_insufficient_credits_soft_error_keeps_media_jobs_null_on_all_legs(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    fal_ready: None,
) -> None:
    """Баланса хватает на ход, но не на задачу: отказ — не постановка, поле остаётся `null`."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=2)

    fake_anthropic.responses = [
        _gen_image(fake_anthropic, prompt="a cat", tool_id="toolu_a1"),
        _handoff(fake_anthropic),
        fake_anthropic.text_result("не хватило кредитов"),
    ]
    run = await _run(client, uid)
    assert run["status"] == "tool_call", run
    sid, tcid = run["sessionId"], run["toolCalls"][0]["id"]
    cont = await _tool_result(client, uid, session_id=sid, tool_call_id=tcid)
    replay = await _tool_result(client, uid, session_id=sid, tool_call_id=tcid)

    for leg_name, leg in (("run", run), ("cont", cont), ("replay", replay)):
        assert leg["mediaJobs"] is None, f"нога {leg_name}: {leg['mediaJobs']}"
    assert any(st.get("summary") == "insufficient_credits" for st in run["serverTools"]), run[
        "serverTools"
    ]
    # Предикат наполнения — только УСПЕШНЫЕ постановки: отказ задачи не создал, восстанавливать
    # нечего ни на одной ноге. Проверяется по строкам `media_jobs`, а не по ответу: пустой ответ
    # сам по себе не отличает «задачи нет» от «задача есть, но потерялась».
    async with db_sessionmaker() as s:
        created = await s.scalar(
            text("SELECT count(*) FROM media_jobs WHERE user_id = :u"), {"u": str(uid)}
        )
    assert created == 0, "soft-отказ задачи не создаёт"


@pytest.mark.asyncio
async def test_policy_blocked_turn_has_media_jobs_null_not_empty_list(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """Хода нет (`messageStepId=null`) — восстанавливать не из чего: `null`, а не `[]` (§6)."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=0)

    body = await _run(client, uid)

    assert body["status"] == "blocked"
    assert body["messageStepId"] is None
    assert "mediaJobs" in body, "поле присутствует в сериализации всегда"
    assert body["mediaJobs"] is None
    assert body["mediaJobs"] != [], "пустой список запрещён (контраст с serverTools)"
    assert fake_anthropic.calls == []
