"""Unit: ADR-108 — pure parts of generation via the proxy service.

Norm — ``docs/adr/ADR-108-media-generation-via-proxy.md`` (§1 predicate, §2/§2.1 routing, §3.3 id of
the proxy task, §4.1 token, §4.3 classification, §7 allowlist, §10 access-log redaction) and
``docs/modules/media-generation/09-testing.md`` §«Integration — транспорт через прокси (ADR-108)».

Every classification is checked on BOTH sides of its predicate: the routing condition §2.1 gives
a vendor route when every condition holds (a) and only the fal route when any single one is
broken (b) — each breach is its own case.
"""

from __future__ import annotations

import decimal
import hashlib
import hmac
import logging
import uuid
from typing import Any

import pytest

from app.config import Settings
from app.media_generation.catalog import build_fal_input, find_model, resolve_values
from app.media_generation.proxy_client import _first_id
from app.media_generation.routing import (
    KIE_CREATE_TASK,
    SOSANA_CREATE_IMAGE,
    candidate_routes,
    fal_route,
    lookup_price,
    merged_vendor_prices,
)
from app.media_generation.webhook import (
    OUTCOME_COMPLETED,
    OUTCOME_FAILED,
    OUTCOME_PENDING,
    TOKEN_MAX_LENGTH,
    callback_url,
    parse_vendor_price,
    sign_webhook_token,
    verify_webhook_token,
    webhook_outcome,
)

_DOMAIN = "gen.example.test"


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "PROXY_API_KEY": "",
        "PROXY_WEBHOOK_SECRET": "",
        "SERVICE_DOMAIN": "",
        "FAL_API_KEY": "",
        "MEDIA_RESULT_HOST_SUFFIXES": "",
        "MEDIA_VENDOR_PRICES": "{}",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


# ================================ §1 — the predicate ================================


@pytest.mark.parametrize(
    ("proxy_key", "domain", "fal_key", "proxy", "fal", "generation"),
    [
        ("px", _DOMAIN, "", True, False, True),
        # The domain is part of the predicate: without it no callbackUrl can be built.
        ("px", "", "", False, False, False),
        ("px", "", "fk", False, True, True),
        ("", _DOMAIN, "fk", False, True, True),
        ("", "", "", False, False, False),
        # Blank counts as empty on both keys.
        ("   ", _DOMAIN, "  ", False, False, False),
    ],
)
def test_generation_predicate_is_proxy_or_fal(
    proxy_key: str, domain: str, fal_key: str, proxy: bool, fal: bool, generation: bool
) -> None:
    cfg = _settings(PROXY_API_KEY=proxy_key, SERVICE_DOMAIN=domain, FAL_API_KEY=fal_key)
    assert cfg.proxy_configured() is proxy
    assert cfg.fal_configured() is fal
    assert cfg.media_generation_configured() is generation


def test_proxy_defaults_are_the_normative_ones() -> None:
    """ADR-108 §1.1 is the one normative place of the defaults."""
    fields = Settings.model_fields
    assert fields["proxy_base"].default == "https://proxy.broadapps.dev"
    assert fields["proxy_timeout_seconds"].default == 30
    assert fields["proxy_api_key"].default == ""
    assert fields["proxy_webhook_secret"].default == ""
    assert fields["media_result_host_suffixes_raw"].default == ""
    assert fields["media_vendor_prices_raw"].default == "{}"
    assert _settings().media_vendor_prices() == {}
    assert _settings().media_result_host_suffixes() == ()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('{"nano-banana-2:2K:fal": 0.01}', {"nano-banana-2:2K:fal": 0.01}),
        ('{"a:b:fal": -1, "c:d:fal": "x", "e:f:fal": true, "g:h:fal": 2}', {"g:h:fal": 2.0}),
        ("not json", {}),
        ("[1, 2]", {}),
    ],
)
def test_vendor_price_override_keeps_only_non_negative_numbers(
    raw: str, expected: dict[str, float]
) -> None:
    assert _settings(MEDIA_VENDOR_PRICES=raw).media_vendor_prices() == expected


