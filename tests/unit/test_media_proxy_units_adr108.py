"""Unit: ADR-108 — pure parts of generation via the proxy service.

Norm — ``docs/adr/ADR-108-media-generation-via-proxy.md`` (§1 predicate, §2/§2.1 routing, §3.3 id of
the proxy task, §4.1 token, §4.3 classification, §7 allowlist, §10 access-log redaction) and
``docs/modules/media-generation/09-testing.md`` §«Integration — транспорт через прокси (ADR-108)».

Every classification is checked on BOTH sides of its predicate: the fixed routes of §2 give
``[sosana|kie, fal]`` when the vendor route can be built (a) and only the fal route when any
single condition of §2.1 is broken (b) — each breach is its own case.
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
from app.media_generation.proxy_client import ProxyClient, _first_id, mask_secret_text
from app.media_generation.routing import (
    KIE_CREATE_TASK,
    SOSANA_CREATE_IMAGE,
    candidate_routes,
    fal_route,
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
        "KIE_API_KEY": "",
        "SOSANA_API_KEY": "",
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
    assert fields["kie_api_key"].default == ""
    assert fields["sosana_api_key"].default == ""
    # ADR-108 §2: routing is fixed — no vendor price table.
    assert "media_vendor_prices_raw" not in fields
    assert _settings().media_result_host_suffixes() == ()


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


def _routes(model_id: str, *, with_image: bool = False, **params: Any) -> list[Any]:
    model, variant, values, payload = _run(model_id, with_image=with_image, **params)
    return candidate_routes(model=model, variant=variant, values=values, fal_payload=payload)


@pytest.mark.parametrize("model_id", ["nano-banana-2", "nano-banana-pro"])
@pytest.mark.parametrize("resolution", ["1K", "2K", "4K"])
@pytest.mark.parametrize(
    ("extra", "aspect"),
    [
        ({"aspectRatio": "16:9"}, "16:9"),
        ({"aspectRatio": "1:1", "seed": 7}, "1:1"),
        ({"aspectRatio": "9:16", "outputFormat": "webp"}, "9:16"),
        ({"outputFormat": "png"}, "auto"),
        ({}, "auto"),
    ],
    ids=["16:9", "seed", "webp", "png-no-aspect", "no-aspect"],
)
def test_eligible_banana_run_is_routed_sosana_then_fal(
    model_id: str, resolution: str, extra: dict[str, Any], aspect: str
) -> None:
    """(a) §2.1: seed/outputFormat are dropped on sosana, no aspectRatio → the vendor's ``auto``."""
    routes = _routes(model_id, resolution=resolution, **extra)
    assert [r.service for r in routes] == ["sosana", "fal"]
    assert routes[0].endpoint == SOSANA_CREATE_IMAGE
    assert routes[0].payload == {
        "prompt": "a cat",
        "model": f"{model_id}-{resolution.lower()}",
        "aspect_ratio": aspect,
        "prompt_optimization": False,
    }


def test_sosana_edit_run_carries_the_reference_images() -> None:
    routes = _routes("nano-banana-2", with_image=True, resolution="2K")
    assert [r.service for r in routes] == ["sosana", "fal"]
    assert routes[0].payload["image_urls"] == ["https://example.com/a.png"]


@pytest.mark.parametrize(
    ("case", "params"),
    [
        ("numImages=2", {"resolution": "2K", "aspectRatio": "16:9", "numImages": 2}),
        ("0.5K", {"resolution": "0.5K", "aspectRatio": "16:9"}),
        ("panoramic 8:1", {"resolution": "2K", "aspectRatio": "8:1"}),
    ],
)
def test_any_single_breach_of_the_condition_leaves_only_fal(
    case: str, params: dict[str, Any]
) -> None:
    """(b) one condition of §2.1 broken → the run has the single fal route."""
    routes = _routes("nano-banana-2", **params)
    assert [r.service for r in routes] == ["fal"], case


