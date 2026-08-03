"""JWT authentication (RS256) with JWKS or static public key (05-security.md, Q-005-1).

Verifies signature, exp, iss, aud. Extracts sub (userId) and device_id. Never logs the
token. JWKS keys are cached for a short TTL.

Also hosts ``require_admin`` (ADR-009): the isolated admin authorization, fully separate from
``get_current_user`` — different secret, header and dependency, no provisioning/trial.
"""

from __future__ import annotations

import hmac
import uuid
from dataclasses import dataclass
from typing import Annotated

import httpx
import jwt
from fastapi import Depends, Header
from jwt import PyJWKClient

from app.api_gateway.openapi_security import admin_scheme
from app.config import get_settings
from app.errors import ForbiddenError, UnauthorizedError


@dataclass(frozen=True)
class AuthenticatedUser:
    user_id: uuid.UUID
    device_id: str | None


class JwtVerifier:
    def __init__(self) -> None:
        settings = get_settings()
        self._issuer = settings.jwt_issuer or None
        self._audience = settings.jwt_audience or None
        self._jwks_url = settings.jwt_jwks_url or None
        # Resolve the public key with file-path priority over the \n-escaped string (ADR-018 §7).
        # verify() logic itself is unchanged; this only broadens how the key is sourced.
        self._public_key = settings.resolve_public_key() or None
        # PyJWKClient keeps a per-kid cache internally, so token rotation / multiple kids
        # each resolve their own signing key. lifespan bounds how long a JWKS fetch is reused.
        self._jwks_client: PyJWKClient | None = (
            PyJWKClient(
                self._jwks_url,
                cache_keys=True,
                lifespan=settings.jwks_cache_ttl_seconds,
            )
            if self._jwks_url
            else None
        )

    def _signing_key(self, token: str) -> object:
        if self._jwks_client is not None:
            try:
                return self._jwks_client.get_signing_key_from_jwt(token).key
            except (jwt.PyJWKClientError, httpx.HTTPError) as exc:
                raise UnauthorizedError("unable to resolve signing key") from exc
        if self._public_key:
            return self._public_key
        raise UnauthorizedError("no JWT verification key configured")

    def verify(self, token: str) -> AuthenticatedUser:
        key = self._signing_key(token)
        options = {"require": ["exp", "sub"], "verify_aud": self._audience is not None}
        try:
            claims = jwt.decode(
                token,
                key=key,  # type: ignore[arg-type]
                algorithms=["RS256"],
                audience=self._audience,
                issuer=self._issuer,
                options=options,
            )
        except jwt.InvalidTokenError as exc:
            raise UnauthorizedError("invalid token") from exc

        sub = claims.get("sub")
        if not sub:
            raise UnauthorizedError("missing sub")
        try:
            user_id = uuid.UUID(str(sub))
        except ValueError as exc:
            raise UnauthorizedError("sub is not a valid user id") from exc
        return AuthenticatedUser(user_id=user_id, device_id=claims.get("device_id"))


_verifier_singleton: JwtVerifier | None = None


def get_jwt_verifier() -> JwtVerifier:
    global _verifier_singleton
    if _verifier_singleton is None:
        _verifier_singleton = JwtVerifier()
    return _verifier_singleton


def _configured_admin_secrets() -> tuple[str, ...]:
    """All non-empty admin secrets (current, rotation grace, CRM alias)."""
    settings = get_settings()
    return tuple(
        s
        for s in (
            settings.admin_api_secret,
            settings.admin_api_secret_prev,
            settings.admin_api_key,
        )
        if s
    )


def _admin_token_matches(presented: str) -> bool:
    """Constant-time compare the presented admin credential against configured secret(s).

    Accepts a match with ADMIN_API_SECRET, ADMIN_API_SECRET_PREV (rotation), or ADMIN_API_KEY
    (CRM alias). An empty/unset configured secret never matches.
    """
    matched = False
    for candidate in _configured_admin_secrets():
        if hmac.compare_digest(presented, candidate):
            matched = True
    return matched


async def require_admin(
    x_admin_key: Annotated[str | None, Header(alias="X-Admin-Key")] = None,
    x_admin_token: Annotated[str | None, Depends(admin_scheme)] = None,
) -> None:
    """Authorize an admin request (ADR-009 + broad-crm contract).

    Credentials: ``X-Admin-Key`` (CRM) or legacy ``X-Admin-Token``. Fail-closed when no secret is
    configured (401). Missing header → 403; wrong credential → 401.
    """
    if not _configured_admin_secrets():
        raise UnauthorizedError("admin api not configured")
    presented = x_admin_key if x_admin_key is not None else x_admin_token
    if presented is None:
        raise ForbiddenError("missing admin key")
    if not _admin_token_matches(presented):
        raise UnauthorizedError("invalid admin key")