# ============================ §2 / §2.1 — routing ============================


def _run(
    model_id: str, *, with_image: bool = False, **params: Any
) -> tuple[Any, Any, dict[str, Any], dict[str, Any]]:
    model = find_model(model_id)
    assert model is not None
    variant = model.variant_for(with_image=with_image)
    assert variant is not None
    values = resolve_values(variant=variant, values={"prompt": "a cat", **params})
    image_urls = ["https://example.com/a.png"] if with_image else []
    payload = build_fal_input(model=model, variant=variant, values=values, image_urls=image_urls)
    return model, variant, values, payload


def _routes(model_id: str, *, hosts: bool = True, prices: Any = None, **params: Any) -> list[Any]:
    model, variant, values, payload = _run(model_id, **params)
    return candidate_routes(
        model=model,
        variant=variant,
        values=values,
        fal_payload=payload,
        prices=merged_vendor_prices(prices),
        result_hosts_configured=hosts,
    )


@pytest.mark.parametrize("model_id", ["nano-banana-2", "nano-banana-pro"])
@pytest.mark.parametrize("resolution", ["1K", "2K", "4K"])
def test_eligible_banana_run_is_routed_sosana_kie_fal(model_id: str, resolution: str) -> None:
    """(a) every §2.1 condition holds → sosana, kie, fal — cheapest first (default table)."""
    routes = _routes(model_id, resolution=resolution, aspectRatio="16:9")
    assert [r.service for r in routes] == ["sosana", "kie", "fal"]
    assert routes[0].endpoint == SOSANA_CREATE_IMAGE
    assert routes[1].endpoint == KIE_CREATE_TASK
    assert [r.unit_price for r in routes] == sorted(r.unit_price for r in routes)


@pytest.mark.parametrize(
    ("case", "hosts", "params"),
    [
        ("result hosts empty", False, {"resolution": "2K", "aspectRatio": "16:9"}),
        ("numImages=2", True, {"resolution": "2K", "aspectRatio": "16:9", "numImages": 2}),
        ("0.5K", True, {"resolution": "0.5K", "aspectRatio": "16:9"}),
        ("seed", True, {"resolution": "2K", "aspectRatio": "16:9", "seed": 7}),
        (
            "outputFormat=webp",
            True,
            {"resolution": "2K", "aspectRatio": "16:9", "outputFormat": "webp"},
        ),
        ("panoramic 8:1", True, {"resolution": "2K", "aspectRatio": "8:1"}),
        ("aspectRatio omitted", True, {"resolution": "2K"}),
    ],
)
def test_any_single_breach_of_the_condition_leaves_only_fal(
    case: str, hosts: bool, params: dict[str, Any]
) -> None:
    """(b) one condition of §2.1 broken → the run has the single fal route."""
    routes = _routes("nano-banana-2", hosts=hosts, **params)
    assert [r.service for r in routes] == ["fal"], case


@pytest.mark.parametrize("fmt", ["png", "jpeg"])
def test_output_format_png_or_jpeg_keeps_kie_but_drops_sosana(fmt: str) -> None:
    """§2.1 п.4 per service: sosana takes no outputFormat, kie reproduces png/jpeg (jpeg → jpg)."""
    routes = _routes("nano-banana-2", resolution="2K", aspectRatio="1:1", outputFormat=fmt)
    assert [r.service for r in routes] == ["kie", "fal"]
    assert routes[0].payload["input"]["output_format"] == ("jpg" if fmt == "jpeg" else "png")


@pytest.mark.parametrize(
    ("model_id", "params"),
    [
        ("kling-video", {}),
        ("kling-video-v3", {}),
        ("veo-3.1", {}),
    ],
)
def test_video_models_route_only_to_fal(model_id: str, params: dict[str, Any]) -> None:
    routes = _routes(model_id, **params)
    assert [r.service for r in routes] == ["fal"]


