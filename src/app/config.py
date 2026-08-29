"""Application configuration from environment (pydantic-settings).

All secrets and tunables come from env / secret manager (05-security.md, 07-deployment.md).
No magic numbers in business code: limits and grant size are config-driven (ADR-006).
"""

from __future__ import annotations

import ipaddress
from functools import lru_cache
from typing import Any, Literal

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_IpNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network

# Default payment-freshness window (hours) for CloudPayments verification/reconciliation (ADR-054
# §Окно свежести). A non-positive CLOUDPAYMENTS_PAYMENT_FRESHNESS_HOURS falls back to this.
_CLOUDPAYMENTS_DEFAULT_FRESHNESS_HOURS = 72


def _dedup_nonempty(*values: str) -> tuple[str, ...]:
    """Non-empty values in listing order, without duplicates (ADR-074 key chain).

    Order is the failover order, so a set is not applicable. Secrets are compared only
    to each other and are never logged.
    """
    seen: list[str] = []
    for value in values:
        stripped = value.strip()
        if stripped and stripped not in seen:
            seen.append(stripped)
    return tuple(seen)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    # --- Storage ---
    database_url: str = Field(
        default="postgresql+asyncpg://postgres:postgres@localhost:5432/claude_ios",
        alias="DATABASE_URL",
    )
    redis_url: str = Field(default="redis://localhost:6379/0", alias="REDIS_URL")

    # --- LLM provider selection (ADR-033, dual-credits ADR-073) ---
    # Default provider for credits when the client does not pick a model. Default "anthropic" →
    # existing instances are unchanged; "openai" activates OpenAI as that default. Dual-provider
    # credits (both keys, both catalogs) is OPT-IN via LLM_PROVIDERS (empty = single provider).
    llm_provider: str = Field(default="anthropic", alias="LLM_PROVIDER")
    # CSV of extra credits providers besides LLM_PROVIDER, e.g. "openai,anthropic". Unset/empty →
    # only LLM_PROVIDER (ADR-033 compat). A named extra provider is served only when its API key
    # is non-empty. Public, not a secret. Per-instance.
    llm_providers_raw: str = Field(default="", alias="LLM_PROVIDERS")

    # --- Model allowlist per provider (ADR-034 / ADR-076) ---
    # JSON object {model-id: displayName}. Parsed by allowed_models() with the SAME shape rules as
    # token_products() (str→non-empty-str only). Built-in product catalog (ADR-076) is always
    # included for the provider; this env map ADDS extras and may override display names — it does
    # not hide built-in rows. Instance default is always present. Per-provider: allowed_models()
    # reads only the active provider's raw; dual-credits catalog_models() (ADR-073) unions
    # credits_providers(). Not secrets.
    anthropic_models_raw: str = Field(default="{}", alias="ANTHROPIC_MODELS")
    openai_models_raw: str = Field(default="{}", alias="OPENAI_MODELS")

    # --- OpenAI (ADR-033; used when LLM_PROVIDER=openai, or as extra credits via LLM_PROVIDERS) ---
    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    # ADR-074: spare OpenAI key. Canonical name matches 232; OPEN_AI_BACK_UP_API_KEY is accepted.
    openai_api_key_backup: str = Field(
        default="",
        alias="OPENAI_API_KEY_BACKUP",
        validation_alias=AliasChoices("OPENAI_API_KEY_BACKUP", "OPEN_AI_BACK_UP_API_KEY"),
    )
    openai_model: str = Field(default="gpt-4.1", alias="OPENAI_MODEL")
    # Output budget per call (parity with ANTHROPIC_MAX_TOKENS=16000).
    openai_max_tokens: int = Field(default=16000, alias="OPENAI_MAX_TOKENS")
    openai_timeout_seconds: float = Field(default=120.0, alias="OPENAI_TIMEOUT_SECONDS")
    openai_max_retries: int = Field(default=2, alias="OPENAI_MAX_RETRIES")
    # BYOK active model reported when keyStatus=valid on an OpenAI instance (ADR-016/ADR-033 §7).
    openai_byok_default_model: str = Field(default="gpt-4.1", alias="OPENAI_BYOK_DEFAULT_MODEL")

    # --- Anthropic ---
    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")
    # ADR-074: spare Anthropic key. Canonical name matches 232; ANTHROPIC_FALLBACK_API_KEY accepted.
    anthropic_api_key_backup: str = Field(
        default="",
        alias="ANTHROPIC_API_KEY_BACKUP",
        validation_alias=AliasChoices("ANTHROPIC_API_KEY_BACKUP", "ANTHROPIC_FALLBACK_API_KEY"),
    )
    # OpenAI model used when a Claude request fails over to OpenAI (empty → no Anthropic→OpenAI).
    anthropic_chat_fallback_openai_model: str = Field(
        default="", alias="ANTHROPIC_CHAT_FALLBACK_OPENAI_MODEL"
    )
    # Anthropic model used when both OpenAI keys are dead (empty → no OpenAI→Anthropic).
    openai_chat_fallback_anthropic_model: str = Field(
        default="", alias="OPENAI_CHAT_FALLBACK_ANTHROPIC_MODEL"
    )
    anthropic_model: str = Field(default="claude-sonnet-4-5", alias="ANTHROPIC_MODEL")
    # ADR-025: output budget per call. Raised 4096→16000 so code/file generation (several
    # files.write with full content) is not truncated by max_tokens. Stays non-streaming; 16000
    # is below the SDK non-streaming guard. Per-instance in .env (applied to every deploy instance).
    anthropic_max_tokens: int = Field(default=16000, alias="ANTHROPIC_MAX_TOKENS")
    # ADR-025: raised 60→120 to avoid a false 502 timeout on a long non-streaming generation at
    # max_tokens=16000. Configurable; still well below the SDK non-streaming guard.
    anthropic_timeout_seconds: float = Field(default=120.0, alias="ANTHROPIC_TIMEOUT_SECONDS")
    anthropic_max_retries: int = Field(default=2, alias="ANTHROPIC_MAX_RETRIES")
    # ADR-016: active model reported in BYOK responses when keyStatus=valid. Defaults to a
    # current Claude model; configurable via env. Not a secret (model name).
    byok_default_model: str = Field(default="claude-sonnet-4-6", alias="BYOK_DEFAULT_MODEL")

    # --- JWT (RS256, 05-security.md, Q-005-1 default own issuer) ---
    jwt_jwks_url: str = Field(default="", alias="JWT_JWKS_URL")
    jwt_issuer: str = Field(default="", alias="JWT_ISSUER")
    jwt_audience: str = Field(default="", alias="JWT_AUDIENCE")
    # Optional static public key (PEM) fallback when JWKS endpoint is not configured.
    jwt_public_key: str = Field(default="", alias="JWT_PUBLIC_KEY")
    jwks_cache_ttl_seconds: int = Field(default=300, alias="JWT_JWKS_CACHE_TTL")

    # --- Embedded auth-issuer (ADR-018, modules/auth) ---
    # Private signing key (RS256). SECRET: never in repo/image/logs (redaction). Provided as a
    # PEM file path (preferred in prod: mounted secret) or as a PEM string with \n-escaping in
    # env. Path takes priority. Absent => issuer endpoints return 503 (verify-only still works).
    jwt_private_key: str = Field(default="", alias="JWT_PRIVATE_KEY")
    jwt_private_key_path: str = Field(default="", alias="JWT_PRIVATE_KEY_PATH")
    # Public key file path (alongside the existing PEM-string JWT_PUBLIC_KEY; path takes priority).
    jwt_public_key_path: str = Field(default="", alias="JWT_PUBLIC_KEY_PATH")
    # Key id placed in the JWT header / JWKS (key rotation groundwork, not MVP).
    jwt_kid: str = Field(default="", alias="JWT_KID")
    # Access-token TTL 1h, refresh-token TTL 30d (ADR-018 §5).
    auth_access_ttl_seconds: int = Field(default=3600, alias="AUTH_ACCESS_TTL_SECONDS")
    auth_refresh_ttl_seconds: int = Field(default=2592000, alias="AUTH_REFRESH_TTL_SECONDS")
    # Per-IP rate limit on /v1/auth/* (anti-abuse mass registration).
    auth_rate_limit_per_ip: int = Field(default=10, alias="AUTH_RATE_LIMIT_PER_IP")
    # Toggle GET /v1/auth/jwks (public, non-secret). Default true.
    auth_jwks_enabled: bool = Field(default=True, alias="AUTH_JWKS_ENABLED")

    # --- KMS (envelope encryption, ADR-003, Q-002-1) ---
    kms_key_id: str = Field(default="", alias="KMS_KEY_ID")
    # Local fallback master key (base64, 32 bytes) for non-cloud envs; prod uses real KMS.
    kms_local_master_key: str = Field(default="", alias="KMS_LOCAL_MASTER_KEY")

    # --- App Store (Q-007-1) ---
    appstore_environment: str = Field(default="sandbox", alias="APPSTORE_ENVIRONMENT")
    appstore_bundle_id: str = Field(default="", alias="APPSTORE_BUNDLE_ID")
    appstore_root_cert_dir: str = Field(default="", alias="APPSTORE_ROOT_CERT_DIR")
    # DEV/TEST ONLY: skip Apple x5c chain anchoring for real StoreKit JWS.
    # The JWS signature is still verified with the embedded leaf certificate, but without
    # anchoring that leaf to an Apple root this is not proof of Apple issuance. The verifier
    # ignores this flag when APPSTORE_ENVIRONMENT=production.
    storekit_dev_skip_cert_chain_verification: bool = Field(
        default=False, alias="STOREKIT_DEV_SKIP_CERT_CHAIN_VERIFICATION"
    )

    # --- Sign in with Apple (ADR-043, modules/auth Phase 6) ---
    # Apple OIDC identity-token verification for POST /v1/auth/apple. Native Sign in with Apple
    # only (aud = app bundle id); Services ID / web-flow is out of scope (Q-043-1). Values are
    # env (not secrets except APPLE_TEST_SECRET) and per-instance, like APPSTORE_BUNDLE_ID.
    apple_oidc_issuer: str = Field(default="https://appleid.apple.com", alias="APPLE_OIDC_ISSUER")
    apple_jwks_url: str = Field(
        default="https://appleid.apple.com/auth/keys", alias="APPLE_JWKS_URL"
    )
    # Expected `aud` = app bundle id. Empty => fall back to APPSTORE_BUNDLE_ID
    # (apple_audience_resolved()); both empty => Apple sign-in "not configured" => 503.
    apple_audience: str = Field(default="", alias="APPLE_AUDIENCE")
    # test-mode (ADR-043 §2): env-gated HS256 identity tokens for hermetic tests (no Apple infra).
    # Default false => prod fail-closed RS256 verification is unchanged. Active ONLY when
    # apple_test_mode is true AND apple_test_secret is non-empty; HS256 outside test-mode => 401
    # (no alg-confusion). The secret is redaction-allowlisted (`*secret*`) and never logged.
    apple_test_mode: bool = Field(default=False, alias="APPLE_TEST_MODE")
    apple_test_secret: str = Field(default="", alias="APPLE_TEST_SECRET")

    # --- StoreKit test-mode (TD-007, 09-e2e-testing.md §2; test/CI only) ---
    # Env-gated HS256 test transactions for e2e (no Apple infra). Default false => prod
    # fail-closed real JWS verification is unchanged. Active ONLY when storekit_test_mode is
    # true AND storekit_test_secret is non-empty. The secret is redaction-allowlisted and
    # never logged (05-security.md).
    storekit_test_mode: bool = Field(default=False, alias="STOREKIT_TEST_MODE")
    storekit_test_secret: str = Field(default="", alias="STOREKIT_TEST_SECRET")

    # --- Billing (ADR-006) ---
    subscription_credits_per_period: int = Field(
        default=1000, alias="SUBSCRIPTION_CREDITS_PER_PERIOD"
    )
    # Turn-level generation-mode prices for the existing wallet credit system. These values do
    # NOT mint a separate currency: they only replace the old fixed chat debit amount (1) when a
    # final assistant_message is produced. BYOK remains unbilled by the internal wallet.
    chat_credit_cost_general: int = Field(default=1, alias="CHAT_CREDIT_COST_GENERAL")
    chat_credit_cost_research: int = Field(default=3, alias="CHAT_CREDIT_COST_RESEARCH")
    chat_credit_cost_reasoning: int = Field(default=3, alias="CHAT_CREDIT_COST_REASONING")
    # ADR-064 §9: study_learn sits between general (1) and research/reasoning (3) — the turn
    # deterministically makes >=2 upstream calls and produces a bulky pool, but uses neither hosted
    # web search nor a thinking budget. MUST stay inside _positive_chat_credit_cost below (a 0 or
    # negative env would silently make the mode free: the balance gate passes, the debit takes 0).
    chat_credit_cost_study_learn: int = Field(default=2, alias="CHAT_CREDIT_COST_STUDY_LEARN")
    # ADR-065 §1: per-instance allowlist of generation modes this instance ADVERTISES in
    # GET /v1/chat/v2/capabilities. Same shape of knob as ANTHROPIC_MODELS/OPENAI_MODELS (ADR-034):
    # env controls what the CATALOG shows, never what the backend can do. Empty (the default) means
    # «not configured» → the fail-closed default set, see advertised_generation_modes().
    chat_advertised_generation_modes_raw: str = Field(
        default="", alias="CHAT_ADVERTISED_GENERATION_MODES"
    )
    # Server-side defaults for the single public "reasoning" mode. The app exposes only the mode;
    # these knobs let operators tune provider cost/quality without changing the mobile contract.
    chat_reasoning_level: str = Field(default="medium", alias="CHAT_REASONING_LEVEL")
    anthropic_thinking_budget_tokens: int = Field(
        default=4096, alias="ANTHROPIC_THINKING_BUDGET_TOKENS"
    )
    anthropic_thinking_display: str = Field(default="omitted", alias="ANTHROPIC_THINKING_DISPLAY")
    anthropic_web_search_tool_type: str = Field(
        default="web_search_20260318", alias="ANTHROPIC_WEB_SEARCH_TOOL_TYPE"
    )

    # --- Adapty subscription webhook (ADR-029, billing-adapty/07) ---
    # Isolated static bearer secret for POST /v1/billing/adapty/webhook. Set by the operator in
    # the Adapty UI; compared constant-time (hmac.compare_digest). Separate from JWT / admin /
    # KMS / preview secrets and per-instance (ADR-017). Empty (default) => the endpoint returns
    # 500 (misconfiguration); a blank secret never authenticates any presented token.
    adapty_webhook_secret: str = Field(default="", alias="ADAPTY_WEBHOOK_SECRET")
    # JSON object vendor_product_id -> tokens. Source of truth for the per-product grant tier on
    # subscription_started/renewed. Parsed by adapty_product_tokens() (same shape as
    # token_products()). Malformed/non-object => {} => every product falls back to the fixed grant.
    adapty_product_tokens_raw: str = Field(default="{}", alias="ADAPTY_PRODUCT_TOKENS")
    # Fixed fallback grant (tokens) used when vendor_product_id is absent from the tier map.
    # Isolated from SUBSCRIPTION_CREDITS_PER_PERIOD so the Adapty path is calibrated independently
    # (ADR-029 §5); defaults coincide (1000) for predictability.
    adapty_subscription_tokens_grant: int = Field(
        default=1000, alias="ADAPTY_SUBSCRIPTION_TOKENS_GRANT"
    )

    # --- CloudPayments (broadapps/YooKassa) RU webhook (ADR-050, billing-cloudpayments/07) ---
    # Isolated static bearer secret for POST /v1/billing/cloudpayments/webhook (on avelyra = the
    # broadapps app API key). Compared constant-time (hmac.compare_digest); separate from JWT /
    # admin / Adapty / KMS / preview secrets and per-instance (ADR-017). Empty (default) => the
    # endpoint returns 500 (misconfiguration) so it is active only where the secret is set.
    cloudpayments_webhook_token: str = Field(default="", alias="CLOUDPAYMENTS_WEBHOOK_TOKEN")
    # JSON object productId -> tokens: per-tier credits granted on a subscription payment. Parsed
    # by cloudpayments_product_tokens() (same shape as token_products()). Malformed/non-object =>
    # {} => every subscription falls back to the fixed grant below.
    cloudpayments_product_tokens_raw: str = Field(
        default="{}", alias="CLOUDPAYMENTS_PRODUCT_TOKENS"
    )
    # Fixed fallback grant (tokens) for a subscription product absent from the per-tier map above.
    # Isolated from SUBSCRIPTION_CREDITS_PER_PERIOD / the Adapty path so the RU path is calibrated
    # independently (ADR-050 §3a).
    cloudpayments_subscription_tokens_grant: int = Field(
        default=1000, alias="CLOUDPAYMENTS_SUBSCRIPTION_TOKENS_GRANT"
    )
    # --- CloudPayments webhook payment verification (ADR-054) ---
    # broadapps sends the callback WITHOUT auth/signature, so it is only a TRIGGER: the endpoint
    # verifies the payment via the broadapps API (GET /users/{deviceId}/payments) with our
    # CLOUDPAYMENTS_API_TOKEN before crediting. These three tune that reconciliation.
    #
    # Set of broadapps `status` values counted as "paid" (CSV or JSON array; compared lower-case).
    # Default "succeeded" (the real broadapps value); parsed by cloudpayments_paid_statuses().
    # Malformed / empty => {"succeeded"}. The actual status is logged each reconcile (Q-054-1).
    cloudpayments_paid_statuses_raw: str = Field(
        default="succeeded", alias="CLOUDPAYMENTS_PAID_STATUSES"
    )
    # Freshness window (hours): only payments with paid_at >= now() - window are creditable, so the
    # first callback for a user with pre-existing history does not credit the whole back-catalogue
    # at once (ADR-054 §Окно свежести). Reference is now() (not the manipulable paid_at); a
    # non-positive value falls back to the default (see the field_validator below).
    cloudpayments_payment_freshness_hours: int = Field(
        default=_CLOUDPAYMENTS_DEFAULT_FRESHNESS_HOURS,
        alias="CLOUDPAYMENTS_PAYMENT_FRESHNESS_HOURS",
    )
    # Per-source-IP rate limit on the PUBLIC webhook (ADR-054 §1): generous so legitimate
    # callbacks/retries are never throttled; its job is anti-amplification of the outgoing GET.
    cloudpayments_webhook_rate_limit_per_ip: int = Field(
        default=120, alias="CLOUDPAYMENTS_WEBHOOK_RATE_LIMIT_PER_IP"
    )

    # --- CloudPayments (broadapps) RU checkout / payment-link (ADR-051) ---
    # Outgoing call to broadapps POST {base}/payments/link that creates a YooKassa payment link.
    # api_base is PUBLIC (not a secret): the fixed upstream host (no SSRF — never taken from the
    # client body). app_id is the broadapps application UUID (server-side, not in the client).
    cloudpayments_api_base: str = Field(
        default="https://pay.broadapps.dev/api/v1", alias="CLOUDPAYMENTS_API_BASE"
    )
    cloudpayments_app_id: str = Field(default="", alias="CLOUDPAYMENTS_APP_ID")
    # SECRET: outgoing Bearer WE present to broadapps. Semantically distinct from
    # CLOUDPAYMENTS_WEBHOOK_TOKEN (which broadapps presents to US) even if the value currently
    # coincides — separate config allows independent rotation of each side. Empty (default) =>
    # the /checkout endpoint returns 503 (not configured) so it is active only where set (avelyra).
    cloudpayments_api_token: str = Field(default="", alias="CLOUDPAYMENTS_API_TOKEN")

    # --- Token purchase (ADR-015, token-purchase/03) ---
    # Server-side mapping consumable productId -> credits (JSON object). Source of truth for
    # how many credits a token-package purchase grants; never taken from the client body
    # (BR-TP-1 anti-tamper). Example: {"tokens_1500":1500,"tokens_600":600,"tokens_250":250,
    # "tokens_100":100}. Empty default => no products configured (every purchase 422 until set).
    token_products_raw: str = Field(default="{}", alias="TOKEN_PRODUCTS")

    # Optional STATIC display catalog for GET /v1/tokens/products (subs + tokens with
    # title/price/currency). Display-only — crediting still uses TOKEN_PRODUCTS. JSON array of
    # objects; each must have a string `productId`. Empty/malformed => endpoint falls back to the
    # TOKEN_PRODUCTS-derived {productId, credits} list. Not a secret.
    products_catalog_raw: str = Field(default="[]", alias="PRODUCTS_CATALOG")

    # --- Image/video generation via fal.ai (ADR-060, media-generation/03) ---
    # SECRET: the fal API key, presented upstream as `Authorization: Key <value>`. Empty (default)
    # => the whole /v1/media/* surface answers 503 media_generation_not_configured, so the feature
    # is opt-in per instance. Never logged (redaction covers *key* fields).
    fal_api_key: str = Field(default="", alias="FAL_API_KEY")
    # ADR-072: when False, chat does NOT offer media.ask_params / media.generate_* (and refuses
    # mediaSelection), while /v1/media/* still works if FAL_API_KEY is set. Default True keeps
    # prior behaviour on every instance. Per-instance (e.g. ravelumi: REST gallery only).
    chat_media_tools_enabled: bool = Field(default=True, alias="CHAT_MEDIA_TOOLS_ENABLED")
    # ADR-081: comma-separated tool families to hide on THIS instance only
    # (`files`, `calendar`, `reminders`, `site`). Empty (default) = offer the full set —
    # other instances keep files/calendar/reminders/site. Unknown tokens are ignored + WARNING.
    chat_disabled_tool_families_raw: str = Field(default="", alias="CHAT_DISABLED_TOOL_FAMILIES")
    # ADR-094: инструменты работы с кодом (files.delete/move/search/patch, git.*) — помощник по
    # коду в духе Codex. Выключены по умолчанию НАМЕРЕННО: они правят и удаляют файлы на машине
    # человека и пишут в его репозиторий, а исполняет их КЛИЕНТ. На инстансе, чьё приложение
    # таких вызовов не умеет, модель звала бы их впустую и ход оставался бы незавершённым.
    # Включать только там, где клиент их реализовал.
    code_tools_enabled: bool = Field(default=False, alias="CODE_TOOLS_ENABLED")
    # ADR-082: when True, legacy `/v1/chat/run` (and `/tool-result`) attach hosted web search
    # by treating the turn as `research` (price = CHAT_CREDIT_COST_RESEARCH). Default False —
    # every other instance keeps 1-credit general chat. Per-instance (orvianix / ravionet).
    # Does not change `/v1/chat/v2/*` (the client already sends generationMode there).
    chat_legacy_web_search_enabled: bool = Field(
        default=False, alias="CHAT_LEGACY_WEB_SEARCH_ENABLED"
    )
    # PUBLIC upstream host of the fal QUEUE API (async submit/poll; the sync fal.run host cannot
    # serve minute-long video runs). Fixed server-side — never taken from a request body — and
    # also the allowlist for the polling URLs fal returns (SSRF guard in FalClient).
    fal_queue_base: str = Field(default="https://queue.fal.run", alias="FAL_QUEUE_BASE")
    # Connect+read timeout for one fal HTTP call. This bounds submit/status/result calls only, not
    # the generation itself (that runs in fal's queue and is polled).
    fal_timeout_seconds: float = Field(default=30.0, alias="FAL_TIMEOUT_SECONDS")
    # Optional per-model credit price override (JSON object modelId->credits), e.g.
    # {"veo-3.1":400,"nano-banana-2":3}. Unlisted models fall back to the catalog default.
    # Server-side only: the price is NEVER taken from the request body (anti-tamper, cf. BR-TP-1).
    media_model_credits_raw: str = Field(default="{}", alias="MEDIA_MODEL_CREDITS")
    # Cap on how many jobs GET /v1/media/jobs returns in one page.
    media_jobs_page_limit: int = Field(default=50, alias="MEDIA_JOBS_PAGE_LIMIT")
    # How long fal keeps a generated asset (ADR-061 §5). fal's own default is "at least 7 days",
    # after which the CDN URL we hand to the client dies for good. Empty => send no preference and
    # inherit that default; "0" => no expiration; a positive integer => that many seconds.
    # Not a limit we enforce — it is a preference sent upstream with each submit.
    fal_asset_retention_seconds_raw: str = Field(default="", alias="FAL_ASSET_RETENTION_SECONDS")

    # --- Reference-image upload for image-to-image / image-to-video (ADR-062) ---
    # PUBLIC host of the fal REST API used to obtain an upload slot. Fixed server-side.
    fal_rest_base: str = Field(default="https://rest.fal.ai", alias="FAL_REST_BASE")
    # Host suffixes we are willing to PUT a user's file to, and to hand back as an asset URL.
    # `initiate` answers with an upload URL on a CDN/bucket host, NOT on FAL_REST_BASE, so the
    # prefix check that guards status_url/response_url cannot be used here (ADR-062 §4). Comma-
    # separated; a URL outside the list is an upstream error, never a request we go ahead and make.
    fal_upload_host_suffixes_raw: str = Field(
        default=".fal.ai,.fal.media,.fal.run,.storage.googleapis.com",
        alias="FAL_UPLOAD_HOST_SUFFIXES",
    )
    # Per-file decoded-byte ceiling for POST /v1/media/uploads. Larger than the chat image cap
    # (5 MB) on purpose: a chat image is fed to a model and paid for in tokens, a reference image
    # decides the quality of a generation the user already paid credits for.
    media_upload_max_bytes: int = Field(default=10 * 1024 * 1024, alias="MEDIA_UPLOAD_MAX_BYTES")
    # Raised transport body limit applied ONLY to POST /v1/media/uploads (ADR-045 pattern).
    # INVARIANT (single source of truth = MEDIA_UPLOAD_MAX_BYTES, this limit is derived):
    #   media_upload_request_body_limit >= ceil(media_upload_max_bytes * 4/3) + JSON_OVERHEAD
    # 10 MB of base64 is ~13.34 MB; 16 MB leaves ~2.66 MB for the JSON envelope. Must stay >= the
    # invariant under any operator calibration.
    media_upload_request_body_limit: int = Field(
        default=16 * 1024 * 1024, alias="MEDIA_UPLOAD_REQUEST_BODY_LIMIT"
    )
    # Decoded-byte ceiling for gallery template covers (ADR-066 admin create).
    media_template_cover_max_bytes: int = Field(
        default=2 * 1024 * 1024, alias="MEDIA_TEMPLATE_COVER_MAX_BYTES"
    )
    # Raised transport body limit for POST /v1/admin/media/templates only (cover base64).
    media_template_cover_request_body_limit: int = Field(
        default=4 * 1024 * 1024, alias="MEDIA_TEMPLATE_COVER_REQUEST_BODY_LIMIT"
    )
    # Background reconciler for non-terminal media jobs (ADR-067 / Q-060-2). Interval <= 0
    # disables the loop (tests). Batch caps how many fal polls run per tick.
    media_reconcile_interval_seconds: float = Field(
        default=15.0, alias="MEDIA_RECONCILE_INTERVAL_SECONDS"
    )
    media_reconcile_batch_size: int = Field(default=50, alias="MEDIA_RECONCILE_BATCH_SIZE")
    # TTL of the HMAC token in GET /v1/media/jobs/{id}/assets/{index}/{token} (ADR-085).
    # After expiry the client re-polls the job and gets a fresh URL. Secret is PREVIEW_URL_SECRET.
    media_download_ttl_seconds: int = Field(default=86400, alias="MEDIA_DOWNLOAD_TTL_SECONDS")

    # --- APNs push (ADR-067 / TD-011) ---
    # Empty credentials => device-token CRUD still works; send is a no-op (warning logged).
    # SECRET: AuthKey_*.p8 contents (\\n-escaped) or path via APNS_AUTH_KEY_PATH.
    apns_key_id: str = Field(default="", alias="APNS_KEY_ID")
    apns_team_id: str = Field(default="", alias="APNS_TEAM_ID")
    apns_auth_key: str = Field(default="", alias="APNS_AUTH_KEY")
    apns_auth_key_path: str = Field(default="", alias="APNS_AUTH_KEY_PATH")
    # Bundle id / APNs topic (per-instance).
    apns_topic: str = Field(default="", alias="APNS_TOPIC")
    # sandbox (default) | production — host selection only; not a secret.
    apns_environment: str = Field(default="sandbox", alias="APNS_ENVIRONMENT")
    apns_timeout_seconds: float = Field(default=10.0, alias="APNS_TIMEOUT_SECONDS")

    # --- Admin auth (ADR-009, ADM-1) ---
    # Isolated admin secret (X-Admin-Token). High-entropy (>= 32 bytes), only via secret
    # manager / env, never in code/repo/image. Not shared with JWT/KMS/ANTHROPIC/PREVIEW
    # secrets. ADMIN_API_SECRET_PREV is the previous secret kept valid during rotation
    # (grace period); both compared constant-time. Empty (unset) secrets never match.
    admin_api_secret: str = Field(default="", alias="ADMIN_API_SECRET")
    admin_api_secret_prev: str = Field(default="", alias="ADMIN_API_SECRET_PREV")
    # CRM/broad-crm alias for the same admin secret (X-Admin-Key header). If set, accepted
    # alongside ADMIN_API_SECRET; typically only one is configured per instance.
    admin_api_key: str = Field(default="", alias="ADMIN_API_KEY")
    admin_rate_limit_per_min: int = Field(default=10, alias="ADMIN_RATE_LIMIT_PER_MIN")
    # Body size limit for admin endpoints (<= 8 KB, ADR-009 §6).
    admin_size_limit_body: int = Field(default=8 * 1024, alias="ADMIN_SIZE_LIMIT_BODY")

    # --- Website builder / preview (ADR-010, ADR-011, WB-2) ---
    # Isolated HMAC secret for signed preview URLs. Separate from JWT/KMS/ADMIN secrets.
    preview_url_secret: str = Field(default="", alias="PREVIEW_URL_SECRET")
    preview_url_ttl_seconds: int = Field(default=900, alias="PREVIEW_URL_TTL_SECONDS")
    preview_max_file_bytes: int = Field(default=1024 * 1024, alias="PREVIEW_MAX_FILE_BYTES")
    preview_max_project_bytes: int = Field(
        default=10 * 1024 * 1024, alias="PREVIEW_MAX_PROJECT_BYTES"
    )
    preview_max_files: int = Field(default=200, alias="PREVIEW_MAX_FILES")
    # Guard against an infinite server-side tool loop (ADR-011 §2).
    max_server_tool_rounds: int = Field(default=16, alias="MAX_SERVER_TOOL_ROUNDS")
    # PUBLIC service host (not a secret; already in Traefik Host labels and .env.prod.example,
    # ADR-017). Read here only to build the ABSOLUTE site.preview URL so the model copies it
    # verbatim instead of hallucinating a host (ADR-031). Empty => relative fallback (dev).
    service_domain: str = Field(default="", alias="SERVICE_DOMAIN")

    # --- Trusted reverse-proxy (X-Forwarded-For parsing, 07-deployment.md) ---
    # API runs behind a reverse-proxy / LB (TLS termination). Only trust XFF/X-Real-IP
    # when the peer is a known proxy; otherwise the header is spoofable. Empty list =>
    # never trust forwarding headers, always use the socket peer (safe default).
    trusted_proxy_ips: str = Field(default="", alias="TRUSTED_PROXY_IPS")
    # Number of trusted proxy hops in front of the app (chained LB/CDN). The client IP is
    # taken (hop_count + 1) entries from the right of X-Forwarded-For. Default 1.
    trusted_proxy_hop_count: int = Field(default=1, alias="TRUSTED_PROXY_HOP_COUNT")

    # --- Rate limits (Q-003-1 defaults, TD-004) ---
    rate_limit_chat_per_user: int = Field(default=30, alias="RATE_LIMIT_CHAT_PER_USER")
    rate_limit_chat_per_device: int = Field(default=60, alias="RATE_LIMIT_CHAT_PER_DEVICE")
    rate_limit_chat_per_ip: int = Field(default=120, alias="RATE_LIMIT_CHAT_PER_IP")
    rate_limit_other_per_user: int = Field(default=60, alias="RATE_LIMIT_OTHER_PER_USER")
    rate_limit_window_seconds: int = Field(default=60, alias="RATE_LIMIT_WINDOW_SECONDS")

    # --- Size limits in bytes (Q-003-2 defaults, TD-004) ---
    size_limit_body: int = Field(default=512 * 1024, alias="SIZE_LIMIT_BODY")
    # ADR-089 §2: сколько байт тела дочитать и отбросить перед отдачей 413, чтобы клиент успел
    # дописать запрос и ПРОЧИТАТЬ ответ. Без drain закрытие сокета на середине аплоада и есть тот
    # broken pipe, который приложение показывает как «нет связи». Бюджет ограничен: безлимитный
    # drain отменял бы смысл самого лимита.
    size_limit_drain_bytes: int = Field(default=1024 * 1024, alias="SIZE_LIMIT_DRAIN_BYTES")
    size_limit_message: int = Field(default=32 * 1024, alias="SIZE_LIMIT_MESSAGE")
    size_limit_context: int = Field(default=64 * 1024, alias="SIZE_LIMIT_CONTEXT")
    size_limit_tool_result: int = Field(default=256 * 1024, alias="SIZE_LIMIT_TOOL_RESULT")
    size_limit_api_key: int = Field(default=4 * 1024, alias="SIZE_LIMIT_API_KEY")

    # --- Inline multimodal attachments (ADR-020, 05-security.md, Q-020-2 defaults) ---
    # Inline base64 attachments are accepted only in the first user message-step of
    # /v1/chat/run. All limits are enforced BEFORE base64 decoding to bound memory use
    # (decoded size ≈ 3/4 of the base64 length). The mediaType allowlist is fixed in code
    # (schemas/chat.py, Q-020-1 governs extension), not env-driven.
    attachment_max_count: int = Field(default=10, alias="ATTACHMENT_MAX_COUNT")
    # Per-attachment decoded-byte ceiling, split by class: image vs document (PDF).
    attachment_max_bytes_image: int = Field(
        default=20 * 1024 * 1024, alias="ATTACHMENT_MAX_BYTES_IMAGE"
    )
    attachment_max_bytes_document: int = Field(
        default=8 * 1024 * 1024, alias="ATTACHMENT_MAX_BYTES_DOCUMENT"
    )
    # Combined decoded-byte ceiling across all attachments in a request.
    attachment_total_bytes: int = Field(default=60 * 1024 * 1024, alias="ATTACHMENT_TOTAL_BYTES")
    # PDF page-count guard (anti decompression/structure bomb) via pypdf.
    attachment_pdf_max_pages: int = Field(default=100, alias="ATTACHMENT_PDF_MAX_PAGES")
    # Raised transport body limit applied ONLY to the /v1/chat/run route (other routes keep
    # size_limit_body). Inline base64 of large files exceeds the general ≤512KB cap.
    #
    # ИНВАРИАНТ (симметричен паре WORKSPACE_FILE_MAX_BYTES <-> WORKSPACE_REQUEST_BODY_LIMIT):
    #   attachment_request_body_limit >= ceil(attachment_total_bytes * 4/3) + JSON_OVERHEAD
    # Вложения едут в JSON как base64, который раздувает объём на треть, поэтому транспортный
    # лимит ограничивает сумму вложений РАНЬШЕ, чем attachment_total_bytes: при теле 12 MiB
    # заявленная сумма в 10 MiB была недостижима (10 MiB -> 13.3 MiB base64 -> 413), и клиент
    # получал 413 вместо внятного 422 attachments_total_too_large. Держать соотношение при
    # любой калибровке оператором.
    attachment_request_body_limit: int = Field(
        default=80 * 1024 * 1024, alias="ATTACHMENT_REQUEST_BODY_LIMIT"
    )

    # --- Workspaces (рабочие пространства) knowledge files (ADR-036 §4/§6) ---
    # Limits for workspace_files (own BYTEA table; ADR-036 §4, TD-027). All defaults are the
    # values fixed in ADR-036 (08 MB per file = the document-cap; 32 MB total per workspace; 20
    # files per workspace). WORKSPACE_CONTEXT_MAX_CHARS bounds the total injected extracted_text
    # (ADR-036 §6) — images are bounded by file count/size, not by this char limit.
    workspace_file_max_count: int = Field(default=20, alias="WORKSPACE_FILE_MAX_COUNT")
    workspace_file_max_bytes: int = Field(default=8 * 1024 * 1024, alias="WORKSPACE_FILE_MAX_BYTES")
    workspace_files_total_bytes: int = Field(
        default=32 * 1024 * 1024, alias="WORKSPACE_FILES_TOTAL_BYTES"
    )
    workspace_context_max_chars: int = Field(default=200_000, alias="WORKSPACE_CONTEXT_MAX_CHARS")
    # Raised transport body limit applied ONLY to the workspace files-upload route
    # (POST /v1/workspaces/{id}/files) — other routes keep size_limit_body (ADR-045).
    # INVARIANT (single source of truth = WORKSPACE_FILE_MAX_BYTES, this limit is derived):
    #   workspace_request_body_limit >= ceil(workspace_file_max_bytes * 4/3) + JSON_OVERHEAD
    # where *4/3 is the base64 inflation of an 8 MB file (≈10.67 MB) and JSON_OVERHEAD is the
    # JSON-envelope slack ({"type","mediaType","filename","data":"..."}, escaping, field headers;
    # recommended >=256 KB). Default 12 MB satisfies it: 10.67 MB + ~1.33 MB slack > 256 KB. Must
    # stay >= the invariant under any operator calibration (TD-004), symmetric to the
    # ATTACHMENT_MAX_BYTES_DOCUMENT <-> ATTACHMENT_REQUEST_BODY_LIMIT relation for /v1/chat/run.
    workspace_request_body_limit: int = Field(
        default=12 * 1024 * 1024, alias="WORKSPACE_REQUEST_BODY_LIMIT"
    )

    # --- Документы чата (ADR-090) -----------------------------------------------------------
    # Текстовые документы, которые модель создаёт/правит, а клиент скачивает. Скоуп — сессия:
    # удаляются вместе с чатом (ON DELETE CASCADE), поэтому потолки заданы на сессию, а не на
    # пользователя.
    document_max_bytes: int = Field(default=256 * 1024, alias="DOCUMENT_MAX_BYTES")
    document_max_count: int = Field(default=20, alias="DOCUMENT_MAX_COUNT")
    document_total_bytes: int = Field(default=1024 * 1024, alias="DOCUMENT_TOTAL_BYTES")
    # Срез СПИСКА документов (id+имя) в системном промте. Содержимое туда не попадает никогда —
    # оно большое и меняется; для чтения есть document.read (ADR-090 §6).
    document_context_max_chars: int = Field(default=2000, alias="DOCUMENT_CONTEXT_MAX_CHARS")

    # --- Модерация UGC (ADR-086) ---------------------------------------------------------
    # Пре-модерация промптов/референсов/вложений и пост-модерация результата фото-генерации
    # через OpenAI omni-moderation. Fail-closed по умолчанию (§7): цена пропуска непроверенного
    # UGC несимметрична цене временной недоступности двух поверхностей.
    moderation_enabled: bool = Field(default=True, alias="MODERATION_ENABLED")
    # SECRET. Пусто → фолбэк на OPENAI_API_KEY. На anthropic-инстансах обязателен явно.
    moderation_api_key: str = Field(default="", alias="MODERATION_API_KEY")
    moderation_model: str = Field(default="omni-moderation-latest", alias="MODERATION_MODEL")
    # Фиксированный хост исходящего вызова (SSRF-guard): берётся из конфига, не из запроса.
    moderation_base_url: str = Field(
        default="https://api.openai.com/v1", alias="MODERATION_BASE_URL"
    )
    moderation_timeout_seconds: float = Field(default=10.0, alias="MODERATION_TIMEOUT_SECONDS")
    moderation_max_retries: int = Field(default=1, alias="MODERATION_MAX_RETRIES")
    moderation_block_categories_raw: str = Field(
        default="sexual,sexual/minors,violence/graphic,self-harm,self-harm/intent,"
        "self-harm/instructions",
        alias="MODERATION_BLOCK_CATEGORIES",
    )
    moderation_text_max_chars: int = Field(default=4000, alias="MODERATION_TEXT_MAX_CHARS")
    # Аварийный переключатель оператора (§7). true = осознанное снижение соответствия сторам.
    moderation_fail_open: bool = Field(default=False, alias="MODERATION_FAIL_OPEN")

    # --- Cross-chat RAG memory ---
    memory_enabled: bool = Field(default=True, alias="MEMORY_ENABLED")
    memory_embedding_fake: bool = Field(default=False, alias="MEMORY_EMBEDDING_FAKE")
    memory_embedding_model: str = Field(
        default="text-embedding-3-small", alias="MEMORY_EMBEDDING_MODEL"
    )
    memory_embedding_dimensions: int = Field(default=1536, alias="MEMORY_EMBEDDING_DIMENSIONS")
    memory_chunk_max_chars: int = Field(default=1500, alias="MEMORY_CHUNK_MAX_CHARS")
    memory_chunk_overlap_chars: int = Field(default=200, alias="MEMORY_CHUNK_OVERLAP_CHARS")
    memory_search_top_k: int = Field(default=8, alias="MEMORY_SEARCH_TOP_K")
    memory_retrieval_max_chars: int = Field(default=8000, alias="MEMORY_RETRIEVAL_MAX_CHARS")
    memory_explicit_max_chars: int = Field(default=4000, alias="MEMORY_EXPLICIT_MAX_CHARS")
    memory_explicit_entry_max_chars: int = Field(
        default=4000, alias="MEMORY_EXPLICIT_ENTRY_MAX_CHARS"
    )
    memory_hybrid_vector_weight: float = Field(default=0.7, alias="MEMORY_HYBRID_VECTOR_WEIGHT")

    # --- DB connection pool (02-tech-stack.md, sized for ~10k users / 2-3 replicas) ---
    # Per-process pool. Effective max conns ≈ (pool_size + max_overflow) * workers * replicas;
    # keep below Postgres max_connections. architect documents the sizing math in docs.
    db_pool_size: int = Field(default=10, alias="DB_POOL_SIZE")
    db_max_overflow: int = Field(default=5, alias="DB_MAX_OVERFLOW")
    db_pool_timeout: float = Field(default=30.0, alias="DB_POOL_TIMEOUT")
    db_pool_recycle: int = Field(default=1800, alias="DB_POOL_RECYCLE")

    # --- Session (Q-001-1) ---
    session_soft_ttl_seconds: int = Field(default=24 * 3600, alias="SESSION_SOFT_TTL_SECONDS")

    # --- Wallet ---
    wallet_last_transactions: int = Field(default=20, alias="WALLET_LAST_TRANSACTIONS")

    # --- Policy cache ---
    policy_cache_ttl_seconds: int = Field(default=5, alias="POLICY_CACHE_TTL_SECONDS")

    # --- API documentation (08-api-documentation.md, R7) ---
    # Toggles /docs, /redoc, /openapi.json. Default true (dev/CI/staging). Recommended
    # false in prod so the API surface is not publicly exposed (05-security.md).
    docs_enabled: bool = Field(default=True, alias="DOCS_ENABLED")

    # --- Prompt presets localization (ADR-049) ---
    # Per-instance default locale for GET /v1/presets (avelyra=ru, others=en). Public, not a
    # secret (ADR-017). Default "en" = current behavior (unset env → EN, backward-compatible).
    # A value outside SUPPORTED_PRESET_LOCALES degrades gracefully to "en" (+ WARNING log), never
    # a startup crash — read via resolved_presets_default_locale(), not the raw field.
    presets_default_locale: str = Field(default="en", alias="PRESETS_DEFAULT_LOCALE")

    # --- Observability ---
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    otel_exporter_otlp_endpoint: str = Field(default="", alias="OTEL_EXPORTER_OTLP_ENDPOINT")
    metrics_scrape_token: str = Field(default="", alias="METRICS_SCRAPE_TOKEN")

    def token_products(self) -> dict[str, int]:
        """Parse TOKEN_PRODUCTS (JSON object productId->credits) into a validated mapping.

        Only string keys with positive-int credit values survive (ADR-015, BR-TP-1). A
        malformed JSON document or non-object yields an empty mapping (every purchase then
        fails 422), never a partial/ambiguous credit table. Pure (no I/O); cached via
        get_settings()'s lru_cache for the process lifetime.
        """
        import json

        try:
            parsed = json.loads(self.token_products_raw or "{}")
        except (ValueError, json.JSONDecodeError):
            return {}
        if not isinstance(parsed, dict):
            return {}
        products: dict[str, int] = {}
        for key, value in parsed.items():
            if not isinstance(key, str):
                continue
            # bool is a subclass of int; exclude it explicitly to avoid True->1 surprises.
            if isinstance(value, bool) or not isinstance(value, int):
                continue
            if value <= 0:
                continue
            products[key] = value
        return products

    def media_model_credits(self) -> dict[str, int]:
        """Parse MEDIA_MODEL_CREDITS (JSON object modelId->credits) into a validated mapping.

        An override table for the per-model generation price (ADR-060 §4); models absent here keep
        their catalog default. Only string keys with positive-int values survive, so a typo can
        never make a model free — a malformed document degrades to "no overrides", not to zero
        prices. Pure (no I/O); cached via get_settings()'s lru_cache.
        """
        import json

        try:
            parsed = json.loads(self.media_model_credits_raw or "{}")
        except (ValueError, json.JSONDecodeError):
            return {}
        if not isinstance(parsed, dict):
            return {}
        credits: dict[str, int] = {}
        for key, value in parsed.items():
            if not isinstance(key, str):
                continue
            # bool is a subclass of int; exclude it explicitly to avoid True->1 surprises.
            if isinstance(value, bool) or not isinstance(value, int):
                continue
            if value <= 0:
                continue
            credits[key] = value
        return credits

    def fal_asset_retention(self) -> int | None | Literal[False]:
        """Parse FAL_ASSET_RETENTION_SECONDS into the lifecycle preference sent to fal (ADR-061 §5).

        Three outcomes, because the upstream contract has three: ``False`` — send no preference at
        all (fal's own "at least 7 days" applies); ``None`` — no expiration; a positive int — that
        many seconds. Anything unparseable or negative degrades to ``False``, i.e. to fal's
        default: a typo must not silently pin assets forever, nor delete them early. Pure (no I/O).
        """
        raw = self.fal_asset_retention_seconds_raw.strip()
        if not raw:
            return False
        try:
            seconds = int(raw)
        except ValueError:
            return False
        if seconds == 0:
            return None
        return seconds if seconds > 0 else False

    def fal_upload_host_suffixes(self) -> tuple[str, ...]:
        """Host suffixes an upload URL from fal may live on (ADR-062 §4).

        Empty/blank entries are dropped and comparison is done lowercase, so operator formatting
        (spaces, trailing comma, mixed case) cannot silently widen or empty the allowlist. An empty
        result means "trust nothing", which fails closed — we would rather not upload than PUT a
        user's file to a host an upstream response named. Pure (no I/O).
        """
        parts = (item.strip().lower() for item in self.fal_upload_host_suffixes_raw.split(","))
        return tuple(part for part in parts if part)

    def products_catalog(self) -> list[dict[str, Any]]:
        """Parse PRODUCTS_CATALOG (JSON array) into a list of display product dicts.

        Display-only (GET /v1/tokens/products). Keeps only object items with a non-empty string
        `productId`; malformed JSON / non-array / bad items are dropped gracefully (empty list =>
        endpoint falls back to the TOKEN_PRODUCTS-derived catalog). Pure (no I/O).
        """
        import json

        try:
            parsed = json.loads(self.products_catalog_raw or "[]")
        except (ValueError, json.JSONDecodeError):
            return []
        if not isinstance(parsed, list):
            return []
        out: list[dict[str, Any]] = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            pid = item.get("productId")
            if isinstance(pid, str) and pid:
                out.append(item)
        return out

    def adapty_product_tokens(self) -> dict[str, int]:
        """Parse ADAPTY_PRODUCT_TOKENS (JSON object vendor_product_id->tokens) (ADR-029 §5).

        Mirrors token_products(): only string keys with positive-int values survive (bool is a
        subclass of int and is excluded). A malformed JSON document or non-object yields an empty
        mapping, in which case every vendor_product_id falls back to
        adapty_subscription_tokens_grant. Pure (no I/O); cached via get_settings()'s lru_cache.
        """
        import json

        try:
            parsed = json.loads(self.adapty_product_tokens_raw or "{}")
        except (ValueError, json.JSONDecodeError):
            return {}
        if not isinstance(parsed, dict):
            return {}
        products: dict[str, int] = {}
        for key, value in parsed.items():
            if not isinstance(key, str):
                continue
            # bool is a subclass of int; exclude it explicitly to avoid True->1 surprises.
            if isinstance(value, bool) or not isinstance(value, int):
                continue
            if value <= 0:
                continue
            products[key] = value
        return products

    def cloudpayments_product_tokens(self) -> dict[str, int]:
        """Parse CLOUDPAYMENTS_PRODUCT_TOKENS (JSON object productId->credits) (ADR-050 §3a).

        Mirrors token_products()/adapty_product_tokens(): only string keys with positive-int values
        survive (bool is a subclass of int and is excluded). A malformed JSON document or non-object
        yields an empty mapping, in which case every subscription product falls back to
        cloudpayments_subscription_tokens_grant. Pure (no I/O); cached via get_settings()'s cache.
        """
        import json

        try:
            parsed = json.loads(self.cloudpayments_product_tokens_raw or "{}")
        except (ValueError, json.JSONDecodeError):
            return {}
        if not isinstance(parsed, dict):
            return {}
        products: dict[str, int] = {}
        for key, value in parsed.items():
            if not isinstance(key, str):
                continue
            # bool is a subclass of int; exclude it explicitly to avoid True->1 surprises.
            if isinstance(value, bool) or not isinstance(value, int):
                continue
            if value <= 0:
                continue
            products[key] = value
        return products

    @field_validator("cloudpayments_payment_freshness_hours")
    @classmethod
    def _clamp_freshness_hours(cls, value: int) -> int:
        """A non-positive CLOUDPAYMENTS_PAYMENT_FRESHNESS_HOURS falls back to the default (ADR-054).

        The window must be strictly positive (``paid_at >= now() - timedelta(hours=window)``); a
        mis-configured ``0``/negative env degrades to the safe default instead of disabling the
        window, mirroring the graceful config parsing elsewhere (token_products()/allowed_models()).
        """
        return value if value > 0 else _CLOUDPAYMENTS_DEFAULT_FRESHNESS_HOURS

    @field_validator(
        "chat_credit_cost_general",
        "chat_credit_cost_research",
        "chat_credit_cost_reasoning",
        "chat_credit_cost_study_learn",
    )
    @classmethod
    def _positive_chat_credit_cost(cls, value: int) -> int:
        """Generation-mode prices must stay positive so wallet debits are always valid.

        EVERY ``CHAT_CREDIT_COST_*`` field belongs in THIS validator (ADR-064 §9): a price left out
        of it fails silently — a ``0``/negative env raises no start-up error and no block, the
        balance gate passes and the debit takes zero, so the mode quietly becomes free on that
        instance. Adding a new generation-mode price without adding it here is the same class of
        defect as «declared but not wired»: the field exists, the guard is not applied to it.
        """
        return value if value > 0 else 1

    @field_validator("anthropic_thinking_budget_tokens")
    @classmethod
    def _positive_thinking_budget(cls, value: int) -> int:
        """Anthropic extended thinking requires a positive token budget."""
        return value if value > 0 else 4096

    def chat_generation_credit_cost(self, generation_mode: str) -> int:
        """Return the wallet debit amount for one completed assistant turn.

        This is the single bridge between the public chat generation mode
        (``general|research|reasoning|study_learn``) and the existing integer-credit wallet. The
        value is used for the pre-generation balance gate, for the final idempotent debit AND for
        ``creditCost`` in ``GET /v1/chat/v2/capabilities``, so a mode cannot be advertised at one
        price, allowed at another and charged at a third. No second pricing mechanism exists
        (ADR-064 §9). An unknown mode falls back to the ``general`` price.
        """
        normalized = generation_mode.strip().lower()
        if normalized == "research":
            return self.chat_credit_cost_research
        if normalized == "reasoning":
            return self.chat_credit_cost_reasoning
        if normalized == "study_learn":
            return self.chat_credit_cost_study_learn
        return self.chat_credit_cost_general

    def advertised_generation_modes(self) -> tuple[str, ...]:
        """Generation modes this instance ADVERTISES in GET /v1/chat/v2/capabilities (ADR-065 §1).

        This is an ADVERTISEMENT gate, NOT a behaviour gate: ``POST /v1/chat/v2/run`` accepts every
        mode the backend understands on EVERY instance regardless of this list. The list only
        decides which elements appear in ``generationModes[]`` — a mode outside it is ABSENT from
        the array rather than marked ``available: false``, because the hole this closes was opened
        by ALREADY-RELEASED binaries whose handling of that field we do not control.

        Parsing rules (all normative, ADR-065 §1.2-1.5):
        - comma-separated, ``strip().lower()`` per entry;
        - unknown values are IGNORED with a WARNING — never a startup crash (graceful config
          parsing, same as ``resolved_presets_default_locale``/``allowed_models_for``);
        - env unset / blank / entirely invalid → the FAIL-CLOSED default
          ``general,research,reasoning``: the modes that need no dedicated UI. ``study_learn`` is
          NOT advertised by default — an instance whose app can draw the quiz lists it explicitly.
          The asymmetry is deliberate: mis-advertising costs the user 2 debited credits and an
          empty screen, while under-advertising costs only a hidden feature;
        - ``general`` is ALWAYS present, even when a non-empty env omits it, because
          ``defaultGenerationMode`` must exist in the list;
        - the result is in CANONICAL order (the declaration order of ``GenerationMode``), never the
          order the operator typed, so a client may render the list as-is.

        Pure (no I/O beyond logging); cached via ``get_settings()``'s lru_cache, so the WARNING
        fires once per process.
        """
        from app.observability.logging import get_logger
        from app.schemas.chat import (
            DEFAULT_ADVERTISED_GENERATION_MODES,
            DEFAULT_GENERATION_MODE,
            GENERATION_MODE_ORDER,
        )

        selected: set[str] = set()
        unknown: list[str] = []
        for entry in self.chat_advertised_generation_modes_raw.split(","):
            normalized = entry.strip().lower()
            if not normalized:
                continue
            if normalized in GENERATION_MODE_ORDER:
                selected.add(normalized)
            else:
                unknown.append(normalized)
        if unknown:
            get_logger("app.config").warning(
                "CHAT_ADVERTISED_GENERATION_MODES contains unknown mode(s) %r; ignoring them",
                unknown,
            )
        if not selected:
            # Unset, blank, or entirely invalid → fail-closed default (never «everything»).
            selected = set(DEFAULT_ADVERTISED_GENERATION_MODES)
        # defaultGenerationMode must always be offered, otherwise the UI switcher has no default.
        selected.add(DEFAULT_GENERATION_MODE)
        return tuple(mode for mode in GENERATION_MODE_ORDER if mode in selected)

    def resolved_reasoning_level(self) -> str:
        """Provider-safe reasoning effort for the public ``generationMode=reasoning`` mode."""
        level = self.chat_reasoning_level.strip().lower()
        return level if level in {"low", "medium", "high"} else "medium"

    def resolved_anthropic_thinking_display(self) -> str:
        """Provider-safe Anthropic thinking display setting.

        ``omitted`` is the default to avoid returning reasoning summaries to the client by
        accident; operators can opt into ``summarized`` if the product later needs visible
        reasoning summaries.
        """
        display = self.anthropic_thinking_display.strip().lower()
        return display if display in {"omitted", "summarized"} else "omitted"

    def cloudpayments_paid_statuses(self) -> frozenset[str]:
        """Parse CLOUDPAYMENTS_PAID_STATUSES into the set of "paid" broadapps statuses (ADR-054 §4).

        Accepts a JSON array (``["succeeded","paid"]``) OR a CSV (``succeeded,paid``); each entry is
        stripped and lower-cased (the reconciliation compares ``status.strip().lower()``). A
        malformed / empty value yields ``{"succeeded"}`` (the authoritative broadapps "paid" value)
        so the gate is never accidentally emptied. Pure; cached via get_settings()'s lru_cache.
        """
        import json

        raw = (self.cloudpayments_paid_statuses_raw or "").strip()
        if not raw:
            return frozenset({"succeeded"})
        statuses: set[str] = set()
        if raw.startswith("["):
            try:
                parsed = json.loads(raw)
            except (ValueError, json.JSONDecodeError):
                parsed = None
            if isinstance(parsed, list):
                for item in parsed:
                    if isinstance(item, str) and item.strip():
                        statuses.add(item.strip().lower())
        else:
            for part in raw.split(","):
                token = part.strip().lower()
                if token:
                    statuses.add(token)
        return frozenset(statuses) if statuses else frozenset({"succeeded"})

    def cloudpayments_checkout_configured(self) -> bool:
        """True when the RU checkout endpoint is configured on this instance (ADR-051 §5).

        Requires BOTH the broadapps application id and the outgoing API token; either empty =>
        POST /v1/billing/cloudpayments/checkout returns 503 (feature not available here). Active
        only on the instance where the operator sets both (avelyra).
        """
        return bool(self.cloudpayments_app_id and self.cloudpayments_api_token)

    def default_model(self) -> str:
        """Active instance default model (ADR-034 §1): the model used when none is selected.

        Provider-aware: ``openai_model`` when ``LLM_PROVIDER=openai``, otherwise ``anthropic_model``
        (the default). This is the model the active client falls back to
        (``settings.<provider>_model``) when ``create_message(model=None)`` — so it is, by
        construction, ALWAYS present in
        ``allowed_models()`` (the empty-allowlist fallback returns exactly this model; a non-empty
        allowlist without it has it prepended at the API layer — GET /v1/models).
        Dual-credits (ADR-073) does not change this: ``default:true`` stays the
        LLM_PROVIDER default.
        """
        if self._normalized_llm_provider() == "openai":
            return self.openai_model
        return self.anthropic_model

    def _normalized_llm_provider(self) -> str:
        """Canonical credits default: ``openai`` or ``anthropic`` (anything else → anthropic)."""
        provider = self.llm_provider.strip().lower()
        return "openai" if provider == "openai" else "anthropic"

    def _credits_api_key_configured(self, provider: str) -> bool:
        if provider == "openai":
            return bool(self.openai_api_key.strip())
        return bool(self.anthropic_api_key.strip())

    def openai_api_key_chain(self) -> tuple[str, ...]:
        """OpenAI keys in failover order: primary, then backup (ADR-074).

        Empty values are dropped (an empty key would 401 and waste an attempt). A duplicate
        backup equal to the primary is also dropped so the same key is not tried twice.
        """
        return _dedup_nonempty(self.openai_api_key, self.openai_api_key_backup)

    def anthropic_api_key_chain(self) -> tuple[str, ...]:
        """Anthropic keys in failover order — mirror of ``openai_api_key_chain()``."""
        return _dedup_nonempty(self.anthropic_api_key, self.anthropic_api_key_backup)

    def credits_providers(self) -> tuple[str, ...]:
        """Providers that may serve credits chats on this instance (ADR-073).

        Always includes ``LLM_PROVIDER`` first (the instance default). Extra names from
        ``LLM_PROVIDERS`` (CSV) are appended when they are ``openai``/``anthropic``, distinct from
        the default, and have a non-empty API key. Unset/empty ``LLM_PROVIDERS`` → a 1-tuple of
        the default provider — identical to ADR-033 single-provider instances.
        """
        active = self._normalized_llm_provider()
        extras: list[str] = []
        for part in self.llm_providers_raw.split(","):
            provider = part.strip().lower()
            if provider not in ("openai", "anthropic") or provider == active:
                continue
            if not self._credits_api_key_configured(provider):
                continue
            if provider not in extras:
                extras.append(provider)
        return (active, *extras)

    def allowed_models_union(self) -> dict[str, str]:
        """Selectable credits model ids across ``credits_providers()`` (ADR-073).

        First provider wins on id collision. On a single-provider instance this equals
        ``allowed_models()``.
        """
        merged: dict[str, str] = {}
        for provider in self.credits_providers():
            for model_id, display_name in self.allowed_models_for(provider).items():
                if model_id not in merged:
                    merged[model_id] = display_name
        return merged

    def credits_provider_for_model(self, model: str | None) -> str:
        """Credits provider that should serve ``model`` (ADR-073).

        ``None`` (session uses the instance default) → ``LLM_PROVIDER``. A known id is matched
        against enabled providers in ``credits_providers()`` order. Unknown / stale ids (e.g. a
        Claude session after dual-credits was turned off) fall back to ``LLM_PROVIDER`` so the
        existing stale-model guard can send ``model=None`` instead of 502.
        """
        active = self._normalized_llm_provider()
        if model is None:
            return active
        for provider in self.credits_providers():
            if model in self.allowed_models_for(provider):
                return provider
        return active

    def catalog_models(self) -> list[tuple[str, str, bool, str]]:
        """GET /v1/models rows: ``(id, displayName, default, provider)`` (ADR-034 + ADR-073).

        Default model (``default_model()``) is first and the only ``default=True``. Remaining ids
        follow each enabled provider's allowlist insertion order, ``credits_providers()`` order,
        without duplicates. Single-provider instances emit the same ids/order as today, plus the
        additive ``provider`` column.
        """
        default_id = self.default_model()
        providers = self.credits_providers()
        active = providers[0]
        active_map = self.allowed_models_for(active)
        default_display = active_map.get(default_id, default_id)
        rows: list[tuple[str, str, bool, str]] = [(default_id, default_display, True, active)]
        seen = {default_id}
        for provider in providers:
            for model_id, display_name in self.allowed_models_for(provider).items():
                if model_id in seen:
                    continue
                seen.add(model_id)
                rows.append((model_id, display_name, False, provider))
        return rows

    def moderation_api_key_resolved(self) -> str:
        """Ключ модерации: явный MODERATION_API_KEY, иначе фолбэк на OPENAI_API_KEY (ADR-086 §11).

        Пусто ⇒ на инстансе с MODERATION_ENABLED=true модерируемые поверхности отдают
        503 moderation_not_configured (проблема оператора, не пользователя).
        """
        explicit = self.moderation_api_key.strip()
        return explicit or self.openai_api_key.strip()

    def moderation_block_categories(self) -> frozenset[str]:
        """BLOCK-набор категорий (ADR-086 §6).

        ``sexual/minors`` входит ВСЕГДА, даже если оператор удалил её из env — это не
        настраиваемая политика.
        """
        parsed = {
            item.strip().lower()
            for item in self.moderation_block_categories_raw.split(",")
            if item.strip()
        }
        parsed.add("sexual/minors")
        return frozenset(parsed)

    def byok_default_model_for(self, provider: str) -> str:
        """BYOK default model for a SPECIFIC provider (ADR-044 §5/§6, ADR-016).

        ``"openai"`` → ``openai_byok_default_model``; any non-openai value (incl. ``"anthropic"``) →
        ``byok_default_model``. This is the model reported as ``activeModel`` when keyStatus=valid
        and the model used for BYOK generation when the session model is absent / belongs to another
        provider (ADR-044 §5.3). Provider-aware, independent of ``LLM_PROVIDER``.
        """
        if provider.strip().lower() == "openai":
            return self.openai_byok_default_model
        return self.byok_default_model

    def allowed_models(self) -> dict[str, str]:
        """Active provider's model allowlist as a validated {id: displayName} mapping (ADR-034 §1).

        Thin wrapper over :meth:`allowed_models_for` for the ACTIVE provider (``LLM_PROVIDER``,
        default anthropic). Signature and behavior are unchanged — existing callers keep working.
        """
        return self.allowed_models_for(self._normalized_llm_provider())

    def allowed_models_for(self, provider: str) -> dict[str, str]:
        """Selectable models for a provider: built-in catalog ∪ env allowlist (ADR-076).

        Provider-aware (ADR-034 §1, generalized for ADR-044 §5 / ADR-076): reads
        ``openai_models_raw`` for ``"openai"``, else ``anthropic_models_raw``. Same shape rules as
        ``token_products()``: only ``str`` keys with a non-empty ``str`` value survive. Malformed
        JSON or a non-object yields an empty env map (built-in catalog still applies).

        Order: instance default first, then built-in product ids, then extra env ids. Env values
        override display names. The default is ALWAYS present (displayName from env, else built-in,
        else the id). Pure (no I/O); cached via get_settings().
        """
        import json

        from app.chat.product_catalog import product_models_for

        is_openai = provider.strip().lower() == "openai"
        raw = self.openai_models_raw if is_openai else self.anthropic_models_raw
        try:
            parsed = json.loads(raw or "{}")
        except (ValueError, json.JSONDecodeError):
            parsed = {}
        parsed_models: dict[str, str] = {}
        if isinstance(parsed, dict):
            for key, value in parsed.items():
                if not isinstance(key, str):
                    continue
                stripped_key = key.strip()
                if not stripped_key:
                    continue
                # bool is a subclass of int (not str); the isinstance(str) check excludes it.
                if not isinstance(value, str) or not value:
                    continue
                parsed_models[stripped_key] = value
        builtin = product_models_for("openai" if is_openai else "anthropic")
        default = self.openai_model if is_openai else self.anthropic_model
        if default in parsed_models:
            default_name = parsed_models[default]
        elif default in builtin:
            default_name = builtin[default]
        else:
            default_name = default
        merged: dict[str, str] = {default: default_name}
        for model_id, display_name in builtin.items():
            if model_id in merged:
                continue
            merged[model_id] = parsed_models.get(model_id, display_name)
        for model_id, display_name in parsed_models.items():
            if model_id not in merged:
                merged[model_id] = display_name
        return merged

    @staticmethod
    def _resolve_pem(path_value: str, string_value: str) -> str:
        """Resolve a PEM key: file path takes priority over the \\n-escaped string (ADR-018 §7).

        When a path is set it is read from disk verbatim (recommended prod: mounted secret, no
        escaping). Otherwise the env string value has literal ``\\n`` sequences turned into real
        newlines so a single-line .env value yields a valid multi-line PEM. Empty when neither is
        configured. Never logs the key material (redaction covers ``*key*``).
        """
        if path_value:
            with open(path_value, encoding="utf-8") as handle:
                return handle.read()
        if string_value:
            return string_value.replace("\\n", "\n")
        return ""

    def resolve_private_key(self) -> str:
        """Private RS256 signing key PEM, or '' if the issuer is not configured (=> 503)."""
        return self._resolve_pem(self.jwt_private_key_path, self.jwt_private_key)

    def resolve_public_key(self) -> str:
        """Public RS256 verification key PEM (used by JwtVerifier and the JWKS endpoint)."""
        return self._resolve_pem(self.jwt_public_key_path, self.jwt_public_key)

    def resolve_apns_auth_key(self) -> str:
        """APNs AuthKey .p8 PEM, or '' when push send is not configured (ADR-067)."""
        return self._resolve_pem(self.apns_auth_key_path, self.apns_auth_key)

    def apple_audience_resolved(self) -> str:
        """Effective Apple `aud` for verification (ADR-043 §3).

        Returns ``apple_audience`` (stripped) if set, else ``appstore_bundle_id`` (stripped) as a
        fallback (if a bundle id is already configured for StoreKit it doubles as the Apple
        audience), else ``""``. An empty result means Apple sign-in is "not configured" — the
        router returns 503 (operational misconfiguration, not a client error). Pure (no I/O).
        """
        explicit = self.apple_audience.strip()
        if explicit:
            return explicit
        return self.appstore_bundle_id.strip()

    def normalized_service_domain(self) -> str:
        """Return SERVICE_DOMAIN as a bare host[:port] for the absolute preview URL (ADR-031).

        Strips a leading http(s):// scheme (case-insensitive) and surrounding slashes so the
        value is the same host regardless of how it is set (``broadnova.shop``,
        ``https://broadnova.shop`` or ``broadnova.shop/``). Returns '' when unset/blank, which
        the caller treats as "not configured" => relative fallback. Snapping the trailing slash
        guarantees the assembled URL has no double slash before ``/v1/``.
        """
        value = self.service_domain.strip()
        lowered = value.lower()
        if lowered.startswith("https://"):
            value = value[len("https://") :]
        elif lowered.startswith("http://"):
            value = value[len("http://") :]
        value = value.strip("/")
        return value

    def disabled_tool_families(self) -> frozenset[str]:
        """Per-instance tool-family denylist (ADR-081). Empty raw → empty set (full catalog)."""
        from app.chat.tools import parse_disabled_tool_families
        from app.observability.logging import get_logger

        disabled, unknown = parse_disabled_tool_families(self.chat_disabled_tool_families_raw)
        if unknown:
            get_logger("app.config").warning(
                "CHAT_DISABLED_TOOL_FAMILIES has unsupported tokens %s; ignored",
                unknown,
            )
        return disabled

    def resolved_presets_default_locale(self) -> str:
        """Per-instance default locale for GET /v1/presets, validated gracefully (ADR-049 §4).

        Canonicalizes ``presets_default_locale`` (``zh-Hans`` / ``zh_Hans`` / ``RU``) and returns
        the supported form. A value outside the set degrades to ``DEFAULT_PRESET_LOCALE``
        (``"en"``) and logs a WARNING — mis-configured env falls back to a safe default instead of
        crashing the process, mirroring ``token_products()``/``allowed_models_for()`` (ADR-034 §1).
        Pure (no I/O). Cached via get_settings()'s lru_cache; the WARNING fires once per process.
        """
        from app.chat.presets import DEFAULT_PRESET_LOCALE, canonicalize_preset_locale
        from app.observability.logging import get_logger

        resolved = canonicalize_preset_locale(self.presets_default_locale)
        if resolved is not None:
            return resolved
        get_logger("app.config").warning(
            "PRESETS_DEFAULT_LOCALE=%r is not a supported locale; falling back to %r",
            self.presets_default_locale,
            DEFAULT_PRESET_LOCALE,
        )
        return DEFAULT_PRESET_LOCALE

    def trusted_proxy_networks(self) -> tuple[_IpNetwork, ...]:
        """Parse TRUSTED_PROXY_IPS (comma-separated IPs/CIDRs) into networks.

        Invalid entries are skipped. Empty/blank => empty tuple (never trust XFF).
        """
        networks: list[_IpNetwork] = []
        for raw in self.trusted_proxy_ips.split(","):
            entry = raw.strip()
            if not entry:
                continue
            try:
                networks.append(ipaddress.ip_network(entry, strict=False))
            except ValueError:
                continue
        return tuple(networks)


# Content-type allowlist for site_files (ADR-010, website-builder/05-security.md). Only these
# types may be stored and served by the preview endpoint. Fixed on the server (not configurable
# at runtime to keep the threat model deterministic; Q-010-2 leaves the exact list to architect).
PREVIEW_CONTENT_TYPE_ALLOWLIST: frozenset[str] = frozenset(
    {
        "text/html",
        "text/css",
        "text/javascript",
        "application/json",
        "image/png",
        "image/jpeg",
        "image/svg+xml",
        "image/gif",
        "image/webp",
        "font/woff2",
        "text/plain",
    }
)


@lru_cache
def get_settings() -> Settings:
    return Settings()
