"""FastAPI dependencies: auth, db session, owner check, service wiring (api-gateway/03)."""

from __future__ import annotations

import ipaddress
import uuid
from collections.abc import AsyncIterator
from functools import lru_cache
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.admin.crm_service import CrmAdminService
from app.admin.service import AdminService
from app.api_gateway.auth import AuthenticatedUser, get_jwt_verifier
from app.api_gateway.openapi_security import bearer_scheme
from app.audit.service import AuditService
from app.auth.apple import get_apple_verifier
from app.auth.issuer import TokenIssuer
from app.auth.service import AuthService
from app.billing_adapty.service import AdaptyWebhookService
from app.billing_cloudpayments.checkout import CloudPaymentsCheckoutClient
from app.billing_cloudpayments.service import CloudPaymentsWebhookService
from app.billing_cloudpayments.verify import CloudPaymentsVerifyClient
from app.byok.kms import get_kms_client
from app.byok.service import BYOKService
from app.chat.global_tools import GlobalToolHandlers, SystemClock
from app.chat.llm_client import get_generation_llm_client, get_llm_client
from app.chat.orchestrator import ChatOrchestrator
from app.chat.repository import ChatRepository
from app.chats.repository import ChatsRepository
from app.chats.service import ChatsService
from app.config import get_settings
from app.db import session_scope
from app.documents import DocumentsService
from app.errors import ForbiddenError, MediaGenerationNotConfiguredError, UnauthorizedError
from app.media_generation.fal_client import FalClient
from app.media_generation.repository import MediaJobsRepository
from app.media_generation.service import MediaGenerationService
from app.media_generation.templates_repository import MediaTemplatesRepository
from app.media_generation.templates_service import MediaTemplatesService
from app.memory.service import MemoryService
from app.moderation import ModerationService
from app.notifications.apns_client import ApnsClient
from app.notifications.push_service import MediaPushService
from app.notifications.repository import DevicePushTokensRepository
from app.notifications.service import NotificationsService
from app.observability.context import set_user_id
from app.preferences.service import PreferencesService
from app.profile.service import ProfileService
from app.request_logs.service import RequestLogWriter
from app.subscription.service import SubscriptionService
from app.subscription.storekit import get_storekit_verifier
from app.token_purchase.service import TokenPurchaseService
from app.wallet.service import WalletService
from app.website.service import WebsiteService
from app.website.tools import SiteToolHandlers
from app.workspaces.repository import WorkspacesRepository
from app.workspaces.service import WorkspacesService


async def get_db() -> AsyncIterator[AsyncSession]:
    async for session in session_scope():
        yield session


