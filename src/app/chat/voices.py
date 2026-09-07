"""Voice registry (ADR-100 §3): catalog for GET /v1/voices + the voice resolution of a synthesis.

Single source of truth for the seven voices, by the same pattern as ``app.chat.characters``
(ADR-097 §1) and ``app.chat.presets`` (ADR-035): a module-level static tuple plus pure functions
over it. No table, no env-JSON, no migration for the catalog itself.

Each entry carries:
- ``id`` — stable slug; the value stored in ``user_preferences.default_voice_id`` and half of the
  client's audio-cache key ``(stepId, voiceId)``. Public.
- ``provider`` / ``provider_voice_id`` / ``instructions`` — the synthesis triple. SERVER-SIDE
  ONLY (ADR-100 §3), for the same reason ``persona`` is (ADR-097 §3): these are parameters we
  expect to keep tuning BY EAR, and handing them out would turn a wording fix into an app
  release. Their whole point is that moving ONE voice to another provider is a one-line edit
  here — ``id`` does not change, so neither saved user settings nor client caches invalidate.
- ``gender`` — the axis the owner named for the two default entries. Public.
- ``name`` — ``locale -> display name`` for the settings screen; key ``"en"`` is REQUIRED
  (canon / per-field fallback, ADR-049 §1). Public only for ``selectable`` entries.
- ``selectable`` — may a user pick this entry as their default. Character voices are NOT
  selectable and are absent from the catalog: they are not chosen by the user, and listing them
  would invite the app to send one back.

Locale RESOLUTION is the router's concern, not this module's.

Q-100-4: ``provider_voice_id`` values are the voices ``gpt-4o-mini-tts`` actually serves, checked
against the provider's own set rather than taken from the design document, and the male/female
defaults plus the five character voices are the owner's own listening choice (2026-09-08).
"""

from __future__ import annotations

import logging
from typing import Any, Literal, NamedTuple

from app.chat.characters import character_voice_id
from app.chat.presets import DEFAULT_PRESET_LOCALE
from app.config import get_settings
from app.observability.logging import get_logger, log_event

logger = get_logger(__name__)

# The one provider that serves synthesis today (ADR-100 §8). Kept as a field rather than a
# constant so that moving a SINGLE voice elsewhere stays a one-line edit in the table below.
PROVIDER_OPENAI = "openai"


class Voice(NamedTuple):
    """One voice: stable identity, public display fields, server-side synthesis triple."""

    id: str
    provider: str
    provider_voice_id: str
    instructions: str
    gender: Literal["male", "female"]
    name: dict[str, str]
    selectable: bool


def _loc(en: str, ru: str) -> dict[str, str]:
    """Display map for the two locales filled at launch; ``zh-Hans`` falls back to ``en``."""
    return {"en": en, "ru": ru}


# Static registry — single source of truth (ADR-100 §3). Declaration order IS the on-screen order
# of the catalog and is identical in every locale. The two `selectable` entries come first so the
# last-resort fallback of `resolve_voice` (the first selectable entry) is a deliberate value.
_VOICES: tuple[Voice, ...] = (
    Voice(
        id="default_male",
        provider=PROVIDER_OPENAI,
        provider_voice_id="cedar",
        instructions=(
            "Speak in an even, friendly, neutral tone at a natural conversational pace. "
            "No performance, no accent, no dramatic emphasis — this is the plain assistant voice."
        ),
        gender="male",
        name=_loc("Male", "Мужской"),
        selectable=True,
    ),
    Voice(
        id="default_female",
        provider=PROVIDER_OPENAI,
        provider_voice_id="marin",
        instructions=(
            "Speak in an even, friendly, neutral tone at a natural conversational pace. "
            "No performance, no accent, no dramatic emphasis — this is the plain assistant voice."
        ),
        gender="female",
        name=_loc("Female", "Женский"),
        selectable=True,
    ),
    Voice(
        id="char_anime_girl",
        provider=PROVIDER_OPENAI,
        provider_voice_id="coral",
        instructions=(
            "Speak brightly and with genuine enthusiasm, at a quick but clearly articulated pace. "
            "Warm and whole-hearted, never squeaky or babyish."
        ),
        gender="female",
        name=_loc("Anime Girl", "Аниме-девушка"),
        selectable=False,
    ),
    Voice(
        id="char_fantasy_queen",
        provider=PROVIDER_OPENAI,
        provider_voice_id="sage",
        instructions=(
            "Speak ceremoniously and unhurriedly, with regal warmth and full, measured phrasing. "
            "Dignified rather than cold, and never condescending."
        ),
        gender="female",
        name=_loc("Fantasy Queen", "Королева фэнтези"),
        selectable=False,
    ),
    Voice(
        id="char_vampire_lord",
        provider=PROVIDER_OPENAI,
        provider_voice_id="onyx",
        instructions=(
            "Speak slowly and in a velvet low register, faintly amused, with dry irony. "
            "Composed and unhurried; no theatrical menace, no hissing, no growling."
        ),
        gender="male",
        name=_loc("Vampire Lord", "Лорд вампиров"),
        selectable=False,
    ),
    Voice(
        id="char_cyber_assassin",
        provider=PROVIDER_OPENAI,
        provider_voice_id="ash",
        instructions=(
            "Speak in clipped sentences with a low tone and cold professionalism. "
            "Economical and level — no menace, no aggression, no whispering."
        ),
        gender="male",
        name=_loc("Cyber Assassin", "Кибер-ассасин"),
        selectable=False,
    ),
    Voice(
        id="char_virtual_friend",
        provider=PROVIDER_OPENAI,
        provider_voice_id="nova",
        instructions=(
            "Speak in warm, everyday conversational speech with real, unforced interest. "
            "Relaxed and close, like a friend talking — no performance, no sweetness."
        ),
        gender="female",
        name=_loc("Virtual Friend", "Виртуальный друг"),
        selectable=False,
    ),
)

