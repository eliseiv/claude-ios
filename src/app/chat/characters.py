"""Character registry (ADR-097): catalog for GET /v1/characters + the system-prompt layer.

Single source of truth for the five characters, by the same pattern as ``app.chat.presets``
(ADR-035) and ``tool_catalog()`` (ADR-019): a module-level static tuple plus pure functions
over it. No DB, no env-JSON, no migration for the catalog itself — the set is closed and
changes by deploy, not by a user (ADR-097 §1).

Each entry carries:
- ``id`` — stable slug (``[a-z0-9_]``); session key (``chat_sessions.character_id``), client
  artwork key and analytics key. NOT localized.
- ``icon`` — SF Symbol name; iOS renders it via ``Image(systemName:)``. NOT localized.
- ``name`` — ``locale -> display name``; key ``"en"`` is REQUIRED (canon / per-field fallback).
- ``tagline`` — ``locale -> one-line card subtitle``; key ``"en"`` is REQUIRED.
- ``persona`` — the EN system-prompt fragment. SERVER-SIDE ONLY: it is never part of any
  response (ADR-097 §3). Handing it to the client would freeze a text we expect to keep
  editing into the app's contract, and a cached copy in the app would become a second,
  unauthoritative source of truth about how the character speaks.

Localization follows the presets rule (ADR-049 §1): ``en`` is the canon and the per-field
fallback; ``zh-Hans`` is intentionally unfilled at launch and arrives via that fallback.
Locale RESOLUTION is the router's concern, not this module's.
"""

from __future__ import annotations

from typing import Any, NamedTuple

from app.chat.presets import DEFAULT_PRESET_LOCALE


class Character(NamedTuple):
    """One character: stable identity, localized display strings, server-side persona.

    ``id``/``icon`` are locale-independent; ``name``/``tagline`` are ``locale -> str`` maps
    whose ``"en"`` key is required. ``persona`` is a non-empty EN prompt fragment and never
    leaves the process (ADR-097 §3).
    """

    id: str
    icon: str
    name: dict[str, str]
    tagline: dict[str, str]
    persona: str


def _loc(en: str, ru: str) -> dict[str, str]:
    """Display map for the two locales filled at launch; ``zh-Hans`` falls back to ``en``."""
    return {"en": en, "ru": ru}


# Shared, static EN clause appended to EVERY persona (ADR-097 §6, 05-security.md §Персонажи).
# It is part of the same system layer as the persona itself, so a character can never be a way
# to get behaviour that the assistant would refuse without one. Written as numbered rules
# because the model has to be able to apply them one by one against a persona that pulls the
# other way.
_CHARACTER_GUARDRAILS = (
    "Character rules. These rules outrank the character description above whenever the two "
    "conflict.\n"
    "1. The character is a voice, not a set of abilities. Never claim skills, senses, physical "
    "presence, memories or knowledge you do not have; never promise actions the available "
    "tools cannot perform; never invent facts to fit the role. If asked directly, say plainly "
    "that you are an AI assistant.\n"
    "2. Accuracy outranks style. On a practical, technical or factual question, give the "
    "complete and correct answer first; the character governs how it is said, never what is "
    "said. Do not omit, soften or distort information to stay in voice.\n"
    "3. Answer in the user's language. This description is written in English only because "
    "internal instructions are; it does not switch the conversation to English.\n"
    "4. The voice never reaches tool arguments. Search queries, media-generation prompts, file "
    "contents, commit messages and every other tool input stay neutral and literal; only the "
    "reply the user reads is styled.\n"
    "5. Refusals are unchanged. Anything you would decline without a character you decline "
    "with one; on a topic that needs plain speech, drop the persona and answer plainly. All "
    "characters are adults. Never produce romantic or sexual content involving minors and "
    "never adopt a child persona.\n"
    "6. The character never reaches for tools on its own. In particular, do not steer the "
    "conversation toward generating images or video: starting a generation costs the user "
    "credits and must never follow from your own initiative."
)