@pytest.mark.parametrize(
    ("model_id", "with_image", "params"),
    [
        ("nano-banana-2", False, {"resolution": "2K", "aspectRatio": "16:9", "seed": 3}),
        ("nano-banana-2", True, {}),
        ("nano-banana-pro", False, {"numImages": 2}),
        ("nano-banana-pro", True, {"resolution": "4K"}),
        ("kling-video", False, {"duration": "10"}),
        ("kling-video", True, {}),
        ("kling-video-v3", False, {"generateAudio": True}),
        ("kling-video-v3", True, {}),
        ("veo-3.1", False, {"resolution": "4k", "generateAudio": True}),
        ("veo-3.1", True, {"aspectRatio": "auto"}),
    ],
)
def test_fal_route_carries_exactly_the_direct_fal_payload(
    model_id: str, with_image: bool, params: dict[str, Any]
) -> None:
    """§2: the fal route = ``https://queue.fal.run/<variant endpoint>`` + the direct payload."""
    model, variant, values, payload = _run(model_id, with_image=with_image, **params)
    routes = candidate_routes(
        model=model,
        variant=variant,
        values=values,
        fal_payload=payload,
        prices=merged_vendor_prices(),
        result_hosts_configured=True,
    )
    fal = [r for r in routes if r.service == "fal"]
    assert len(fal) == 1
    assert fal[0].endpoint == f"https://queue.fal.run/{variant.endpoint}"
    assert fal[0].payload == payload
    assert fal[0].catalog_endpoint == variant.endpoint


def test_override_making_fal_cheapest_puts_fal_first() -> None:
    routes = _routes(
        "nano-banana-2",
        resolution="2K",
        aspectRatio="16:9",
        prices={"nano-banana-2:2K:fal": 0.001},
    )
    assert [r.service for r in routes] == ["fal", "sosana", "kie"]


def test_equal_prices_tie_break_sosana_kie_fal() -> None:
    routes = _routes(
        "nano-banana-2",
        resolution="1K",
        aspectRatio="16:9",
        prices={
            "nano-banana-2:1K:sosana": 0.5,
            "nano-banana-2:1K:kie": 0.5,
            "nano-banana-2:1K:fal": 0.5,
        },
    )
    assert [r.service for r in routes] == ["sosana", "kie", "fal"]


def test_price_lookup_falls_back_to_wildcards_and_ignores_negatives() -> None:
    prices = merged_vendor_prices({"*:*:fal": 2.0, "veo-3.1:*:fal": -1.0})
    assert lookup_price(prices, model_id="veo-3.1", tier="*", service="fal") == 2.0
    assert lookup_price(prices, model_id="nano-banana-2", tier="2K", service="fal") == 0.12
    # A feature endpoint (not in the table) still has its fal route.
    route = fal_route(
        model_id="feature",
        tier="*",
        endpoint="fal-ai/imageutils/rembg",
        payload={"image_url": "x"},
        prices=prices,
    )
    assert route.service == "fal"
    assert route.endpoint == "https://queue.fal.run/fal-ai/imageutils/rembg"


# ============================ §3.3 — the proxy task id ============================


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"request_id": "r1"}, "r1"),
        ({"requestId": "r2"}, "r2"),
        ({"id": 42}, "42"),
        ({"uid": "u"}, "u"),
        ({"taskId": "t"}, "t"),
        ({"data": {"taskId": "inner"}}, "inner"),
        ({"status": "queued"}, ""),
        ({"request_id": ""}, ""),
    ],
)
def test_proxy_task_id_is_first_non_empty_or_empty_string(
    body: dict[str, Any], expected: str
) -> None:
    """``""`` when there is no id — not the sample's ``"pending"`` (ADR-108 §3.3)."""
    assert _first_id(body) == expected


# ============================ §4.1 — token and callback URL ============================