_BY_ID: dict[str, Voice] = {v.id: v for v in _VOICES}
_SELECTABLE: tuple[Voice, ...] = tuple(v for v in _VOICES if v.selectable)


def get_voice(voice_id: str | None) -> Voice | None:
    """Registry lookup by stable slug, exact match. ``None`` for unknown/empty."""
    if not voice_id:
        return None
    return _BY_ID.get(voice_id)


def is_selectable_voice(voice_id: str) -> bool:
    """True when ``voice_id`` is a voice a user is allowed to pick (the `unknown_voice` gate).

    Character voices deliberately answer ``False``: they are not part of the catalog, so a client
    sending one back into ``PATCH /v1/preferences`` is asking for something the contract never
    offered (ADR-100 §3).
    """
    entry = _BY_ID.get(voice_id)
    return entry is not None and entry.selectable


def voice_catalog(locale: str) -> list[dict[str, Any]]:
    """Public catalog for ``locale`` — ``{id, name, gender}`` of ``selectable`` entries, in order.

    Pure (no I/O, no settings). ``name`` resolves for ``locale`` with a per-field EN fallback (an
    unfilled locale degrades to the canon, never to an empty string); ``id``/``gender`` are
    locale-independent. ``provider`` / ``provider_voice_id`` / ``instructions`` are deliberately
    absent — they are internal synthesis parameters, not displayable content (ADR-100 §3).
    Whether the feature is enabled on this instance is the router's decision, not this function's.
    """
    return [
        {
            "id": v.id,
            "name": v.name.get(locale) or v.name[DEFAULT_PRESET_LOCALE],
            "gender": v.gender,
        }
        for v in _SELECTABLE
    ]


def _fallback_voice() -> Voice:
    """Last line of defence: the first ``selectable`` entry in declaration order (ADR-100 §4).

    "Nothing to speak with" is not a failure worth dropping a request that HAS something to say,
    so voice resolution never raises and never yields ``500``.
    """
    return _SELECTABLE[0]


def resolve_default_voice_id(user_default_voice_id: str | None) -> str:
    """Voice of a chat WITHOUT a character: user setting → instance default → fallback.

    Steps 2 and 3 of ``resolve_voice`` (ADR-100 §4), reused verbatim by ``GET /v1/voices`` for
    its ``defaultVoiceId`` field, which answers exactly that question ("what will THIS user hear
    in a chat without a character"). One function, so the pre-selected row in settings can never
    disagree with what the next synthesis actually uses.
    """
    entry = get_voice(user_default_voice_id)
    if entry is not None and entry.selectable:
        return entry.id
    if user_default_voice_id:
        # The stored setting points at a voice that was retired by a deploy, or at a character
        # voice that was never selectable. Fall through rather than fail — but say so, because a
        # sustained rate of this line means a registry edit stranded real user settings.
        log_event(
            logger,
            logging.WARNING,
            "voice_setting_unresolved",
            voiceId=user_default_voice_id,
            step="user_preference",
        )
    instance_default = get_settings().tts_default_voice_id
    entry = get_voice(instance_default)
    if entry is not None and entry.selectable:
        return entry.id
    log_event(
        logger,
        logging.WARNING,
        "voice_setting_unresolved",
        voiceId=instance_default,
        step="instance_default",
    )
    return _fallback_voice().id


def resolve_voice(*, character_id: str | None, user_default_voice_id: str | None) -> Voice:
    """The ONE function that decides which voice a synthesis speaks with (ADR-100 §4).

    Three steps, in order:

    1. the session's CHARACTER — only when ``CHARACTERS_ENABLED`` is on and the stored
       ``chat_sessions.character_id`` still resolves in the character registry;
    2. the user's ``user_preferences.default_voice_id``, when it points at a ``selectable`` entry;
    3. the instance default ``TTS_DEFAULT_VOICE_ID``.

    A step whose value does not resolve to a live registry entry FALLS THROUGH to the next one
    with a WARNING; the last line of defence is the first ``selectable`` entry. Nothing here can
    raise, by design.

    ``CHARACTERS_ENABLED=false`` skips step 1 entirely, even for a session that has a stored
    ``character_id`` — the literal symmetry with the prompt layer (ADR-097 §7): one switch turns
    the character off whole. The opposite would give a chat that answers in the plain assistant
    voice but SOUNDS like the Vampire Lord.

    The voice is resolved AT SYNTHESIS TIME and is never pinned to the session — that is the
    deliberate contrast with the session-fixed ``character_id`` (ADR-100 §5): the character is
    frozen because the history is replayed to the model, while the voice touches no context at
    all. A default-voice setting that only affected chats the user has not started yet would be
    indistinguishable from a broken setting.
    """
    if get_settings().characters_enabled:
        voice_id = character_voice_id(character_id)
        if voice_id is not None:
            entry = get_voice(voice_id)
            if entry is not None:
                return entry
            log_event(
                logger,
                logging.WARNING,
                "voice_setting_unresolved",
                voiceId=voice_id,
                step="character",
            )
    resolved = get_voice(resolve_default_voice_id(user_default_voice_id))
    # resolve_default_voice_id only ever returns a live registry id; the guard is for the
    # type-checker, not for a reachable state.
    return resolved if resolved is not None else _fallback_voice()