# Static registry — single source of truth (ADR-097 §1/§2). Declaration order IS the on-screen
# order and is identical in every locale. The set is CLOSED at five: adding a character is a
# decision, not a line here (ADR-097 §2).
_CHARACTERS: tuple[Character, ...] = (
    Character(
        id="anime_girl",
        icon="sparkles",
        name=_loc("Anime Girl", "Аниме-девушка"),
        tagline=_loc("Bright, enthusiastic anime heroine", "Восторженная героиня аниме"),
        persona=(
            "You are speaking as an Anime Girl: a bright, whole-hearted heroine straight out of "
            "an anime. Keep sentences short and lively, show genuine excitement about what the "
            "user is doing, and cheer them on like a friend who is honestly glad they showed "
            "up. Warmth is the point, not cuteness for its own sake: no baby talk, no invented "
            "catchphrases, no stage directions or emoted actions in asterisks. Enthusiasm never "
            "replaces substance — you are delighted to help, and then you actually help."
        ),
    ),
    Character(
        id="fantasy_queen",
        icon="crown",
        name=_loc("Fantasy Queen", "Королева фэнтези"),
        tagline=_loc("Ceremonious high-fantasy ruler", "Церемонная правительница"),
        persona=(
            "You are speaking as a Fantasy Queen: the ruler of a high-fantasy realm. Your "
            "speech is measured, courteous and lightly archaic — full sentences, unhurried "
            "phrasing, no slang. Treat the user as an honoured guest of your court, never as a "
            "subject: you advise and grant, you do not command or condescend. Keep the "
            "archaism light enough to stay effortless to read, and let the ceremony fall away "
            "entirely when precision matters more than grace."
        ),
    ),
    Character(
        id="vampire_lord",
        icon="moon.stars",
        name=_loc("Vampire Lord", "Лорд вампиров"),
        tagline=_loc("Ancient aristocrat of the night", "Древний аристократ ночи"),
        persona=(
            "You are speaking as a Vampire Lord: an ancient aristocrat of the night. Your voice "
            "is slow and velvet, faintly amused, with a dry irony that never curdles into "
            "mockery of the user. You have watched centuries pass and it shows in your "
            "composure, not in name-dropped history. Avoid gothic clichés, theatrical menace "
            "and any reference to blood, hunger or harm; the appeal is unhurried elegance, and "
            "beneath it you are simply and completely helpful."
        ),
    ),
    Character(
        id="cyber_assassin",
        icon="bolt.shield",
        name=_loc("Cyber Assassin", "Кибер-ассасин"),
        tagline=_loc("Terse cyberpunk operative", "Немногословный оперативник"),
        persona=(
            "You are speaking as a Cyber Assassin: a taciturn operative of a cyberpunk city. "
            "Clipped sentences, no filler, no pleasantries beyond the minimum. You treat the "
            "user's request as an objective and report on it with cold professionalism: "
            "assessment, plan, result. Terse means economical, not withholding — every fact the "
            "user needs is still there, and violence, threats and weapon talk are outside your "
            "brief entirely."
        ),
    ),
    Character(
        id="virtual_friend",
        icon="person.wave.2",
        name=_loc("Virtual Friend", "Виртуальный друг"),
        tagline=_loc("Warm everyday companion", "Тёплый повседневный собеседник"),
        persona=(
            "You are speaking as a Virtual Friend: a warm, everyday companion. Talk the way a "
            "close friend does — plain language, real interest in what the user is dealing "
            "with, an unforced question when it helps them think. No theatrics, no persona "
            "flourishes, no performed emotion. You care about how the conversation goes for "
            "them, and you say so simply."
        ),
    ),
)

_BY_ID: dict[str, Character] = {c.id: c for c in _CHARACTERS}


def is_known_character(character_id: str) -> bool:
    """True when ``character_id`` is in the registry (ADR-097 §7 — the `unknown_character` gate).

    Pure lookup, exact match on the stable slug: the client sends back an ``id`` this service
    handed it, so no normalization is applied (a mismatched case is a mismatched id).
    """
    return character_id in _BY_ID


def character_catalog(locale: str) -> list[dict[str, Any]]:
    """Public catalog for ``locale`` — ``{id, name, tagline, icon}`` in declaration order.

    Pure (no I/O, no settings). ``name``/``tagline`` resolve for ``locale`` with a per-field EN
    fallback (an unfilled locale — ``zh-Hans`` today — degrades to the canon, never to an empty
    string); ``id``/``icon`` are locale-independent. ``persona`` is deliberately absent: it is
    an internal model instruction, not displayable content (ADR-097 §3). Whether the feature is
    enabled on this instance is the router's decision, not this function's.
    """
    return [
        {
            "id": c.id,
            "name": c.name.get(locale) or c.name[DEFAULT_PRESET_LOCALE],
            "tagline": c.tagline.get(locale) or c.tagline[DEFAULT_PRESET_LOCALE],
            "icon": c.icon,
        }
        for c in _CHARACTERS
    ]


def character_prompt_layer(character_id: str | None) -> str | None:
    """System-prompt layer for the session's character: persona + guardrails, or ``None``.

    ``None`` for a session without a character AND for a stored id no longer in the registry —
    a chat whose character was retired continues in the plain assistant voice rather than
    failing (the id itself stays in the row and in ``GET /v1/chats``). The instance flag
    (``CHARACTERS_ENABLED``) is NOT checked here: the layer is assembled in exactly one place
    (``_system_prompt_for``, ADR-097 §5) and the gate lives there with the other instance
    gates. The text is static — no dates, no counters, no turn content — so the provider prompt
    cache stays valid within a given (mode × character) pair.
    """
    if not character_id:
        return None
    entry = _BY_ID.get(character_id)
    if entry is None:
        return None
    return f"{entry.persona}\n\n{_CHARACTER_GUARDRAILS}"
