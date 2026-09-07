"""Outgoing broadapps paywall-experiment calls (ADR-098, billing-cloudpayments/03 §Эксперименты).

``BroadappsExperimentsClient`` performs a single per-call ``httpx.AsyncClient`` POST to
``{settings.cloudpayments_api_base}/experiments/assignments`` (segment assignment) or
``.../experiments/paywall-shown`` (paywall impression), with an ``application/json`` body (NOT the
multipart form used by the payment-link call next door — the body carries a nested ``context``
object) and a server-held ``Authorization: Bearer <api_token>``.

Deliberately a separate module from ``checkout.py``: the failure policy differs. Both calls
classify a failure identically (timeout / connect / non-2xx / malformed), but the CONSEQUENCE is
opposite — ``assign`` raises ``UpstreamError`` (502, the segment is never invented), while
``paywall_shown`` never raises at all and returns ``False``, because a lost telemetry event must
not break the paywall, and a 502 on this prefix means "the payment link was not created". Keeping
that policy in the same class as the money call would eventually apply it to the money call.

Exactly one structured log is emitted per call with an allowlist of fields
(billing-cloudpayments/08-observability §Эксперименты): the Bearer token, ``app_id`` and the raw
upstream body are never logged. The numeric upstream status IS logged here (unlike checkout) — the
experiment codes have no server-side allowlist, so the most likely non-2xx is an operator typo
rather than an outage, and the status is the only thing that tells them apart. It is still never
proxied outward.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import Settings
from app.errors import UpstreamError
from app.observability.logging import log_event

logger = logging.getLogger(__name__)  # == "app.billing_cloudpayments.experiments"

# Connect+read timeout for both outgoing experiment calls (ADR-098 §6). Deliberately shorter than
# the checkout timeout (15.0s): these calls sit on the paywall-rendering path, where waiting
# fifteen seconds for a statistics row is a worse trade than losing the row. No dedicated env.
_EXPERIMENTS_TIMEOUT_SECONDS = 5.0

# Server-side constant: the service has exactly one client. Not taken from the request body — an
# unvalidated string would pollute the provider's analytics dimension (ios/iOS/iphone in one
# column). A second client is a one-line change and a separate decision, not today's flexibility.
_PLATFORM = "ios"

# Final canonical locale when neither the header nor the instance default yields a usable subtag.
_FALLBACK_LOCALE = "en"

# A usable primary language subtag: two or three lowercase letters (`ru` from `ru-RU`).
_PRIMARY_SUBTAG_RE = re.compile(r"^[a-z]{2,3}$")


def resolve_experiment_locale(accept_language: str | None, default_locale: str) -> str:
    """Analytics locale for the outgoing call (ADR-098 §3). Pure — no I/O, no settings access.

    Order (first usable wins): primary subtag of the FIRST ``Accept-Language`` tag → primary
    subtag of the instance ``PRESETS_DEFAULT_LOCALE`` → ``"en"``.

    Deliberately NOT clamped to the locales our own catalogs are translated into (unlike
    ``resolve_presets_locale``): there the locale selects OUR text, so an unsupported one has
    nothing to show and must fall back; here it is the provider's analytics dimension, and
    substituting a German user with English would distort it.
    """
    for candidate in (accept_language, default_locale):
        subtag = _primary_subtag(candidate)
        if subtag is not None:
            return subtag
    return _FALLBACK_LOCALE


def _primary_subtag(tag: str | None) -> str | None:
    """Primary language subtag of the first tag in ``tag``, lower-cased, or None if unusable.

    ``"ru-RU,en;q=0.8"`` → ``"ru"``; ``"zh_Hans"`` → ``"zh"``; ``""`` / ``"*"`` / ``"123"`` → None.
    Only the FIRST tag is considered: a header we cannot parse falls through to the next source
    rather than scanning for any parseable tag further down the list.
    """
    if not tag:
        return None
    first = tag.split(",")[0].split(";")[0].strip()
    primary = first.replace("_", "-").split("-")[0].lower()
    return primary if _PRIMARY_SUBTAG_RE.match(primary) else None


@dataclass(frozen=True)
class AssignResult:
    """Projection of the broadapps assignment response (ADR-098 §4).

    ``segment_code`` is the EFFECTIVE segment (which may differ from the requested one — then
    ``requested_segment_matches`` is False and the effective one is authoritative).
    ``created`` is False when the assignment already existed: that is the provider-side
    idempotency contract, passed through as-is (we keep no assignment table or cache).
    """

    segment_code: str
    is_control: bool
    requested_segment_matches: bool
    created: bool


class BroadappsExperimentsClient:
    """Paywall-experiment passthrough to broadapps. No DB, no persisted state, no migrations."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def assign(
        self,
        *,
        user_id: uuid.UUID,
        experiment_code: str,
        segment_code: str,
        placement: str,
        locale: str,
    ) -> AssignResult:
        """POST broadapps ``/experiments/assignments`` and return the effective assignment.

        ``user_id`` is the authenticated subject (JWT ``sub``), never a client-supplied value —
        every one of our outgoing broadapps calls carries that one identity (ADR-098 §1).
        Any upstream failure maps to ``UpstreamError`` (502) without leaking the upstream
        body/status or our token: the segment is NEVER invented, because a fabricated assignment
        would reach the UI while being absent from the provider's panel.
        """
        url = f"{self._settings.cloudpayments_api_base}/experiments/assignments"
        payload = self._payload(
            user_id=user_id,
            experiment_code=experiment_code,
            segment_code=segment_code,
            placement=placement,
            locale=locale,
        )
        context: dict[str, Any] = {
            "userId": str(user_id),
            "experimentCode": experiment_code,
            "requestedSegmentCode": segment_code,
            "placement": placement,
            "locale": locale,
        }

        try:
            async with httpx.AsyncClient(timeout=_EXPERIMENTS_TIMEOUT_SECONDS) as client:
                response = await client.post(url, json=payload, headers=self._headers())
        except httpx.TimeoutException as exc:
            raise self._assign_error("timeout", context) from exc
        except httpx.RequestError as exc:
            raise self._assign_error("connect_error", context) from exc

        if not (200 <= response.status_code < 300):
            raise self._assign_error(
                "upstream_status", context, upstream_status=response.status_code
            )

        try:
            body = response.json()
        except (ValueError, UnicodeDecodeError) as exc:
            raise self._assign_error("malformed_response", context) from exc

        result = self._to_assign_result(body)
        if result is None:
            raise self._assign_error("malformed_response", context)

        log_event(
            logger,
            logging.INFO,
            "cloudpayments_experiment_assign_outcome",
            result="assigned",
            assignedSegmentCode=result.segment_code,
            isControl=result.is_control,
            requestedSegmentMatches=result.requested_segment_matches,
            created=result.created,
            **context,
        )
        return result

    async def paywall_shown(
        self,
        *,
        user_id: uuid.UUID,
        experiment_code: str,
        segment_code: str,
        placement: str,
        locale: str,
    ) -> bool:
        """POST broadapps ``/experiments/paywall-shown``; True when the provider accepted it.

        NEVER raises and never yields a 502 to the client (ADR-098 §6): a refused impression log
        must not affect the impression, and a false 502 on this prefix would devalue the code that
        means "payment link not created" right next door. The failure is not lost — it is the
        WARNING outcome below. The response body is NOT parsed at all: its shape is undocumented
        (Q-098-3), and reading what we do not know would only manufacture false ``malformed``.
        """
        url = f"{self._settings.cloudpayments_api_base}/experiments/paywall-shown"
        payload = self._payload(
            user_id=user_id,
            experiment_code=experiment_code,
            segment_code=segment_code,
            placement=placement,
            locale=locale,
        )
        context: dict[str, Any] = {
            "userId": str(user_id),
            "experimentCode": experiment_code,
            "segmentCode": segment_code,
            "placement": placement,
            "locale": locale,
        }

        try:
            async with httpx.AsyncClient(timeout=_EXPERIMENTS_TIMEOUT_SECONDS) as client:
                response = await client.post(url, json=payload, headers=self._headers())
        except httpx.TimeoutException:
            return self._paywall_shown_error("timeout", context)
        except httpx.RequestError:
            return self._paywall_shown_error("connect_error", context)

        if not (200 <= response.status_code < 300):
            return self._paywall_shown_error(
                "upstream_status", context, upstream_status=response.status_code
            )

        log_event(
            logger,
            logging.INFO,
            "cloudpayments_paywall_shown_outcome",
            result="logged",
            **context,
        )
        return True

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._settings.cloudpayments_api_token}",
            "Accept": "application/json",
        }

    def _payload(
        self,
        *,
        user_id: uuid.UUID,
        experiment_code: str,
        segment_code: str,
        placement: str,
        locale: str,
    ) -> dict[str, Any]:
        """Body shared by both outgoing calls (ADR-098 §3).

        ``app_id`` comes from instance config (a client-supplied one would steer the assignment
        into another application); ``platform`` is the server constant; ``locale`` is derived from
        the request header. The three client-supplied codes travel VERBATIM — case is not
        normalized and typos are not corrected, because the panel matches the string literally.
        """
        return {
            "experiment_code": experiment_code,
            "user_id": str(user_id),
            "app_id": self._settings.cloudpayments_app_id,
            "segment_code": segment_code,
            "context": {
                "platform": _PLATFORM,
                "locale": locale,
                "paywall": {"placement": placement},
            },
        }

    @staticmethod
    def _to_assign_result(body: Any) -> AssignResult | None:
        """Project the broadapps body into an ``AssignResult``, or None if malformed.

        ``assignment.segment.code`` is mandatory — a 2xx without it is ``malformed_response``, not
        an "empty segment". The three booleans are read STRICTLY (``is True``): a stringified
        ``"false"`` from a bad serialization must not read as truth; absent => False.
        """
        if not isinstance(body, dict):
            return None
        assignment = body.get("assignment")
        if not isinstance(assignment, dict):
            return None
        segment = assignment.get("segment")
        if not isinstance(segment, dict):
            return None
        code = segment.get("code")
        if not isinstance(code, str) or not code:
            return None
        return AssignResult(
            segment_code=code,
            is_control=segment.get("is_control") is True,
            requested_segment_matches=assignment.get("requested_segment_matches") is True,
            created=assignment.get("created") is True,
        )

    @staticmethod
    def _assign_error(
        reason: str, context: dict[str, Any], *, upstream_status: int | None = None
    ) -> UpstreamError:
        """Log the error outcome (allowlist) and build the generic 502 raised to the client."""
        extra = {"upstreamStatus": upstream_status} if upstream_status is not None else {}
        log_event(
            logger,
            logging.WARNING,
            "cloudpayments_experiment_assign_outcome",
            result="error",
            reason=reason,
            **extra,
            **context,
        )
        return UpstreamError("experiment provider unavailable")

    @staticmethod
    def _paywall_shown_error(
        reason: str, context: dict[str, Any], *, upstream_status: int | None = None
    ) -> bool:
        """Log the error outcome and return False — the caller still answers 200 {"logged": false}.

        There is no third outcome: a refused impression is ``result=error`` in the log, never a
        "success with a caveat", otherwise a provider refusal would be indistinguishable from an
        accepted event.
        """
        extra = {"upstreamStatus": upstream_status} if upstream_status is not None else {}
        log_event(
            logger,
            logging.WARNING,
            "cloudpayments_paywall_shown_outcome",
            result="error",
            reason=reason,
            **extra,
            **context,
        )
        return False
