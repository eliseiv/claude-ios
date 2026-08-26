"""OpenAI Responses API client for `/v1/chat/v2/*`.

This client is intentionally separate from `OpenAIClient`, which remains the legacy
Chat Completions implementation for `/v1/chat/*`. The v2 client converts the FULL local history
to Responses input items on every call.

The Responses API also offers provider-side conversation state through `previous_response_id`, and
the plumbing for it exists end to end: the orchestrator persists the returned response id into
`chat_sessions.provider_state`, and this client would send it and trim the input to the delta after
the last assistant turn. That path is deliberately **switched off** — see `_CONTINUATION_ENABLED`
and TD-032. Full local replay is therefore not a fallback here; it is the only path taken.

The provider state is never used for BYOK calls either: a user can rotate their key between turns,
so a stored response id could point at another OpenAI account.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any, Final

import openai

from app.chat.attachments import PreparedAttachments
from app.chat.llm_client import (
    STOP_REASON_END_TURN,
    STOP_REASON_MAX_TOKENS,
    STOP_REASON_TOOL_USE,
    LLMResult,
    LLMUsage,
    NeutralMessage,
    StreamEvent,
)
from app.chat.openai_client import (
    _PROVIDER,
    OpenAIAuthError,
    OpenAIClient,
    _log_upstream_error,
)
from app.chat.tools import UnknownToolNameError, openai_tool_function, to_domain_tool_name
from app.config import get_settings
from app.errors import UpstreamError, ValidationFailedError

# gpt-4o / gpt-4.1 reject Responses `reasoning.effort` (400 unsupported_parameter).
# The public `generationMode=reasoning` still has to work on instances whose default
# or session model is gpt-4o — remap to a catalog model that accepts the knob.
_DEFAULT_OPENAI_REASONING_MODEL = "gpt-5-mini"

#: Send `previous_response_id` to the Responses API instead of replaying local history? **No** —
#: TD-032. Kept as an explicit switch rather than as an emergent property of a name comparison,
#: because that is exactly how the chain used to be off: `provider_state.model` held the DATED
#: SNAPSHOT the provider answers with (`gpt-5.1-2025-11-13`), the caller asks for the ALIAS
#: (`gpt-5.1`), so `_usable_previous_response_id` dropped the handle on EVERY turn. Nobody chose
#: that; it was a side effect. Storing the requested alias in `usage.model` (needed for costing —
#: the purchase-price table is keyed by alias, ADR-079) would have silently flipped the chain ON,
#: which is a change of product behaviour, not a costing fix.
#:
#: Why it must not simply be flipped on, in the order a turn would break:
#:
#: 1. A response id belongs to ONE OpenAI ACCOUNT. The credits key chain (ADR-074,
#:    `Settings.openai_api_key_chain`) puts `OPENAI_API_KEY_BACKUP` — a SECOND account — behind
#:    the primary key.
#: 2. `_provider_state_for_attempt` (`app.chat.orchestrator`) hands the stored state to ANY OpenAI
#:    candidate; it compares the provider name, not the key slot that produced the handle.
#: 3. Every `/chat/run` restarts the chain at the primary key, so a handle minted by the backup
#:    account is replayed to the primary one on the next turn.
#: 4. The resulting rejection is not a credential failure, so `next_attempt_index` returns `None`
#:    and the failover chain stops instead of rotating: the turn fails.
#: 5. There is no recovery: `clear_provider_state` is called only on truncation and on explicit
#:    resets, never on an upstream error — so one poisoned handle breaks the session for good.
#:
#: Turning this on therefore means: a key slot inside `provider_state` + a slot match in
#: `_provider_state_for_attempt` + an "invalid previous_response_id" detector that clears the state
#: and retries once with full local replay. That is a new failure-handling contour and belongs to
#: its own change, with its own ADR — see TD-032 for the closing trigger.
_CONTINUATION_ENABLED: Final = False


class OpenAIResponsesClient(OpenAIClient):
    """OpenAI LLMClient implementation backed only by the Responses API.

    The class inherits the common OpenAI serializers/parsers from `OpenAIClient`, but it overrides
    the history-to-Responses conversion so full-history replay emits valid Responses input items.
    That replay is not a fallback: it is the only path this client takes while
    `_CONTINUATION_ENABLED` is off (TD-032), so the conversion runs on every turn.
    In particular, assistant text replay is encoded as an easy assistant message with string
    content instead of `output_text` content parts, which are output-only and should not be used as
    ordinary input message content.
    """

    @staticmethod
    def _obj_get(obj: Any, key: str, default: Any = None) -> Any:
        """Read SDK objects and dict-shaped test fakes through one path."""
        if isinstance(obj, dict):
            return obj.get(key, default)
        return getattr(obj, key, default)

    @classmethod
    def _obj_dump(cls, obj: Any) -> dict[str, Any]:
        """Convert SDK objects or dict-shaped fakes into a plain dict for persisted blocks."""
        if isinstance(obj, dict):
            return dict(obj)
        dump = getattr(obj, "model_dump", None)
        if callable(dump):
            dumped = dump(exclude_none=True)
            return dumped if isinstance(dumped, dict) else {}
        if hasattr(obj, "__dict__"):
            return {k: v for k, v in vars(obj).items() if v is not None}
        return {}

    @staticmethod
    def _serialize_responses_tools(
        tools: list[dict[str, Any]], generation_mode: str
    ) -> list[dict[str, Any]]:
        """Serialize neutral tools to the Responses API wire shape.

        Chat Completions uses nested ``{type:function,function:{...}}`` tools; Responses uses
        flatter ``{type:function,name,parameters,strict}`` entries. `research` appends OpenAI's
        hosted web-search tool, while `reasoning` is configured through the separate `reasoning`
        request parameter.
        """
        serialized: list[dict[str, Any]] = []
        for tool in tools:
            wrapped = openai_tool_function(tool)["function"]
            serialized.append(
                {
                    "type": "function",
                    "name": wrapped["name"],
                    "description": wrapped.get("description", ""),
                    "parameters": wrapped.get("parameters", {}),
                    "strict": False,
                }
            )
        if generation_mode == "research":
            serialized.append({"type": "web_search"})
        return serialized

    @staticmethod
    def _supports_reasoning_effort(model: str) -> bool:
        """True for OpenAI models that accept Responses ``reasoning.effort``."""
        lowered = model.strip().lower()
        return lowered.startswith(("gpt-5", "o1", "o3", "o4"))

    @classmethod
    def _model_for_generation_mode(cls, model: str, generation_mode: str) -> str:
        """Use a reasoning-capable model when the public reasoning mode is on."""
        if generation_mode == "reasoning" and not cls._supports_reasoning_effort(model):
            return _DEFAULT_OPENAI_REASONING_MODEL
        return model

    @classmethod
    def _responses_content_part(cls, block: dict[str, Any], *, user: bool) -> dict[str, Any] | None:
        """Map persisted user/attachment blocks to Responses user input content parts.

        The Responses input message content list accepts `input_text`, `input_image`,
        `input_file` and similar input parts. Assistant text is handled by
        `_responses_assistant_items_from_blocks` as a string-content assistant message, so this
        helper intentionally returns no assistant text part.
        """
        if not user:
            return None
        block_type = block.get("type")
        if block_type == "text" and isinstance(block.get("text"), str):
            return {"type": "input_text", "text": block["text"]}
        if block_type == "image_url":
            image_url = block.get("image_url")
            url = image_url.get("url") if isinstance(image_url, dict) else None
            if isinstance(url, str):
                return {"type": "input_image", "image_url": url, "detail": "auto"}
        if block_type == "file":
            file_obj = block.get("file")
            if isinstance(file_obj, dict):
                out = {"type": "input_file"}
                if isinstance(file_obj.get("filename"), str):
                    out["filename"] = file_obj["filename"]
                if isinstance(file_obj.get("file_data"), str):
                    out["file_data"] = file_obj["file_data"]
                if len(out) > 1:
                    return out
        return None

    @classmethod
    def _responses_user_message_from_blocks(
        cls, blocks: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        """Convert persisted user blocks into one Responses input message item."""
        parts = [p for b in blocks if (p := cls._responses_content_part(b, user=True))]
        if not parts:
            return None
        return {"type": "message", "role": "user", "content": parts}

    @classmethod
    def _responses_assistant_items_from_blocks(
        cls, blocks: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Convert persisted assistant blocks into Responses input items for full replay.

        Full replay is the only path this client takes while `_CONTINUATION_ENABLED` is off
        (TD-032), so this conversion runs on every turn, not just when `previous_response_id` is
        missing. The goal is to rebuild enough local conversation state for the next turn without
        depending on provider storage. Text is replayed as an assistant message; tool calls are
        replayed as `function_call` items with their original provider `call_id`; reasoning
        summaries are kept only when they already carry provider ids. Hosted web-search call
        metadata is intentionally skipped because it is diagnostic output rather than user-visible
        context.
        """

        items: list[dict[str, Any]] = []
        text_parts: list[str] = []

        def flush_text() -> None:
            if text_parts:
                items.append(
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": "".join(text_parts),
                    }
                )
                text_parts.clear()

        def append_function_call(
            *, call_id: Any, name: Any, arguments: Any, parsed_input: Any = None
        ) -> None:
            if not isinstance(call_id, str) or not call_id:
                return
            if not isinstance(name, str) or not name:
                return
            if isinstance(arguments, str) and arguments:
                args = arguments
            else:
                args = json.dumps(parsed_input if isinstance(parsed_input, dict) else {})
            flush_text()
            items.append(
                {
                    "type": "function_call",
                    "call_id": call_id,
                    "name": name,
                    "arguments": args,
                }
            )

        for block in blocks:
            if block.get("role") == "assistant":
                content = block.get("content")
                if isinstance(content, str) and content:
                    text_parts.append(content)
                elif isinstance(content, list):
                    for part in content:
                        if (
                            isinstance(part, dict)
                            and part.get("type") in {"text", "output_text"}
                            and isinstance(part.get("text"), str)
                        ):
                            text_parts.append(part["text"])
                for tc in block.get("tool_calls") or []:
                    fn = tc.get("function") if isinstance(tc, dict) else None
                    if isinstance(fn, dict):
                        append_function_call(
                            call_id=tc.get("id"),
                            name=fn.get("name"),
                            arguments=fn.get("arguments"),
                        )
                continue

            block_type = block.get("type")
            if block_type == "text" and isinstance(block.get("text"), str):
                text_parts.append(block["text"])
                continue
            if block_type == "tool_use":
                append_function_call(
                    call_id=block.get("id"),
                    name=block.get("name"),
                    arguments=None,
                    parsed_input=block.get("input"),
                )
                continue
            if block_type == "reasoning":
                reasoning = {
                    k: v
                    for k, v in block.items()
                    if k in {"id", "summary", "type", "content", "encrypted_content", "status"}
                }
                if "id" in reasoning and "summary" in reasoning:
                    flush_text()
                    items.append(reasoning)
                continue

        flush_text()
        return items

    @staticmethod
    def _responses_tool_output(msg: NeutralMessage) -> dict[str, Any]:
        """Convert a persisted tool result step to a Responses function_call_output item."""
        payload = msg.error if msg.error is not None else msg.result
        return {
            "type": "function_call_output",
            "call_id": msg.provider_tool_use_id,
            "output": json.dumps(payload, ensure_ascii=False),
        }

    @classmethod
    def _messages_after_last_assistant(
        cls, messages: list[NeutralMessage] | list[dict[str, Any]]
    ) -> list[NeutralMessage] | list[dict[str, Any]]:
        """Return only the local delta that should follow `previous_response_id`.

        Unreachable while `_CONTINUATION_ENABLED` is off (TD-032): the only caller trims to the
        delta when it holds a `previous_response_id`, and today it never does. Kept as the shape
        the switch would restore.
        """
        last_assistant = -1
        for i, msg in enumerate(messages):
            role = cls._obj_get(msg, "role")
            if role == "assistant":
                last_assistant = i
        return messages[last_assistant + 1 :] if last_assistant >= 0 else messages

    @classmethod
    def _responses_input_from_messages(
        cls,
        messages: list[NeutralMessage] | list[dict[str, Any]],
        *,
        previous_response_id: str | None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Build Responses input, trimming to the delta after `previous_response_id` when set.

        `previous_response_id` is always `None` here while `_CONTINUATION_ENABLED` is off (TD-032):
        the sole producer, `_usable_previous_response_id`, returns `None` on every turn. So the
        trimming branch below is currently dead and every call builds the FULL local replay
        converted from `chat_steps` — that is v2's normal path, not a fallback from a lost handle.

        The branch is kept because it is the request shape flipping the constant back on would
        restore: with a usable id only messages after the latest assistant turn are sent — either
        the new user turn or function-call outputs for a closed tool barrier.
        """
        source = cls._messages_after_last_assistant(messages) if previous_response_id else messages
        if previous_response_id and not source:
            previous_response_id = None
            source = messages

        items: list[dict[str, Any]] = []
        for msg in source:
            if isinstance(msg, NeutralMessage):
                if msg.role == "user":
                    item = cls._responses_user_message_from_blocks(msg.content_blocks)
                    if item is not None:
                        items.append(item)
                elif msg.role == "assistant":
                    items.extend(cls._responses_assistant_items_from_blocks(msg.content_blocks))
                elif msg.role == "tool":
                    items.append(cls._responses_tool_output(msg))
                continue

            role = msg.get("role")
            if role == "user":
                content = msg.get("content")
                if isinstance(content, str):
                    items.append(
                        {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": content}],
                        }
                    )
                elif isinstance(content, list):
                    blocks = [b for b in content if isinstance(b, dict)]
                    item = cls._responses_user_message_from_blocks(blocks)
                    if item is not None:
                        items.append(item)
            elif role == "assistant":
                items.extend(cls._responses_assistant_items_from_blocks([msg]))
            elif role == "tool":
                items.append(
                    {
                        "type": "function_call_output",
                        "call_id": msg.get("tool_call_id"),
                        "output": str(msg.get("content") or ""),
                    }
                )
        return items, previous_response_id

    @classmethod
    def _inject_responses_attachments(
        cls, input_items: list[dict[str, Any]], attachments: PreparedAttachments
    ) -> None:
        """Inject first-turn attachment content parts into the last Responses user message."""
        if not attachments.content_blocks:
            return
        converted = [
            p
            for block in attachments.content_blocks
            if (p := cls._responses_content_part(block, user=True)) is not None
        ]
        if not converted:
            return
        for item in reversed(input_items):
            if item.get("type") == "message" and item.get("role") == "user":
                content = item.get("content")
                base = content if isinstance(content, list) else []
                item["content"] = [*base, *converted]
                return

    @staticmethod
    def _usable_previous_response_id(
        provider_state: dict[str, Any] | None, *, model: str
    ) -> str | None:
        """Return a usable `previous_response_id` for this OpenAI model, if one exists.

        Always `None` while `_CONTINUATION_ENABLED` is off (TD-032): the stored handle is bound to
        one OpenAI ACCOUNT, and nothing on the call path guarantees the next turn reaches that same
        account — see the constant for the full chain and for what turning it on would require.
        The checks below are the model binding that applies once it IS on: a response id used with
        a different model can make the provider reject the request or silently continue a chain
        with the wrong assumptions, so v2 falls back to full local replay in that case.
        """
        if not _CONTINUATION_ENABLED:
            return None
        if not isinstance(provider_state, dict):
            return None
        if provider_state.get("provider") != _PROVIDER:
            return None
        state_model = provider_state.get("model")
        if isinstance(state_model, str) and state_model and state_model != model:
            return None
        response_id = provider_state.get("responseId")
        return response_id if isinstance(response_id, str) and response_id else None

    def _parse_responses_usage(self, response: Any, model: str) -> LLMUsage:
        """Parse Responses API token accounting into the provider-neutral usage object.

        ``model`` is the name we ASKED for, and it is the name that lands in
        ``chat_steps.usage.model`` — the same convention the Anthropic and Chat Completions clients
        follow, so one stored field means one thing across providers. The Responses API answers
        with a DATED SNAPSHOT of that name (``gpt-5.1-2025-11-13`` for ``gpt-5.1``); storing the
        snapshot instead made the whole turn unpriceable, because the purchase-price table is keyed
        by the alias, and the CRM «Себестоимость» column went empty (ADR-079).

        ``chat_sessions.provider_state.model`` is written from this same field. It is NOT the reason
        this returns the alias, and it does not gate anything today: continuation is off by an
        explicit switch (``_CONTINUATION_ENABLED``, TD-032), not by whether these two names match.
        """
        usage = getattr(response, "usage", None)
        if usage is None:
            return LLMUsage(0, 0, model, 0, 0)
        input_details = self._obj_get(usage, "input_tokens_details")
        output_details = self._obj_get(usage, "output_tokens_details")
        web_search_requests = 0
        for item in getattr(response, "output", []) or []:
            if self._obj_get(item, "type") == "web_search_call":
                web_search_requests += 1
        return LLMUsage(
            input_tokens=self._obj_get(usage, "input_tokens", 0) or 0,
            output_tokens=self._obj_get(usage, "output_tokens", 0) or 0,
            model=model,
            cache_read_tokens=self._obj_get(input_details, "cached_tokens", 0) or 0,
            cache_write_tokens=0,
            reasoning_tokens=self._obj_get(output_details, "reasoning_tokens", 0) or 0,
            web_search_requests=web_search_requests,
        )

    def _parse_responses_result(self, response: Any, model: str) -> LLMResult:
        """Parse a Responses API response into the shared LLMResult contract.

        Function calls are exposed to the orchestrator as the same domain-shaped `tool_uses` as the
        legacy Chat Completions client. Text, reasoning and hosted-search output items are persisted
        as compact blocks because local history is what every next turn replays: continuation via
        `previous_response_id` is switched off by `_CONTINUATION_ENABLED` (TD-032), so these blocks
        are the only conversation state the client has.
        """
        content_blocks: list[dict[str, Any]] = []
        text_parts: list[str] = []
        tool_uses: list[dict[str, Any]] = []
        web_search_calls = 0

        for item in getattr(response, "output", []) or []:
            item_type = self._obj_get(item, "type")
            if item_type == "message":
                for part in self._obj_get(item, "content", []) or []:
                    part_type = self._obj_get(part, "type")
                    if part_type == "output_text":
                        text = self._obj_get(part, "text", "") or ""
                        block: dict[str, Any] = {"type": "text", "text": text}
                        annotations = self._obj_get(part, "annotations", None)
                        if annotations:
                            dumped = [self._obj_dump(a) for a in annotations]
                            block["annotations"] = [a for a in dumped if a]
                        content_blocks.append(block)
                        text_parts.append(text)
                continue
            if item_type == "function_call":
                wire_name = str(self._obj_get(item, "name", ""))
                arguments = self._obj_get(item, "arguments", "") or "{}"
                call_id = str(self._obj_get(item, "call_id", "") or self._obj_get(item, "id", ""))
                try:
                    domain_name = to_domain_tool_name(wire_name)
                except UnknownToolNameError as exc:
                    raise ValidationFailedError(str(exc)) from exc
                try:
                    parsed_args = json.loads(arguments) if arguments else {}
                except (ValueError, json.JSONDecodeError) as exc:
                    raise ValidationFailedError(
                        f"invalid function_call arguments JSON for {wire_name}"
                    ) from exc
                if not isinstance(parsed_args, dict):
                    raise ValidationFailedError(
                        f"function_call arguments for {wire_name} must be a JSON object"
                    )
                content_blocks.append(
                    {"type": "tool_use", "id": call_id, "name": wire_name, "input": parsed_args}
                )
                tool_uses.append({"id": call_id, "name": domain_name, "input": parsed_args})
                continue
            if item_type == "reasoning":
                block = {
                    k: v
                    for k, v in self._obj_dump(item).items()
                    if k in {"id", "summary", "type", "content", "encrypted_content", "status"}
                }
                if block:
                    content_blocks.append(block)
                continue
            if item_type == "web_search_call":
                web_search_calls += 1
                block = self._obj_dump(item)
                if block:
                    content_blocks.append(block)

        if not text_parts:
            output_text = getattr(response, "output_text", None)
            if isinstance(output_text, str) and output_text:
                content_blocks.append({"type": "text", "text": output_text})
                text_parts.append(output_text)

        status = getattr(response, "status", None)
        incomplete_details = getattr(response, "incomplete_details", None)
        incomplete_reason = self._obj_get(incomplete_details, "reason")
        if tool_uses:
            stop_reason = STOP_REASON_TOOL_USE
        elif status == "incomplete" and incomplete_reason == "max_output_tokens":
            stop_reason = STOP_REASON_MAX_TOKENS
        else:
            stop_reason = STOP_REASON_END_TURN

        usage = self._parse_responses_usage(response, model)
        if web_search_calls and usage.web_search_requests == 0:
            usage = LLMUsage(
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                model=usage.model,
                cache_read_tokens=usage.cache_read_tokens,
                cache_write_tokens=usage.cache_write_tokens,
                reasoning_tokens=usage.reasoning_tokens,
                web_search_requests=web_search_calls,
            )
        return LLMResult(
            stop_reason=stop_reason,
            content_blocks=content_blocks,
            usage=usage,
            text="".join(text_parts),
            tool_uses=tool_uses,
            provider_response_id=self._obj_get(response, "id"),
        )

    async def create_message(
        self,
        *,
        system_prompt: str,
        messages: list[NeutralMessage] | list[dict[str, Any]],
        tools: list[dict[str, Any]],
        attachments: PreparedAttachments | None = None,
        api_key: str | None = None,
        model: str | None = None,
        generation_mode: str = "general",
        provider_state: dict[str, Any] | None = None,
    ) -> LLMResult:
        """Create one v2 OpenAI response via the Responses API.

        `generation_mode=general` uses normal Responses API generation. `research` appends the
        hosted OpenAI web-search tool. `reasoning` sends the configured reasoning effort. The
        request always carries a full-history Responses input reconstructed from local
        `chat_steps`: provider-side continuation is switched off by `_CONTINUATION_ENABLED`
        (TD-032), so `_usable_previous_response_id` returns `None` on every turn and no
        `previous_response_id` is sent. Flipping that constant back on — under the conditions
        listed there — is what restores the delta-only request.
        """
        model = model if model is not None else self._default_model
        # The whitelist DECLARES the modes this client supports and must list all four (ADR-064).
        # `study_learn` adds no provider knob (by request parameters it IS `general`); its whole
        # difference lives in the tool-set and the system prompt on the orchestrator side. Relying
        # on the silent fallback instead of listing it would mask an axis de-sync later.
        generation_mode = (
            generation_mode
            if generation_mode in {"general", "research", "reasoning", "study_learn"}
            else "general"
        )
        model = self._model_for_generation_mode(model, generation_mode)
        client = self._client
        if api_key is not None:
            client = client.with_options(api_key=api_key)

        responses_api = getattr(client, "responses", None)
        if responses_api is None:
            raise UpstreamError("openai responses api is unavailable in this SDK")

        previous_response_id = self._usable_previous_response_id(provider_state, model=model)
        input_items, previous_response_id = self._responses_input_from_messages(
            messages, previous_response_id=previous_response_id
        )
        if attachments is not None:
            self._inject_responses_attachments(input_items, attachments)

        response_tools = self._serialize_responses_tools(tools, generation_mode)
        settings = get_settings()
        reasoning = (
            {"effort": settings.resolved_reasoning_level()}
            if generation_mode == "reasoning"
            else openai.NOT_GIVEN
        )
        try:
            response = await responses_api.create(
                model=model,
                instructions=system_prompt,
                input=input_items if input_items else "",
                max_output_tokens=self._max_tokens,
                tools=response_tools or openai.NOT_GIVEN,
                reasoning=reasoning,
                previous_response_id=previous_response_id or openai.NOT_GIVEN,
                store=True,
            )
        except openai.AuthenticationError as exc:
            _log_upstream_error(exc, model=model, status_code=getattr(exc, "status_code", 401))
            raise OpenAIAuthError(str(exc)) from exc
        except (openai.APITimeoutError, openai.APIConnectionError) as exc:
            _log_upstream_error(exc, model=model, status_code=None)
            raise UpstreamError("openai upstream error") from exc
        except openai.APIStatusError as exc:
            _log_upstream_error(exc, model=model, status_code=getattr(exc, "status_code", None))
            raise UpstreamError("openai upstream error") from exc

        return self._parse_responses_result(response, model)

    async def stream_message(
        self,
        *,
        system_prompt: str,
        messages: list[NeutralMessage] | list[dict[str, Any]],
        tools: list[dict[str, Any]],
        attachments: PreparedAttachments | None = None,
        api_key: str | None = None,
        model: str | None = None,
        generation_mode: str = "general",
        provider_state: dict[str, Any] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Stream Responses API output text (ADR-069), then emit completed LLMResult."""
        model = model if model is not None else self._default_model
        generation_mode = (
            generation_mode
            if generation_mode in {"general", "research", "reasoning", "study_learn"}
            else "general"
        )
        model = self._model_for_generation_mode(model, generation_mode)
        client = self._client
        if api_key is not None:
            client = client.with_options(api_key=api_key)

        responses_api = getattr(client, "responses", None)
        if responses_api is None:
            raise UpstreamError("openai responses api is unavailable in this SDK")

        previous_response_id = self._usable_previous_response_id(provider_state, model=model)
        input_items, previous_response_id = self._responses_input_from_messages(
            messages, previous_response_id=previous_response_id
        )
        if attachments is not None:
            self._inject_responses_attachments(input_items, attachments)

        response_tools = self._serialize_responses_tools(tools, generation_mode)
        settings = get_settings()
        reasoning = (
            {"effort": settings.resolved_reasoning_level()}
            if generation_mode == "reasoning"
            else openai.NOT_GIVEN
        )
        create_kwargs: dict[str, Any] = {
            "model": model,
            "instructions": system_prompt,
            "input": input_items if input_items else "",
            "max_output_tokens": self._max_tokens,
            "tools": response_tools or openai.NOT_GIVEN,
            "reasoning": reasoning,
            "previous_response_id": previous_response_id or openai.NOT_GIVEN,
            "store": True,
        }

        # Prefer SDK stream context manager; fall back to create(stream=True) or non-stream.
        stream_cm = getattr(responses_api, "stream", None)
        try:
            if callable(stream_cm):
                async with stream_cm(**create_kwargs) as stream:
                    async for event in stream:
                        etype = getattr(event, "type", None) or ""
                        if etype in {
                            "response.output_text.delta",
                            "response.text.delta",
                        }:
                            delta = getattr(event, "delta", None)
                            if isinstance(delta, str) and delta:
                                yield StreamEvent.text_delta(delta)
                    response = await stream.get_final_response()
            else:
                stream = await responses_api.create(**create_kwargs, stream=True)
                final_response: Any = None
                async for event in stream:
                    etype = getattr(event, "type", None) or ""
                    if etype in {"response.output_text.delta", "response.text.delta"}:
                        delta = getattr(event, "delta", None)
                        if isinstance(delta, str) and delta:
                            yield StreamEvent.text_delta(delta)
                    if etype == "response.completed":
                        final_response = getattr(event, "response", None) or final_response
                    if hasattr(event, "response") and etype.endswith("completed"):
                        final_response = event.response
                if final_response is None and hasattr(stream, "get_final_response"):
                    final_response = await stream.get_final_response()
                if final_response is None:
                    # Last-resort: non-stream create (single synthetic delta).
                    response = await responses_api.create(**create_kwargs)
                    result = self._parse_responses_result(response, model)
                    if result.text:
                        yield StreamEvent.text_delta(result.text)
                    yield StreamEvent.completed(result)
                    return
                response = final_response
        except openai.AuthenticationError as exc:
            _log_upstream_error(exc, model=model, status_code=getattr(exc, "status_code", 401))
            raise OpenAIAuthError(str(exc)) from exc
        except (openai.APITimeoutError, openai.APIConnectionError) as exc:
            _log_upstream_error(exc, model=model, status_code=None)
            raise UpstreamError("openai upstream error") from exc
        except TypeError:
            # Older SDK without stream kwargs — fall back to create_message shape.
            result = await self.create_message(
                system_prompt=system_prompt,
                messages=messages,
                tools=tools,
                attachments=attachments,
                api_key=api_key,
                model=model,
                generation_mode=generation_mode,
                provider_state=provider_state,
            )
            if result.text:
                yield StreamEvent.text_delta(result.text)
            yield StreamEvent.completed(result)
            return
        except openai.APIStatusError as exc:
            _log_upstream_error(exc, model=model, status_code=getattr(exc, "status_code", None))
            raise UpstreamError("openai upstream error") from exc

        yield StreamEvent.completed(self._parse_responses_result(response, model))
