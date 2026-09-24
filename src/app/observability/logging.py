"""Structured JSON logging with correlation ids and secret redaction (05-security.md)."""

from __future__ import annotations

import json
import logging
from typing import Any

from app.observability.context import get_request_id, request_id_var, session_id_var, user_id_var
from app.observability.redaction import redact


class JsonFormatter(logging.Formatter):
    """Renders log records as single-line JSON with correlation ids; redacts secrets."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "requestId": request_id_var.get(),
            "sessionId": session_id_var.get(),
            "userId": user_id_var.get(),
        }
        extra = getattr(record, "extra_fields", None)
        if isinstance(extra, dict):
            payload.update(redact(extra))
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps({k: v for k, v in payload.items() if v is not None})


# ADR-108 §10: the proxy callback carries its HMAC token in the QUERY (`?token=…`), and the token
# must not reach any log. The path prefix is declared HERE (the logging layer must not import the
# media module) and reused by `app.media_generation.webhook`.
PROXY_WEBHOOK_PATH_PREFIX = "/v1/media/webhooks/proxy"

# ADR-113 §4: the payment-page proxy paths. ``/cp/pay/<uuid>`` — the uuid is the access key to the
# payment page, so the access line keeps only ``/cp/pay/*``; ``/payment/return`` loses its query.
# Declared here (the logging layer must not import the billing module) and reused by
# ``app.billing_cloudpayments.pay_page``. ``/main.css`` and ``/main.js`` are not touched.
PAY_PAGE_PATH_PREFIX = "/cp/pay/"
PAY_PAGE_RETURN_PATH = "/payment/return"

# The server's access logger. Under `gunicorn -k uvicorn.workers.UvicornWorker` uvicorn writes the
# access line itself through THIS logger (the worker only swaps its handlers for gunicorn's
# access-log handlers), as `'%s - "%s %s HTTP/%s" %d'` with args
# (client, method, path_with_query, http_version, status).
_ACCESS_LOGGER = "uvicorn.access"
_ACCESS_PATH_ARG = 2


class AccessLogQueryRedactionFilter(logging.Filter):
    """Redact secret-bearing parts of access-log paths; every other line is left byte-for-byte.

    Two independent rules:
      - ``prefixes``: the QUERY string is dropped from paths under these prefixes (their query
        carries a secret). Applies only when the path has a ``?``.
      - ``masked_prefixes``: a path under such a prefix is replaced by ``<prefix>*`` — the path
        TAIL is the secret (the uuid of ``/cp/pay/<uuid>``, ADR-113 §4), so the tail and the query
        are both replaced REGARDLESS of whether the path has a ``?``: the normal payment-page link
        carries no query at all, and its uuid is exactly what must not reach the log.
    The record is rewritten, never dropped: the fact of the call, its method and status stay
    observable.
    """

    def __init__(self, prefixes: tuple[str, ...], masked_prefixes: tuple[str, ...] = ()) -> None:
        super().__init__()
        self._prefixes = prefixes
        self._masked_prefixes = masked_prefixes

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if not isinstance(args, tuple) or len(args) <= _ACCESS_PATH_ARG:
            return True
        path = args[_ACCESS_PATH_ARG]
        if not isinstance(path, str):
            return True
        # Masking is checked BEFORE the "no query -> leave as is" early exit on purpose.
        masked = next((p for p in self._masked_prefixes if path.startswith(p)), None)
        if masked is not None:
            replacement = f"{masked}*"
        elif "?" not in path:
            return True
        else:
            bare = path.split("?", 1)[0]
            if not any(
                bare == prefix or bare.startswith(f"{prefix}/") for prefix in self._prefixes
            ):
                return True
            replacement = bare
        record.args = (*args[:_ACCESS_PATH_ARG], replacement, *args[_ACCESS_PATH_ARG + 1 :])
        return True


def install_access_log_redaction() -> None:
    """Attach the redaction filter to the access logger once (idempotent).

    A filter lives on the LOGGER, so it survives the worker replacing the logger's handlers.
    """
    access = logging.getLogger(_ACCESS_LOGGER)
    if any(isinstance(item, AccessLogQueryRedactionFilter) for item in access.filters):
        return
    access.addFilter(
        AccessLogQueryRedactionFilter(
            (PROXY_WEBHOOK_PATH_PREFIX, PAY_PAGE_RETURN_PATH),
            masked_prefixes=(PAY_PAGE_PATH_PREFIX,),
        )
    )


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
    install_access_log_redaction()


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def log_event(logger: logging.Logger, level: int, message: str, **fields: Any) -> None:
    """Log a structured event; fields are redacted by the formatter."""
    logger.log(level, message, extra={"extra_fields": {**fields, "requestId": get_request_id()}})