def verify_bearer_token(authorization: str | None) -> AuthenticatedUser:
    """Verify the Bearer JWT (signature/exp/iss/aud) and extract the trusted subject.

    Pure, side-effect-free (no DB, no logging of the token) so it stays unit-testable in
    isolation. Identity comes exclusively from the verified ``sub`` claim (ADR-007).
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise UnauthorizedError("missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    return get_jwt_verifier().verify(token)


async def provision_user(session: AsyncSession, user_id: uuid.UUID) -> None:
    """Lazy, idempotent provisioning of the ``users`` row for a verified subject (ADR-007).

    Runs in the *same* per-request session that downstream use-cases use for their FK-bearing
    inserts (subscriptions/wallets/byok_keys/ledger/chat_sessions). The statement is emitted
    immediately against the connection, so the row is visible to every later statement of this
    transaction *before* any FK insert — and is committed together with them. ``ON CONFLICT
    (id) DO NOTHING`` is atomic in PostgreSQL: concurrent first requests for the same ``sub``
    cannot race or duplicate, and an already-provisioned user's ``trial_used``/``created_at``
    are never overwritten. ``created_at``/``trial_used`` come from the DDL defaults.
    """
    await session.execute(
        text("INSERT INTO users (id) VALUES (:sub) ON CONFLICT (id) DO NOTHING"),
        {"sub": str(user_id)},
    )


async def get_current_user(
    session: Annotated[AsyncSession, Depends(get_db)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)] = None,
) -> AuthenticatedUser:
    """Authenticate the request and lazily provision the user (ADR-007).

    Single point through which all authenticated ``/v1/*`` requests pass, so the lazy
    provisioning here uniformly covers every write endpoint without per-flow duplication.
    Provisioning happens only *after* full JWT verification (an invalid/expired token raises
    401 before any row is created) and *before* the subject is used downstream.

    The credentials come from ``bearer_scheme`` (HTTPBearer, ``auto_error=False``), which is a
    ``SecurityBase``: it contributes the ``bearerAuth`` security scheme to OpenAPI (lock icon /
    Authorize button) *without* adding a separate ``authorization`` header parameter to every
    operation. ``auto_error=False`` keeps the scheme from raising on a missing/malformed header
    — the real 401 stays in ``verify_bearer_token`` so behaviour is unchanged (08-api-doc R2).
    """
    # Re-assemble the canonical "Bearer <token>" string so verify_bearer_token keeps its public
    # signature (a Header-shaped value) and its 401 semantics (missing/non-Bearer/invalid token).
    authorization = f"Bearer {credentials.credentials}" if credentials is not None else None
    user = verify_bearer_token(authorization)
    set_user_id(str(user.user_id))
    # FastAPI caches `get_db` per request, so `session` is the exact session the service
    # dependencies (orchestrator/wallet/subscription/byok) receive — the upsert lands in the
    # same transaction as their FK-bearing inserts.
    await provision_user(session, user.user_id)
    return user


CurrentUser = Annotated[AuthenticatedUser, Depends(get_current_user)]
DbSession = Annotated[AsyncSession, Depends(get_db)]


def require_owner(body_user_id: uuid.UUID, current: AuthenticatedUser) -> None:
    """userId in the body must equal sub (403 otherwise) — 05-security.md."""
    if body_user_id != current.user_id:
        raise ForbiddenError("userId does not match authenticated subject")


_token_issuer_singleton: TokenIssuer | None = None


def get_token_issuer() -> TokenIssuer:
    """Process-wide TokenIssuer (RS256). Reads the key pair once from the cached settings."""
    global _token_issuer_singleton
    if _token_issuer_singleton is None:
        _token_issuer_singleton = TokenIssuer(get_settings())
    return _token_issuer_singleton


def get_auth_service(session: DbSession) -> AuthService:
    return AuthService(session, get_token_issuer(), get_settings(), get_apple_verifier())


def get_audit(session: DbSession) -> AuditService:
    return AuditService(session)


def get_wallet_service(session: DbSession) -> WalletService:
    return WalletService(session, AuditService(session))


def get_byok_service(session: DbSession) -> BYOKService:
    # ADR-033: BYOK validates the key of the ACTIVE provider via the LLMClient factory.
    return BYOKService(session, get_kms_client(), get_llm_client(), AuditService(session))


def get_subscription_service(session: DbSession) -> SubscriptionService:
    return SubscriptionService(
        session,
        get_storekit_verifier(),
        WalletService(session, AuditService(session)),
        AuditService(session),
    )


def get_token_purchase_service(session: DbSession) -> TokenPurchaseService:
    return TokenPurchaseService(
        session,
        get_storekit_verifier(),
        WalletService(session, AuditService(session)),
    )


def get_adapty_webhook_service(session: DbSession) -> AdaptyWebhookService:
    audit = AuditService(session)
    return AdaptyWebhookService(
        session,
        WalletService(session, audit),
        audit,
        get_settings(),
    )


def get_cloudpayments_verify_client() -> CloudPaymentsVerifyClient:
    # ADR-054: outgoing broadapps payment-verification GET — no DbSession (no persisted state);
    # needs only settings (api_base / api_token).
    return CloudPaymentsVerifyClient(get_settings())


def get_cloudpayments_webhook_service(session: DbSession) -> CloudPaymentsWebhookService:
    audit = AuditService(session)
    return CloudPaymentsWebhookService(
        session,
        WalletService(session, audit),
        audit,
        get_settings(),
        get_cloudpayments_verify_client(),
    )


def get_cloudpayments_checkout_client() -> CloudPaymentsCheckoutClient:
    # ADR-051: passthrough outgoing call to broadapps — no DbSession (no persisted state); needs
    # only settings (api_base / app_id / api_token).
    return CloudPaymentsCheckoutClient(get_settings())


def get_fal_client() -> FalClient:
    # ADR-060: outgoing fal.ai queue calls — no DbSession (no persisted state); needs only
    # settings (api key / queue base / timeout).
    return FalClient(get_settings())


def require_media_generation_configured() -> None:
    """Gate generation routes on the instance having a fal key (ADR-060 §5).

    Generation is opt-in per instance. Without ``FAL_API_KEY``, models/jobs/uploads/submit answer
    ``503 media_generation_not_configured``. Gallery templates (``/v1/media/templates/*``, ADR-066)
    intentionally sit on a separate router without this gate — the tile catalog does not call fal.
    """
    if not get_fal_client().configured:
        raise MediaGenerationNotConfiguredError("media generation is not configured")


def get_request_log_writer(session: DbSession) -> RequestLogWriter:
    if session.bind is None:  # pragma: no cover - wiring invariant
        raise RuntimeError("request DB session is not bound")
    maker = async_sessionmaker(
        bind=session.bind,
        expire_on_commit=False,
        autoflush=False,
    )
    return RequestLogWriter(maker)


def get_apns_client() -> ApnsClient:
    return ApnsClient(get_settings())


def get_notifications_service(session: DbSession) -> NotificationsService:
    return NotificationsService(DevicePushTokensRepository(session))


def get_media_push_service(session: DbSession) -> MediaPushService:
    return MediaPushService(session, apns=get_apns_client())


@lru_cache(maxsize=1)
def get_moderation_service() -> ModerationService:
    """Единственный экземпляр клиента модерации на процесс (ADR-086).

    Кэшируется как прочие исходящие клиенты: HTTP-пул и настройки не зависят от запроса, а
    пересоздание клиента на каждый вызов стоило бы нового соединения на горячем пути генерации.
    """
    return ModerationService(settings=get_settings())


def get_documents_service(session: DbSession) -> DocumentsService:
    """Документы чата (ADR-090) на той же request-scoped сессии, что и остальной домен."""
    return DocumentsService(session, get_settings())


get_documents_service_dep = Annotated[DocumentsService, Depends(get_documents_service)]


def get_media_generation_service(
    session: DbSession,
    request_logs: Annotated[RequestLogWriter, Depends(get_request_log_writer)],
) -> MediaGenerationService:
    # ADR-060: the wallet debit and the media_jobs insert must land in ONE transaction, so the
    # wallet service is built on the same request-scoped session as the repository.
    # ADR-067: push notifier shares that session so push_sent_at lands with mark_completed.
    return MediaGenerationService(
        repo=MediaJobsRepository(session),
        fal=get_fal_client(),
        wallet=WalletService(session, AuditService(session)),
        settings=get_settings(),
        push=get_media_push_service(session),
        request_logs=request_logs,
        moderation=get_moderation_service(),
    )


def get_media_templates_service(session: DbSession) -> MediaTemplatesService:
    # ADR-066: gallery templates catalog — independent of fal; same request-scoped session.
    return MediaTemplatesService(
        repo=MediaTemplatesRepository(session),
        settings=get_settings(),
    )


def get_admin_service(session: DbSession) -> AdminService:
    audit = AuditService(session)
    return AdminService(session, WalletService(session, audit), audit)


def get_crm_admin_service(session: DbSession) -> CrmAdminService:
    audit = AuditService(session)
    wallet = WalletService(session, audit)
    admin = AdminService(session, wallet, audit)
    return CrmAdminService(session, wallet, audit, admin)


def get_chats_service(session: DbSession) -> ChatsService:
    # ADR-038: chats depends on the workspaces service (read-only owns_workspace) to validate the
    # target workspace when PATCH /v1/chats/{id} re-binds a chat to a workspace.
    return ChatsService(
        ChatsRepository(session),
        WorkspacesService(WorkspacesRepository(session)),
    )


def get_profile_service(session: DbSession) -> ProfileService:
    return ProfileService(session)


def get_preferences_service(session: DbSession) -> PreferencesService:
    return PreferencesService(session)


def get_workspaces_service(session: DbSession) -> WorkspacesService:
    return WorkspacesService(WorkspacesRepository(session))


def get_memory_service(session: DbSession) -> MemoryService:
    return MemoryService(
        session,
        workspaces=WorkspacesService(WorkspacesRepository(session)),
    )


def get_orchestrator(session: DbSession) -> ChatOrchestrator:
    audit = AuditService(session)
    website = WebsiteService(session)
    return ChatOrchestrator(
        session=session,
        repo=ChatRepository(session),
        wallet=WalletService(session, audit),
        byok=BYOKService(session, get_kms_client(), get_llm_client(), audit),
        audit=audit,
        # ADR-033 / ADR-082: inject Completions on ordinary legacy; Responses when this
        # instance opted into hosted web search on `/v1/chat/run` (Chat Completions
        # ignores generation_mode, so research would otherwise be billed and never attached).
        anthropic_client=(
            get_generation_llm_client()
            if get_settings().chat_legacy_web_search_enabled
            else get_llm_client()
        ),
        site_tools=SiteToolHandlers(session, website, audit),
        # ADR-026 / ADR-068: global server-side tools (time.now, media.generate_*) with SystemClock
        # and the request-scoped MediaGenerationService (same wallet/session as /v1/media/*).
        global_tools=GlobalToolHandlers(
            clock=SystemClock(),
            media=get_media_generation_service(session, get_request_log_writer(session)),
            documents=get_documents_service(session),
        ),
        preferences=PreferencesService(session),
        # ADR-036: workspace context provider (instructions + knowledge files) for workspace chats.
        workspaces=WorkspacesService(WorkspacesRepository(session)),
        memory=get_memory_service(session),
        # ADR-086 §3: модерация хода с вложениями — до записи шага и до вызова LLM.
        moderation=get_moderation_service(),
        # ADR-090: document.* + строка о документах в системном промте.
        documents=get_documents_service(session),
    )


def get_v2_orchestrator(session: DbSession) -> ChatOrchestrator:
    """Chat orchestrator wired with the `/v1/chat/v2/*` generation client.

    The repository, wallet, BYOK and tool services are the same as the legacy dependency. Only the
    injected LLM client changes, which keeps the new Responses/reasoning/search behavior behind the
    v2 routes.
    """
    audit = AuditService(session)
    website = WebsiteService(session)
    return ChatOrchestrator(
        session=session,
        repo=ChatRepository(session),
        wallet=WalletService(session, audit),
        byok=BYOKService(session, get_kms_client(), get_llm_client(), audit),
        audit=audit,
        anthropic_client=get_generation_llm_client(),
        site_tools=SiteToolHandlers(session, website, audit),
        global_tools=GlobalToolHandlers(
            clock=SystemClock(),
            media=get_media_generation_service(session, get_request_log_writer(session)),
            documents=get_documents_service(session),
        ),
        preferences=PreferencesService(session),
        workspaces=WorkspacesService(WorkspacesRepository(session)),
        memory=get_memory_service(session),
        # ADR-086 §3: модерация хода с вложениями — до записи шага и до вызова LLM.
        moderation=get_moderation_service(),
        # ADR-090: document.* + строка о документах в системном промте.
        documents=get_documents_service(session),
    )


def _is_trusted_proxy(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in network for network in get_settings().trusted_proxy_networks())


def client_ip(request: Request) -> str | None:
    """Resolve the real client IP, respecting a trusted reverse-proxy chain.

    The API runs behind a reverse-proxy / LB (07-deployment.md), so the socket peer is the
    proxy, not the client. We only honour X-Forwarded-For / X-Real-IP when the immediate peer
    is a configured trusted proxy; otherwise the headers are attacker-controlled and ignored.
    From a trusted XFF chain we take the (hop_count + 1)-th entry from the right — the last
    address inserted by infrastructure we do NOT control — never the spoofable left-most one.
    """
    peer = request.client.host if request.client is not None else None
    if peer is None or not _is_trusted_proxy(peer):
        # Request did not arrive via a trusted proxy: do not trust forwarding headers.
        return peer

    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        hops = [h.strip() for h in forwarded_for.split(",") if h.strip()]
        if hops:
            hop_count = max(get_settings().trusted_proxy_hop_count, 1)
            # The chain is: client, proxy1, ..., proxyN(=peer). Trust the rightmost
            # `hop_count` entries (our infra) and take the next one as the client.
            index = len(hops) - hop_count - 1
            if index < 0:
                index = 0
            return hops[index]

    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()
    return peer
