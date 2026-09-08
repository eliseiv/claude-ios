"""Слой разрешения изменяемых значений инстанса (ADR-099 §2).

Пакет отдельный намеренно: его читают не только admin-ручки, но и оркестратор чата,
медиа-сабмит, вебхуки и каталоги, — то есть он принадлежит не модулю ``admin``, а всему
приложению; ``admin`` лишь **пишет** в него.

Единственный порядок разрешения любой величины: **оверлей БД → env → дефолт кода**.
"""

from app.instance_config.media_pricing import (
    LegacyVideoPricing,
    PriceCell,
    derive_legacy_video_pricing,
    media_base_credits,
    photo_price_cells,
    photo_resolution_credits,
    video_price_cells,
)
from app.instance_config.models import (
    catalog_rows as chat_catalog_rows,
)
from app.instance_config.models import (
    instance_default_model,
    model_is_selectable,
    offered_model_ids,
)
from app.instance_config.products import (
    CHANNEL_ADAPTY,
    CHANNEL_CLOUDPAYMENTS,
    CHANNEL_MANUAL,
    CHANNEL_STOREKIT,
    PURCHASE_KIND_ONE_TIME,
    PURCHASE_KIND_SUBSCRIPTION,
    ProductRow,
    is_archived,
    known_product_ids,
    one_time_credits,
    one_time_product_ids,
    operator_created_rows,
    subscription_credits,
)
from app.instance_config.products import (
    catalog_rows as product_catalog_rows,
)
from app.instance_config.snapshot import (
    InstanceConfigSnapshot,
    get_snapshot,
    overrides_refresh_loop,
    refresh_snapshot,
    refresh_snapshot_from_pool,
    reset_snapshot,
)
from app.instance_config.tariffs import (
    base_credits_for,
    chat_turn_credit_cost,
    media_run_price,
    photo_unit_credits,
    pricing_rows,
)
from app.instance_config.values import (
    advertised_generation_modes,
    anthropic_thinking_display,
    characters_enabled,
    code_tools_enabled,
    disabled_tool_families,
    media_tools_enabled,
    memory_enabled,
    moderation_block_categories,
    moderation_enabled,
    presets_default_locale,
    reasoning_level,
    voice_input_enabled,
)

__all__ = [
    "CHANNEL_ADAPTY",
    "CHANNEL_CLOUDPAYMENTS",
    "CHANNEL_MANUAL",
    "CHANNEL_STOREKIT",
    "InstanceConfigSnapshot",
    "LegacyVideoPricing",
    "PriceCell",
    "PURCHASE_KIND_ONE_TIME",
    "PURCHASE_KIND_SUBSCRIPTION",
    "ProductRow",
    "advertised_generation_modes",
    "anthropic_thinking_display",
    "base_credits_for",
    "characters_enabled",
    "chat_catalog_rows",
    "chat_turn_credit_cost",
    "code_tools_enabled",
    "derive_legacy_video_pricing",
    "disabled_tool_families",
    "get_snapshot",
    "instance_default_model",
    "is_archived",
    "known_product_ids",
    "media_base_credits",
    "media_run_price",
    "media_tools_enabled",
    "memory_enabled",
    "model_is_selectable",
    "moderation_block_categories",
    "moderation_enabled",
    "offered_model_ids",
    "one_time_credits",
    "one_time_product_ids",
    "operator_created_rows",
    "overrides_refresh_loop",
    "photo_price_cells",
    "photo_resolution_credits",
    "photo_unit_credits",
    "presets_default_locale",
    "pricing_rows",
    "product_catalog_rows",
    "reasoning_level",
    "refresh_snapshot",
    "refresh_snapshot_from_pool",
    "reset_snapshot",
    "subscription_credits",
    "video_price_cells",
    "voice_input_enabled",
]
