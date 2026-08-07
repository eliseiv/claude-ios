"""Global (project-independent) server-side tool handlers (ADR-026).

Unlike SiteToolHandlers (project-scoped site.*, ADR-011), these handlers are NOT tied to a
WebsiteService/project — they execute in the chat tool-loop without an external_project_id and
are offered to Claude in every turn (including «чистый чат» with no project, ADR-022).

Two tools today. ``quiz.generate`` (ADR-064) validates and echoes a quiz pool; it is global in the
same sense (no project) but, unlike ``time.now``, it is offered to the model ONLY in the
``study_learn`` generation mode (axis C) — «global» means «needs no project», not «offered always».

``time.now`` (ADR-026 §6) returns the current date/time via an
injectable ``Clock`` provider (determinism for qa, ADR-026 §8 / 06-testing-strategy) — never a
direct ``datetime.now()``. The result always carries a UTC set (``utc``/``unix``/``weekday``);
a valid IANA ``tz`` additionally yields ``local``/``timezone``. An invalid/unknown/over-long tz
degrades to a ``ToolExecution.error("invalid_timezone", ...)`` (the turn survives, ADR-026 §6) —
never a raised exception.

The same ``ToolExecution`` contract as SiteToolHandlers is reused (single tool-result contract for
the orchestrator). Only the frozen dataclass is imported from website.tools — no website
infrastructure is instantiated here (ADR-026 §5).
"""

from __future__ import annotations

import datetime
from typing import Any, Protocol, runtime_checkable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import ValidationError

from app.chat.tools import (
    QUIZ_CONSTRAINTS_HINT,
    QUIZ_INVALID_ERROR_CODE,
    TIME_NOW_TZ_MAX_LENGTH,
    TOOL_QUIZ_GENERATE,
    TOOL_TIME_NOW,
    Quiz,
    content_free_args_error,
)
from app.website.tools import ToolExecution

# English weekday names by UTC date (Monday..Sunday), ADR-026 §6.
_WEEKDAYS = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)


@runtime_checkable
class Clock(Protocol):
    """Injectable source of the current time (ADR-026 §8).

    ``now()`` MUST return a timezone-aware UTC ``datetime``. The default implementation is
    ``SystemClock``; tests inject a ``FixedClock`` for determinism.
    """

    def now(self) -> datetime.datetime: ...


class SystemClock:
    """Default Clock: the real wall-clock time in UTC (ADR-026 §8)."""

    def now(self) -> datetime.datetime:
        return datetime.datetime.now(tz=datetime.UTC)


class GlobalToolHandlers:
    """Dispatch + handlers for global server-side tools (ADR-026).

    Project-independent: no WebsiteService, no external_project_id, no session-context args. Time is
    taken from the injected ``Clock`` (default ``SystemClock``).
    """

    def __init__(self, clock: Clock | None = None) -> None:
        self._clock = clock if clock is not None else SystemClock()

    async def execute(self, *, tool_name: str, args: dict[str, Any]) -> ToolExecution:
        """Execute a global server-side tool. Returns a ToolExecution (result or error envelope)."""
        if tool_name == TOOL_TIME_NOW:
            return self._time_now(args)
        if tool_name == TOOL_QUIZ_GENERATE:
            return self._quiz_generate(args)
        # Unknown global tool name — should never happen (validated upstream against the registry).
        return ToolExecution.error("unknown_tool", f"unknown global server-side tool: {tool_name}")

    @staticmethod
    def _quiz_generate(args: dict[str, Any]) -> ToolExecution:
        """Validate the quiz pool and echo it back as the tool result (ADR-064 §2, §5).

        "Execution" of quiz.generate is validation + echo: the pool is produced by the model, the
        backend only guarantees the contract no provider can guarantee (counts, lengths, the
        cross-field ``correctIndex < len(options)``). The echoed object is what the tool step
        persists and what the response's ``quiz`` field carries.

        All-or-nothing (ADR-064 §5): ANY violation invalidates the WHOLE pool — one
        ``invalid_quiz`` tool-result error, the turn SURVIVES and the model regenerates the pool in
        the same turn. Partial acceptance (dropping the bad question) is forbidden: it would
        silently shrink the pool and hide the problem from the model. The message is CONTENT-FREE
        (field path + error kind only) so quiz text never leaks into the persisted tool-result echo.

        NOTE — the ``except`` branch below is DEFENSIVE, not the working path. On the live path the
        orchestrator validates args BEFORE dispatching here, and a failure there never reaches this
        handler: it is turned into the very same ``invalid_quiz`` tool-result by the degrade branch
        of ``_handle_tool_use`` (ADR-064 §5). This branch therefore only fires for direct calls to
        this handler (unit tests, a future caller that skips the orchestrator's validation), and it
        is kept so the contract holds for them too — the two producers are deliberately identical in
        code, message shape and behaviour.
        """
        try:
            validated = Quiz.model_validate(args)
        except ValidationError as exc:
            return ToolExecution.error(
                QUIZ_INVALID_ERROR_CODE,
                f"{content_free_args_error(exc)}; {QUIZ_CONSTRAINTS_HINT}",
            )
        return ToolExecution.ok(validated.model_dump())

    def _time_now(self, args: dict[str, Any]) -> ToolExecution:
        now_utc = self._clock.now()
        # Defensive: a Clock contract violation (naive/non-UTC) would corrupt the offsets; normalize
        # to UTC so utc/unix/weekday are always correct (ADR-026 §6 — UTC set independent of tz).
        if now_utc.tzinfo is None:
            now_utc = now_utc.replace(tzinfo=datetime.UTC)
        else:
            now_utc = now_utc.astimezone(datetime.UTC)

        result: dict[str, Any] = {
            "utc": now_utc.isoformat(),
            # Integer Unix timestamp in seconds (UTC), ADR-026 §6.
            "unix": int(now_utc.timestamp()),
            "weekday": _WEEKDAYS[now_utc.weekday()],
        }

        tz_raw = args.get("tz")
        if tz_raw is None:
            # No tz → UTC-only set (timezone/local omitted), ADR-026 §6.
            return ToolExecution.ok(result)

        tz_name = str(tz_raw)
        # Q-026-1: length cap (≤ 64) enforced here so an over-long tz degrades to invalid_timezone
        # (a tool-result error, the turn survives) rather than 422-ing the turn (ADR-026 §6).
        if len(tz_name) > TIME_NOW_TZ_MAX_LENGTH:
            return ToolExecution.error(
                "invalid_timezone",
                f"timezone name exceeds the {TIME_NOW_TZ_MAX_LENGTH}-character limit",
            )
        try:
            zone = ZoneInfo(tz_name)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            # Unknown/unparseable IANA name, missing tz database in the image (TD-019), or a
            # filesystem-hostile name when a tz database IS present (ZoneInfo treats the name as a
            # path and the OS rejects it, e.g. OSError Errno 22) → invalid_timezone tool-result
            # error; the UTC set is still available, the turn survives (ADR-026 §6).
            return ToolExecution.error(
                "invalid_timezone", f"unknown or unavailable timezone: {tz_name}"
            )

        local_dt = now_utc.astimezone(zone)
        # Normalized IANA name (key(zone) is the canonical name passed to ZoneInfo), ADR-026 §6.
        result["timezone"] = str(zone.key)
        result["local"] = local_dt.isoformat()
        return ToolExecution.ok(result)