_I2V = "https://example.com/a.png"


@pytest.mark.parametrize(
    ("model_id", "with_image", "params", "expected"),
    [
        (
            "kling-video",
            False,
            {"negativePrompt": "blur", "cfgScale": 0.4},
            {
                "model": "kling/v2-5-turbo-text-to-video-pro",
                "input": {
                    "prompt": "a cat",
                    "duration": "5",
                    "cfg_scale": 0.4,
                    "negative_prompt": "blur",
                },
            },
        ),
        (
            "kling-video",
            True,
            {"duration": "10"},
            {
                "model": "kling/v2-5-turbo-image-to-video-pro",
                "input": {"prompt": "a cat", "duration": "10", "image_url": _I2V},
            },
        ),
        (
            "kling-video-v3",
            False,
            {},
            {
                "model": "kling-3.0/video",
                "input": {
                    "prompt": "a cat",
                    "mode": "pro",
                    "sound": False,
                    "multi_shots": False,
                    "multi_prompt": [],
                    "duration": "5",
                },
            },
        ),
        (
            "kling-video-v3",
            True,
            {"generateAudio": True, "duration": "7"},
            {
                "model": "kling-3.0/video",
                "input": {
                    "prompt": "a cat",
                    "mode": "pro",
                    "sound": True,
                    "multi_shots": False,
                    "multi_prompt": [],
                    "duration": "7",
                    "image_urls": [_I2V],
                },
            },
        ),
        (
            "veo-3.1",
            False,
            {},
            {
                "model": "veo-3-1",
                "input": {
                    "prompt": "a cat",
                    "generation_type": "TEXT_2_VIDEO",
                    "resolution": "720p",
                    "duration": 8,
                },
            },
        ),
        (
            "veo-3.1",
            True,
            {"aspectRatio": "auto"},
            {
                "model": "veo-3-1",
                "input": {
                    "prompt": "a cat",
                    "image_urls": [_I2V],
                    "generation_type": "FIRST_AND_LAST_FRAMES_2_VIDEO",
                    "aspect_ratio": "Auto",
                    "resolution": "720p",
                    "duration": 8,
                },
            },
        ),
    ],
    ids=["kling25-t2v", "kling25-i2v", "kling3-t2v", "kling3-i2v", "veo-t2v", "veo-i2v"],
)
def test_video_run_is_routed_kie_then_fal_with_the_builder_payload(
    model_id: str, with_image: bool, params: dict[str, Any], expected: dict[str, Any]
) -> None:
    routes = _routes(model_id, with_image=with_image, **params)
    assert [r.service for r in routes] == ["kie", "fal"]
    assert routes[0].endpoint == KIE_CREATE_TASK
    assert routes[0].payload == expected


def test_kling25_i2v_negative_prompt_is_clipped_to_500() -> None:
    long_text = ("no blur here. " * 200)[:2000]
    routes = _routes("kling-video", with_image=True, negativePrompt=long_text)
    kie = routes[0].payload["input"]["negative_prompt"]
    assert 0 < len(kie) <= 500
    assert long_text.startswith(kie)
    # fal keeps the full value (its limit is its own).
    assert routes[1].payload["negative_prompt"] == long_text
    # text-to-video keeps up to the vendor's own 2500.
    t2v = _routes("kling-video", negativePrompt=long_text)
    assert t2v[0].payload["input"]["negative_prompt"] == long_text


