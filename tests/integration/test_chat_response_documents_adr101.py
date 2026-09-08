"""Integration: `ChatResponse.documents[]` — документы, созданные/изменённые ходом (ADR-101).

Реальный PostgreSQL (testcontainers), Anthropic — фейк. Перечень кейсов нормативен:
`docs/modules/documents/09-testing.md`, раздел «Integration — `documents[]` в ответе хода».

Главный кейс гарантии — `test_turn_scope_nonempty_continuation_accumulator_keeps_earlier_doc`:
он обязан ПАДАТЬ, если производителя 2 (восстановление по ходу) снова загейтить пустым
аккумулятором. Остальные кейсы поля на этом гейте зелены, поэтому «поле работает» их перечень
не закрывает.
"""

from __future__ import annotations

import base64
import json
import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import FakeAnthropicClient, auth_headers, seed_user

_MEDIA_TYPES = {"text/markdown", "text/plain", "text/csv", "application/json"}
_CARD_KEYS = {"documentId", "filename", "mediaType", "size", "version"}


# --- скрипты фейкового провайдера -------------------------------------------------------------


def _create(
    fake: FakeAnthropicClient,
    *,
    filename: str = "report",
    media_type: str = "text/markdown",
    content: str = "# Report",
    tool_id: str,
) -> Any:
    return fake.tool_result(
        "document.create",
        {"filename": filename, "mediaType": media_type, "content": content},
        tool_id=tool_id,
    )


def _update(fake: FakeAnthropicClient, *, document_id: str, content: str, tool_id: str) -> Any:
    return fake.tool_result(
        "document.update", {"documentId": document_id, "content": content}, tool_id=tool_id
    )


# --- вспомогательные вызовы --------------------------------------------------------------------


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


async def _rest_create(
    client: AsyncClient,
    uid: uuid.UUID,
    session_id: str,
    *,
    filename: str = "seed",
    media_type: str = "text/plain",
    content: str = "seed",
) -> dict[str, Any]:
    """Документ, существующий ДО хода: нужен там, где ход только читает или только правит."""
    r = await client.post(
        f"/v1/chats/{session_id}/documents",
        json={
            "filename": filename,
            "mediaType": media_type,
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        },
        headers=auth_headers(uid),
    )
    assert r.status_code == 201, r.text
    return r.json()


def _parse_sse(raw: str) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []
    for block in raw.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        event_name = "message"
        data_lines: list[str] = []
        for line in block.split("\n"):
            if line.startswith("event:"):
                event_name = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:") :].strip())
        if data_lines:
            events.append((event_name, json.loads("\n".join(data_lines))))
    return events


# ==============================================================================================
# Состав элемента и сквозная адресуемость
# ==============================================================================================