def test_token_is_hmac_sha256_hex_of_the_job_id_under_the_dedicated_secret() -> None:
    job_id = uuid.uuid4()
    cfg = _settings(
        PROXY_API_KEY="px-key", PROXY_WEBHOOK_SECRET="wh-secret", SERVICE_DOMAIN=_DOMAIN
    )
    expected = hmac.new(b"wh-secret", str(job_id).encode(), hashlib.sha256).hexdigest()
    assert sign_webhook_token(settings=cfg, job_id=job_id) == expected
    assert callback_url(settings=cfg, job_id=job_id) == (
        f"https://{_DOMAIN}/v1/media/webhooks/proxy/{job_id}?token={expected}"
    )


def test_token_falls_back_to_the_proxy_key_without_a_dedicated_secret() -> None:
    job_id = uuid.uuid4()
    cfg = _settings(PROXY_API_KEY="px-key", SERVICE_DOMAIN=_DOMAIN)
    expected = hmac.new(b"px-key", str(job_id).encode(), hashlib.sha256).hexdigest()
    assert sign_webhook_token(settings=cfg, job_id=job_id) == expected


def test_verify_accepts_only_the_token_of_this_job() -> None:
    cfg = _settings(PROXY_WEBHOOK_SECRET="wh-secret")
    job_id, other = uuid.uuid4(), uuid.uuid4()
    token = sign_webhook_token(settings=cfg, job_id=job_id)
    assert verify_webhook_token(settings=cfg, job_id=job_id, token=token) is True
    assert verify_webhook_token(settings=cfg, job_id=other, token=token) is False
    assert verify_webhook_token(settings=cfg, job_id=job_id, token=None) is False
    assert verify_webhook_token(settings=cfg, job_id=job_id, token="") is False
    too_long = token + "0" * (TOKEN_MAX_LENGTH - len(token) + 1)
    assert verify_webhook_token(settings=cfg, job_id=job_id, token=too_long) is False


def test_empty_secret_never_verifies() -> None:
    """Both secrets empty ⇒ the HMAC of '' would be computable by anyone — never accepted."""
    cfg = _settings()
    job_id = uuid.uuid4()
    forged = hmac.new(b"", str(job_id).encode(), hashlib.sha256).hexdigest()
    assert verify_webhook_token(settings=cfg, job_id=job_id, token=forged) is False


# ============================ §4.3 — classification ============================


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"status": "failed"}, OUTCOME_FAILED),
        ({"status": "CANCELLED"}, OUTCOME_FAILED),
        ({"code": 500}, OUTCOME_FAILED),
        ({"error": True}, OUTCOME_FAILED),
        ({"status": "completed"}, OUTCOME_COMPLETED),
        ({"images": [{"url": "https://v3.fal.media/files/a.png"}]}, OUTCOME_COMPLETED),
        ({"status": "processing"}, OUTCOME_PENDING),
        ({}, OUTCOME_PENDING),
        # A success status wins over an error-shaped code.
        ({"status": "success", "code": 500}, OUTCOME_COMPLETED),
    ],
)
def test_callback_classification(body: dict[str, Any], expected: str) -> None:
    assert webhook_outcome(body) == expected


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"vendor_price": 0.028}, decimal.Decimal("0.028000")),
        ({"data": {"cost": "1.5"}}, decimal.Decimal("1.500000")),
        ({"vendor_price": -1}, None),
        ({"vendor_price": "NaN"}, None),
        ({"vendor_price": 10**13}, None),
        ({"vendor_price": True}, None),
        ({}, None),
    ],
)
def test_vendor_price_parsing(body: dict[str, Any], expected: decimal.Decimal | None) -> None:
    assert parse_vendor_price(body) == expected


# ============================ §7 — result-host allowlist ============================