def test_image_variant_without_a_start_frame_has_only_the_fal_route() -> None:
    from app.media_generation.routing import _kie_route

    model, variant, values, payload = _run("kling-video", with_image=True)
    payload.pop(model.image_field)
    assert _kie_route(model=model, variant=variant, fal_payload=payload) is None


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
def test_fal_route_is_last_and_carries_exactly_the_direct_fal_payload(
    model_id: str, with_image: bool, params: dict[str, Any]
) -> None:
    """§2: the fal route = ``https://queue.fal.run/<variant endpoint>`` + the direct payload."""
    model, variant, values, payload = _run(model_id, with_image=with_image, **params)
    routes = candidate_routes(model=model, variant=variant, values=values, fal_payload=payload)
    fal = routes[-1]
    assert [r.service for r in routes].count("fal") == 1
    assert fal.service == "fal"
    assert fal.endpoint == f"https://queue.fal.run/{variant.endpoint}"
    assert fal.payload == payload
    assert fal.catalog_endpoint == variant.endpoint


def test_feature_endpoint_has_the_fal_route() -> None:
    route = fal_route(endpoint="fal-ai/imageutils/rembg", payload={"image_url": "x"})
    assert route.service == "fal"
    assert route.endpoint == "https://queue.fal.run/fal-ai/imageutils/rembg"
    assert route.catalog_endpoint == "fal-ai/imageutils/rembg"


# ============================ §3.2 — provider key, §10 — masking ============================


class _CapturingProxy(ProxyClient):
    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.bodies: list[dict[str, Any]] = []

    async def _request(self, body: dict[str, Any], *, endpoint: str) -> Any:  # type: ignore[override]
        self.bodies.append(body)
        return {"request_id": "r1"}


@pytest.mark.parametrize(
    ("service", "kie", "sosana", "expected"),
    [
        ("kie", " kie-k ", "sos-k", "kie-k"),
        ("sosana", "kie-k", "\tsos-k ", "sos-k"),
        ("fal", "kie-k", "sos-k", None),
        ("kie", "   ", "sos-k", None),
        ("sosana", "kie-k", "", None),
    ],
)
async def test_api_key_only_for_kie_and_sosana_and_only_when_non_empty(
    service: str, kie: str, sosana: str, expected: str | None
) -> None:
    client = _CapturingProxy(_settings(KIE_API_KEY=kie, SOSANA_API_KEY=sosana))
    await client.submit(
        service=service,
        endpoint="https://x.test/e",
        payload={"prompt": "p"},
        callback_url="https://cb.test/x",
        catalog_endpoint="fal-ai/x",
    )
    body = client.bodies[0]
    if expected is None:
        assert "apiKey" not in body
    else:
        assert body["apiKey"] == expected


def test_mask_secret_text_hides_live_keys_and_key_shaped_fragments() -> None:
    cfg = _settings(
        KIE_API_KEY="kie-live-123", SOSANA_API_KEY="sos-live-456", PROXY_API_KEY="px-live-789"
    )
    text = (
        "kie said kie-live-123; sosana sos-live-456; proxy px-live-789; "
        "Authorization: Bearer abc.def; sk_test_ZZZ; https://x?key=k1&token=t2&secret=s3 ok"
    )
    masked = mask_secret_text(text, settings=cfg)
    for secret in ("kie-live-123", "sos-live-456", "px-live-789", "abc.def", "sk_test_ZZZ"):
        assert secret not in masked
    for fragment in ("k1", "t2", "s3"):
        assert f"={fragment}" not in masked
    assert "Bearer ***" in masked
    assert "key=***" in masked
    assert masked.endswith(" ok")


def test_mask_secret_text_keeps_plain_text_and_ignores_blank_keys() -> None:
    cfg = _settings(KIE_API_KEY="  ", SOSANA_API_KEY="")
    assert mask_secret_text("prompt is too long", settings=cfg) == "prompt is too long"


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
    assert callback_url(settings=cfg, job_id=job_id, route="kie") == (
        f"https://{_DOMAIN}/v1/media/webhooks/proxy/{job_id}?token={expected}&route=kie"
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
        # ADR-108 §8: vendor credits are not money — never read as a USD price.
        ({"data": {"creditsConsumed": 12}}, None),
        ({"creditsConsumed": 12}, None),
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
        # The fal-only list stays where it is passed explicitly (upload only).
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