@pytest.mark.asyncio
async def test_single_create_surfaces_card_without_content(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """ADR-101 §1/§2: карточка = _doc_brief, содержимого в элементе НЕТ."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=20)

    fake_anthropic.responses = [
        _create(fake_anthropic, filename="отчёт", content="# Отчёт", tool_id="toolu_c1"),
        fake_anthropic.text_result("Готово."),
    ]
    body = await _run(client, uid)

    docs = body["documents"]
    assert isinstance(docs, list) and len(docs) == 1, body
    card = docs[0]
    assert set(card) == _CARD_KEYS, "состав ДОСЛОВНО как у _doc_brief, ни больше ни меньше"
    assert "content" not in card, "содержимое ходом не отдаётся (ADR-101 §2)"
    assert card["filename"] == "отчёт.md"
    assert card["mediaType"] == "text/markdown"
    assert card["version"] == 1
    assert card["size"] == len("# Отчёт".encode())
    uuid.UUID(card["documentId"])


@pytest.mark.asyncio
async def test_document_id_from_field_resolves_over_rest(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """Сквозная цепочка: id из ответа хода открывается штатной REST-ручкой, а не только «похож»."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=20)

    fake_anthropic.responses = [
        _create(fake_anthropic, content="chain", tool_id="toolu_c1"),
        fake_anthropic.text_result("ok"),
    ]
    body = await _run(client, uid)
    doc_id = body["documents"][0]["documentId"]

    got = await client.get(
        f"/v1/chats/{body['sessionId']}/documents/{doc_id}", headers=auth_headers(uid)
    )
    assert got.status_code == 200, got.text
    assert base64.b64decode(got.json()["content"]).decode("utf-8") == "chain"


@pytest.mark.asyncio
async def test_filename_is_the_normalized_one_not_the_requested_one(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """`список.txt` при text/markdown → `список.md`: клиент показывает ФАКТИЧЕСКОЕ имя."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=20)

    fake_anthropic.responses = [
        _create(
            fake_anthropic,
            filename="список.txt",
            media_type="text/markdown",
            content="- a",
            tool_id="toolu_c1",
        ),
        fake_anthropic.text_result("ok"),
    ]
    body = await _run(client, uid)
    assert body["documents"][0]["filename"] == "список.md", body["documents"]


# ==============================================================================================
# Предикат отнесения (§3): что в список НЕ попадает
# ==============================================================================================


@pytest.mark.asyncio
async def test_read_only_turn_has_no_documents(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """`document.read` ничего не изменил — «документ готов» показывать не на чем."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=20)

    fake_anthropic.responses = [fake_anthropic.text_result("hi")]
    first = await _run(client, uid, message="hi")
    sid = first["sessionId"]
    seeded = await _rest_create(client, uid, sid)

    fake_anthropic.responses = [
        fake_anthropic.tool_result(
            "document.read", {"documentId": seeded["documentId"]}, tool_id="toolu_r1"
        ),
        fake_anthropic.text_result("прочитал"),
    ]
    body = await _run(client, uid, session_id=sid, message="прочитай")
    assert body["documents"] is None, body
    assert any(st["toolName"] == "document.read" for st in body["serverTools"])


@pytest.mark.asyncio
async def test_list_only_turn_has_no_documents(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=20)

    fake_anthropic.responses = [fake_anthropic.text_result("hi")]
    first = await _run(client, uid, message="hi")
    sid = first["sessionId"]
    await _rest_create(client, uid, sid)

    fake_anthropic.responses = [
        fake_anthropic.tool_result("document.list", {}, tool_id="toolu_l1"),
        fake_anthropic.text_result("вот список"),
    ]
    body = await _run(client, uid, session_id=sid, message="покажи файлы")
    assert body["documents"] is None, body
    assert any(st["toolName"] == "document.list" for st in body["serverTools"])


@pytest.mark.asyncio
async def test_errored_create_keeps_documents_null_and_shows_errored_server_tool(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """Отказ — это `status=errored`: документа не появилось, но отказ виден в serverTools."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=20)

    fake_anthropic.responses = [
        _create(fake_anthropic, content="", tool_id="toolu_e1"),
        fake_anthropic.text_result("не вышло"),
    ]
    body = await _run(client, uid)

    assert body["documents"] is None, body
    errored = [st for st in body["serverTools"] if st["toolName"] == "document.create"]
    assert len(errored) == 1
    assert errored[0]["status"] == "errored"
    assert errored[0]["summary"] == "invalid_document_request"


@pytest.mark.asyncio
async def test_turn_without_documents_is_null_not_empty_list(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """ADR-101 §6: `null`, а не `[]` — два одинаковых по смыслу состояния клиенту не нужны."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=20)

    fake_anthropic.responses = [fake_anthropic.text_result("просто текст")]
    body = await _run(client, uid)
    assert "documents" in body, "поле присутствует в сериализации всегда"
    assert body["documents"] is None, body
    assert body["documents"] != [], "пустой список запрещён (контраст с serverTools)"
    assert body["serverTools"] == [], "у serverTools правило ОБРАТНОЕ — он всегда хотя бы []"


@pytest.mark.asyncio
async def test_policy_blocked_turn_has_documents_null(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """Хода нет (`messageStepId=null`) — восстанавливать не из чего, чтения не происходит."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=0)

    body = await _run(client, uid)
    assert body["status"] == "blocked"
    assert body["blockReason"] == "credits_empty"
    assert body["messageStepId"] is None
    assert body["documents"] is None, body
    assert fake_anthropic.calls == []


# ==============================================================================================
# Скоуп — ХОД, а не HTTP-вызов (§4)
# ==============================================================================================


@pytest.mark.asyncio
async def test_turn_scope_create_before_handoff_present_on_both_legs(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """create на витке до hand-off виден и на ноге `/chat/run`, и на закрывающей ноге."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=20)

    fake_anthropic.responses = [
        _create(fake_anthropic, filename="A", content="aaa", tool_id="toolu_a1"),
        fake_anthropic.tool_result("files.read", {"path": "a.txt"}, tool_id="toolu_cs1"),
        fake_anthropic.text_result("готово"),
    ]
    run = await _run(client, uid)
    assert run["status"] == "tool_call", run
    assert [d["filename"] for d in run["documents"]] == ["A.md"], run["documents"]

    cont = await _tool_result(
        client, uid, session_id=run["sessionId"], tool_call_id=run["toolCalls"][0]["id"]
    )
    assert cont["status"] == "assistant_message", cont
    assert cont["messageStepId"] == run["messageStepId"], "тот же ХОД"
    assert [d["filename"] for d in cont["documents"]] == ["A.md"], cont["documents"]


@pytest.mark.asyncio
async def test_turn_scope_nonempty_continuation_accumulator_keeps_earlier_doc(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """ГЛАВНЫЙ кейс гарантии ADR-101 §3 при НЕПУСТОМ аккумуляторе continuation'а.

    A создан на витке до hand-off, B — на `/chat/tool-result` того же хода. Ответ continuation'а
    обязан нести ОБЕ записи в порядке `A, B`. На механизме «восстанавливать только при пустом
    аккумуляторе» этот кейс отдаёт одну `B` и падает — в этом его смысл; предыдущий кейс его не
    закрывает, там аккумулятор continuation'а пуст.
    """
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=20)

    fake_anthropic.responses = [
        _create(fake_anthropic, filename="A", content="aaa", tool_id="toolu_a1"),
        fake_anthropic.tool_result("files.read", {"path": "a.txt"}, tool_id="toolu_cs1"),
        _create(fake_anthropic, filename="B", content="bbb", tool_id="toolu_b1"),
        fake_anthropic.text_result("оба файла готовы"),
    ]
    run = await _run(client, uid)
    assert run["status"] == "tool_call", run
    assert [d["filename"] for d in run["documents"]] == ["A.md"]

    cont = await _tool_result(
        client, uid, session_id=run["sessionId"], tool_call_id=run["toolCalls"][0]["id"]
    )
    assert cont["status"] == "assistant_message", cont
    assert [d["filename"] for d in cont["documents"]] == [
        "A.md",
        "B.md",
    ], "документ раннего витка обязан остаться в ответе закрывающей ноги"
    assert cont["documents"][0]["documentId"] == run["documents"][0]["documentId"]


@pytest.mark.asyncio
async def test_turn_legs_agree_continuation_and_idempotent_replay(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """Согласие ног: continuation и его идемпотентный повтор дают ОДИН И ТОТ ЖЕ список."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=20)

    fake_anthropic.responses = [
        _create(fake_anthropic, filename="A", content="aaa", tool_id="toolu_a1"),
        fake_anthropic.tool_result("files.read", {"path": "a.txt"}, tool_id="toolu_cs1"),
        _create(fake_anthropic, filename="B", content="bbb", tool_id="toolu_b1"),
        fake_anthropic.text_result("оба файла готовы"),
    ]
    run = await _run(client, uid)
    sid, tcid = run["sessionId"], run["toolCalls"][0]["id"]

    cont = await _tool_result(client, uid, session_id=sid, tool_call_id=tcid)
    replay = await _tool_result(client, uid, session_id=sid, tool_call_id=tcid)

    assert (
        cont["documents"] == replay["documents"]
    ), "один ход не может отдавать два разных ответа на разных ногах"


@pytest.mark.asyncio
async def test_idempotent_replay_recovers_documents_while_server_tools_stay_empty(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """Контраст ADR-101 §4 целиком в одном тесте: documents восстановлены, serverTools пуст."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=20)

    fake_anthropic.responses = [
        fake_anthropic.tool_result("files.read", {"path": "a.txt"}, tool_id="toolu_cs1"),
        _create(fake_anthropic, filename="B", content="bbb", tool_id="toolu_b1"),
        fake_anthropic.text_result("готово"),
    ]
    run = await _run(client, uid)
    sid, tcid = run["sessionId"], run["toolCalls"][0]["id"]

    cont = await _tool_result(client, uid, session_id=sid, tool_call_id=tcid)
    assert [st["toolName"] for st in cont["serverTools"]] == ["document.create"]
    assert [d["filename"] for d in cont["documents"]] == ["B.md"]

    replay = await _tool_result(client, uid, session_id=sid, tool_call_id=tcid)
    assert replay["serverTools"] == [], "serverTools — индикатор ЗА ВЫЗОВ, на реплее пуст"
    assert [d["filename"] for d in replay["documents"]] == [
        "B.md"
    ], "documents — содержимое ХОДА, на реплее ВОССТАНАВЛИВАЕТСЯ"


@pytest.mark.asyncio
async def test_max_tokens_blocked_after_create_still_carries_documents(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """Обрыв по потолку токенов — не policy-block: у хода есть id, документ доезжает."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=20)

    fake_anthropic.responses = [
        _create(fake_anthropic, filename="trunc", content="partial", tool_id="toolu_t1"),
        fake_anthropic.max_tokens_result(text="частичный ответ...", output_tokens=16000),
    ]
    body = await _run(client, uid)

    assert body["status"] == "blocked"
    assert body["blockReason"] == "max_tokens"
    assert body["messageStepId"] is not None
    assert [d["filename"] for d in body["documents"]] == ["trunc.md"], body


# ==============================================================================================
# Дедупликация и порядок (§5)
# ==============================================================================================


@pytest.mark.asyncio
async def test_create_then_update_in_one_turn_folds_to_single_entry_version_two(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """`create` + `update` одного документа в одном ходе → ОДНА запись с финальной version=2.

    Правка приходит на continuation'е, поэтому свёртка применяется именно к ОБЪЕДИНЕНИЮ
    источников: восстановленная карточка version=1 и свежая version=2 обязаны схлопнуться.
    """
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=20)

    fake_anthropic.responses = [
        _create(fake_anthropic, filename="one", content="v1", tool_id="toolu_c1"),
        fake_anthropic.tool_result("files.read", {"path": "a.txt"}, tool_id="toolu_cs1"),
    ]
    run = await _run(client, uid)
    doc_id = run["documents"][0]["documentId"]
    assert run["documents"][0]["version"] == 1

    fake_anthropic.responses = [
        _update(fake_anthropic, document_id=doc_id, content="v2-longer", tool_id="toolu_u1"),
        fake_anthropic.text_result("обновил"),
    ]
    cont = await _tool_result(
        client, uid, session_id=run["sessionId"], tool_call_id=run["toolCalls"][0]["id"]
    )
    docs = cont["documents"]
    assert len(docs) == 1, docs
    assert docs[0]["documentId"] == doc_id
    assert docs[0]["version"] == 2, "версия ПОСЛЕДНЕЙ правки, а не предпоследней"
    assert docs[0]["size"] == len(b"v2-longer")


@pytest.mark.asyncio
async def test_two_updates_in_one_call_fold_to_one_entry_with_last_version(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=20)

    fake_anthropic.responses = [fake_anthropic.text_result("hi")]
    first = await _run(client, uid, message="hi")
    sid = first["sessionId"]
    seeded = await _rest_create(client, uid, sid, filename="edited", content="v1")
    doc_id = seeded["documentId"]

    fake_anthropic.responses = [
        _update(fake_anthropic, document_id=doc_id, content="v2", tool_id="toolu_u1"),
        _update(fake_anthropic, document_id=doc_id, content="v3", tool_id="toolu_u2"),
        fake_anthropic.text_result("две правки"),
    ]
    body = await _run(client, uid, session_id=sid, message="поправь дважды")
    docs = body["documents"]
    assert len(docs) == 1, docs
    assert docs[0]["documentId"] == doc_id
    assert docs[0]["version"] == 3, docs


@pytest.mark.asyncio
async def test_position_follows_first_appearance_not_last_touch(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """X, Y, снова X → порядок `[X, Y]`: позиция по ПЕРВОМУ появлению, значения — последние."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=20)

    fake_anthropic.responses = [fake_anthropic.text_result("hi")]
    first = await _run(client, uid, message="hi")
    sid = first["sessionId"]
    x = await _rest_create(client, uid, sid, filename="x", content="x1")
    y = await _rest_create(client, uid, sid, filename="y", content="y1")

    fake_anthropic.responses = [
        _update(fake_anthropic, document_id=x["documentId"], content="x2", tool_id="toolu_u1"),
        _update(fake_anthropic, document_id=y["documentId"], content="y2", tool_id="toolu_u2"),
        _update(fake_anthropic, document_id=x["documentId"], content="x3", tool_id="toolu_u3"),
        fake_anthropic.text_result("готово"),
    ]
    body = await _run(client, uid, session_id=sid, message="поправь оба")
    docs = body["documents"]
    assert [d["documentId"] for d in docs] == [x["documentId"], y["documentId"]], docs
    assert docs[0]["version"] == 3, "X тронут дважды — версия последней правки"
    assert docs[1]["version"] == 2


@pytest.mark.asyncio
async def test_two_distinct_documents_in_one_turn_keep_appearance_order(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=20)

    fake_anthropic.responses = [
        _create(fake_anthropic, filename="first", content="1", tool_id="toolu_c1"),
        _create(fake_anthropic, filename="second", content="2", tool_id="toolu_c2"),
        fake_anthropic.text_result("два файла"),
    ]
    body = await _run(client, uid)
    assert [d["filename"] for d in body["documents"]] == ["first.md", "second.md"], body
    assert len({d["documentId"] for d in body["documents"]}) == 2


# ==============================================================================================
# Тип поля и присутствие на всех маршрутах (§1, §8)
# ==============================================================================================


def _resolve(schema: dict[str, Any], node: dict[str, Any]) -> dict[str, Any]:
    ref = node.get("$ref")
    if not ref:
        return node
    target: Any = schema
    for part in ref.removeprefix("#/").split("/"):
        target = target[part]
    return dict(target)


@pytest.mark.asyncio
async def test_media_type_is_declared_enum_not_free_string(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """ADR-101 §1: домен закрыт и в ЗНАЧЕНИИ, и в ОБЪЯВЛЕНИИ схемы — иначе генератор клиента

    получил бы `enum` на REST-объекте и `String` здесь, на одной и той же величине.
    """
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=20)

    fake_anthropic.responses = [
        _create(fake_anthropic, media_type="text/csv", content="a,b", tool_id="toolu_c1"),
        fake_anthropic.text_result("ok"),
    ]
    body = await _run(client, uid)
    assert body["documents"][0]["mediaType"] in _MEDIA_TYPES

    spec = await client.get("/openapi.json")
    assert spec.status_code == 200, spec.text
    schema = spec.json()
    ref_schema = schema["components"]["schemas"]["ChatDocumentRefSchema"]
    prop = _resolve(schema, ref_schema["properties"]["mediaType"])
    for candidate in prop.get("allOf", []):
        prop = {**prop, **_resolve(schema, candidate)}
    assert set(prop.get("enum", [])) == _MEDIA_TYPES, prop
    assert prop.get("type") in (None, "string")


@pytest.mark.asyncio
async def test_field_present_on_v2_run_and_v2_tool_result(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """ADR-101 §8: сборка одна (`_to_response`), поэтому поле есть и на v2-маршрутах."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=20)

    fake_anthropic.responses = [
        _create(fake_anthropic, filename="v2a", content="aaa", tool_id="toolu_a1"),
        fake_anthropic.tool_result("files.read", {"path": "a.txt"}, tool_id="toolu_cs1"),
        _create(fake_anthropic, filename="v2b", content="bbb", tool_id="toolu_b1"),
        fake_anthropic.text_result("готово"),
    ]
    r1 = await client.post(
        "/v1/chat/v2/run",
        json={"userId": str(uid), "message": "go", "mode": "credits"},
        headers=auth_headers(uid),
    )
    assert r1.status_code == 200, r1.text
    run = r1.json()
    assert run["status"] == "tool_call", run
    assert [d["filename"] for d in run["documents"]] == ["v2a.md"]

    r2 = await client.post(
        "/v1/chat/v2/tool-result",
        json={
            "userId": str(uid),
            "sessionId": run["sessionId"],
            "toolCallId": run["toolCalls"][0]["id"],
            "result": {"ok": 1},
        },
        headers=auth_headers(uid),
    )
    assert r2.status_code == 200, r2.text
    cont = r2.json()
    assert [d["filename"] for d in cont["documents"]] == ["v2a.md", "v2b.md"], cont


@pytest.mark.asyncio
async def test_field_present_in_sse_done_event(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=20)

    fake_anthropic.responses = [
        _create(fake_anthropic, filename="streamed", content="s", tool_id="toolu_c1"),
        fake_anthropic.text_result("готово"),
    ]
    r = await client.post(
        "/v1/chat/v2/run/stream",
        json={"userId": str(uid), "message": "go", "mode": "credits"},
        headers=auth_headers(uid),
    )
    assert r.status_code == 200, r.text
    events = _parse_sse(r.text)
    done = [data for kind, data in events if kind == "done"]
    assert len(done) == 1, events
    assert [d["filename"] for d in done[0]["documents"]] == ["streamed.md"], done[0]


@pytest.mark.asyncio
async def test_backward_compatibility_null_documents_alongside_legacy_fields(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """Поле аддитивно: приходит как `null` и не трогает ни одно существующее поле ответа."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=20)

    fake_anthropic.responses = [fake_anthropic.text_result("plain")]
    body = await _run(client, uid)

    assert body["documents"] is None
    assert set(body) >= {
        "status",
        "sessionId",
        "messageStepId",
        "stepId",
        "assistantMessage",
        "toolCalls",
        "toolCall",
        "blockReason",
        "usage",
        "quiz",
        "mediaJobs",
        "documents",
        "serverTools",
    }, body.keys()
    assert body["status"] == "assistant_message"
    assert body["assistantMessage"] == "plain"
    assert body["mediaJobs"] is None