def test_default_allowlist_is_fal_union_result_hosts_and_fal_client_stays_fal_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import get_settings
    from app.media_generation.asset_hosts import fal_asset_host_allowed
    from app.media_generation.fal_client import FalClient

    vendor_url = "https://cdn.sosana.test/out.png"
    fal_url = "https://v3.fal.media/files/out.png"

    monkeypatch.setenv("MEDIA_RESULT_HOST_SUFFIXES", "")
    get_settings.cache_clear()
    try:
        # (b) empty MEDIA_RESULT_HOST_SUFFIXES ⇒ exactly the pre-ADR-108 behaviour.
        assert fal_asset_host_allowed(vendor_url) is False
        assert fal_asset_host_allowed(fal_url) is True

        monkeypatch.setenv("MEDIA_RESULT_HOST_SUFFIXES", ".sosana.test")
        get_settings.cache_clear()
        # (a) the default list is the union.
        assert fal_asset_host_allowed(vendor_url) is True
        assert fal_asset_host_allowed(fal_url) is True
        assert fal_asset_host_allowed("http://cdn.sosana.test/out.png") is False
        # The fal-only list stays where it is passed explicitly (uploads / rehost / download).
        client = FalClient(get_settings())
        assert client._upload_host_allowed(vendor_url) is False
        assert client._upload_host_allowed(fal_url) is True
    finally:
        get_settings.cache_clear()


# ============================ features gate (§1) ============================


def test_features_need_the_fal_key_even_behind_the_proxy() -> None:
    from app.errors import MediaGenerationNotConfiguredError
    from app.media_generation.fal_client import FalClient
    from app.media_generation.features_service import MediaFeaturesService

    def _service(cfg: Settings) -> MediaFeaturesService:
        svc = MediaFeaturesService.__new__(MediaFeaturesService)
        svc._settings = cfg  # noqa: SLF001
        svc._fal = FalClient(cfg)  # noqa: SLF001
        return svc

    proxy_only = _settings(PROXY_API_KEY="px", SERVICE_DOMAIN=_DOMAIN, MAKEUP_ENABLED="true")
    with pytest.raises(MediaGenerationNotConfiguredError):
        _service(proxy_only)._require_makeup_ready()  # noqa: SLF001

    both = _settings(
        PROXY_API_KEY="px", SERVICE_DOMAIN=_DOMAIN, FAL_API_KEY="fk", MAKEUP_ENABLED="true"
    )
    _service(both)._require_makeup_ready()  # noqa: SLF001 — no exception


# ============================ §10 — access-log redaction ============================


def _access_line(path: str) -> str:
    """Emit one uvicorn access record through the REAL ``uvicorn.access`` logger and capture it."""
    captured: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record.getMessage())

    access = logging.getLogger("uvicorn.access")
    handler = _Capture()
    access.addHandler(handler)
    was_disabled, level = access.disabled, access.level
    access.disabled = False
    access.setLevel(logging.INFO)
    try:
        access.info('%s - "%s %s HTTP/%s" %d', "10.0.0.1:5000", "POST", path, "1.1", 200)
    finally:
        access.removeHandler(handler)
        access.disabled, access.level = was_disabled, level
    assert len(captured) == 1
    return captured[0]


@pytest.fixture
def configured_logging() -> Any:
    """Run the REAL ``configure_logging`` (it wires the filter) and restore the root afterwards."""
    from app.observability.logging import AccessLogQueryRedactionFilter, configure_logging

    access = logging.getLogger("uvicorn.access")
    before_filters = list(access.filters)
    for item in list(access.filters):
        if isinstance(item, AccessLogQueryRedactionFilter):
            access.removeFilter(item)
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    configure_logging("INFO")
    try:
        yield
    finally:
        root.handlers[:] = handlers
        root.setLevel(level)
        access.filters[:] = before_filters


def test_access_line_of_the_callback_carries_no_token(configured_logging: Any) -> None:
    job_id = uuid.uuid4()
    line = _access_line(f"/v1/media/webhooks/proxy/{job_id}?token=deadbeefcafe")
    assert "token=" not in line
    assert "deadbeefcafe" not in line
    assert f"/v1/media/webhooks/proxy/{job_id}" in line
    assert line.endswith(" 200")


def test_access_line_of_any_other_path_keeps_its_query(configured_logging: Any) -> None:
    line = _access_line("/v1/media/jobs?limit=5&kind=image")
    assert "/v1/media/jobs?limit=5&kind=image" in line
    # A lookalike prefix is not the callback path.
    line = _access_line("/v1/media/webhooks/proxyish?token=x")
    assert "token=x" in line
