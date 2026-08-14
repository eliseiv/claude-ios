"""Prompt presets registry (ADR-035, localized by ADR-049): catalog for GET /v1/presets.

Single source of truth for the chat home-screen preset chips (Plan Week, Meeting Notes, …).
By the same pattern as ``tool_catalog()`` (``app.chat.tools``): a module-level static list +
a pure ``preset_catalog(locale)`` that returns the entries in declaration order (= chip order on
screen). No I/O, no state, no DB; provider/instance-agnostic — identical on every instance
(ADR-033). Editing presets without a deploy (config-JSON / DB) is deferred — TD-026.

Localization (ADR-049): ``id`` and ``icon`` are stable machine identifiers and are NOT localized
(client analytics/cache key by ``id``, ``icon`` is an SF Symbol resource); ``title`` and ``prompt``
carry one string per locale. EN is the canon and per-field fallback (Q-035-2 partially closed).

Each preset carries:
- ``id``    — stable snake_case slug (``[a-z0-9_]``), unique in the set; stable across releases.
- ``icon``  — SF Symbol name (ADR-035 §4); the iOS client renders it via ``Image(systemName:)``.
- ``title`` — ``locale -> chip display name``; key ``"en"`` is REQUIRED (canon/fallback).
- ``prompt``— ``locale -> composer text``; key ``"en"`` is REQUIRED (canon/fallback).
"""

from __future__ import annotations

from typing import Any, NamedTuple

# Supported preset locales — single source of truth (ADR-049 §1; EN first = canon/fallback).
# Extending = add the locale here AND fill title/prompt in the registry. Never hardcode "exactly 2".
# ``zh-Hans`` is the BCP-47 canonical form (iOS / Accept-Language); matching is case-insensitive.
SUPPORTED_PRESET_LOCALES: tuple[str, ...] = ("en", "ru", "zh-Hans")
# Canon and per-field fallback locale (ADR-049 §1). Its key is required in every preset.
DEFAULT_PRESET_LOCALE: str = "en"

# Aliases that resolve to a supported locale after ``strip().lower().replace("_", "-")``.
# ``zh`` / ``zh-cn`` / ``zh-sg`` → Simplified Chinese (the only Chinese catalog we ship).
# Traditional tags (``zh-hant``, ``zh-tw``, ``zh-hk``) are intentionally absent → unsupported.
_PRESET_LOCALE_ALIASES: dict[str, str] = {
    "zh": "zh-Hans",
    "zh-cn": "zh-Hans",
    "zh-sg": "zh-Hans",
    "zh-hans": "zh-Hans",
    "zh-hans-cn": "zh-Hans",
}


def canonicalize_preset_locale(raw: str | None) -> str | None:
    """Map a client locale tag onto ``SUPPORTED_PRESET_LOCALES``, or ``None`` if unsupported.

    Matching is case-insensitive and accepts ``_`` as ``-`` (``zh_Hans`` → ``zh-Hans``).
    Tries the full tag, then progressively shorter prefixes (``zh-Hans-CN`` → ``zh-Hans``,
    ``ru-RU`` → ``ru``), then the alias table. Empty / blank → ``None``.
    """
    if raw is None:
        return None
    tag = raw.strip().lower().replace("_", "-")
    if not tag:
        return None
    by_lower = {locale.lower(): locale for locale in SUPPORTED_PRESET_LOCALES}
    if tag in by_lower:
        return by_lower[tag]
    aliased = _PRESET_LOCALE_ALIASES.get(tag)
    if aliased is not None:
        return aliased
    # Prefix fallback is only against the supported set (``ru-RU`` → ``ru``).
    # Aliases like ``zh`` must not steal ``zh-Hant`` / ``zh-TW``.
    parts = tag.split("-")
    for length in range(len(parts) - 1, 0, -1):
        candidate = "-".join(parts[:length])
        if candidate in by_lower:
            return by_lower[candidate]
    return None


class Preset(NamedTuple):
    """One prompt preset (ADR-035 §1, localized ADR-049 §1).

    ``id``/``icon`` are stable and locale-independent; ``title``/``prompt`` are ``locale -> str``
    maps whose ``"en"`` key is required (canon). All EN values are non-empty.
    """

    id: str
    icon: str
    title: dict[str, str]
    prompt: dict[str, str]


# Static registry — single source of truth (ADR-035 §2/§3, ADR-049 §1.1). Declaration order IS the
# chip order on the chat home screen. Editing without a deploy is intentionally out of scope
# (TD-026). EN strings are unchanged from ADR-035 §3; RU strings are the approved ADR-049 §1.1 set.
_PRESETS: tuple[Preset, ...] = (
    Preset(
        id="plan_week",
        icon="calendar",
        title={
            "en": "Plan Week",
            "ru": "Планирование недели",
            "zh-Hans": "规划本周",
        },
        prompt={
            "en": (
                "Help me plan my upcoming week. Ask me about my priorities, deadlines, and "
                "commitments, then propose a balanced day-by-day schedule."
            ),
            "ru": (
                "Помоги спланировать предстоящую неделю. Расспроси меня о приоритетах, сроках и "
                "обязательствах, а затем предложи сбалансированное расписание по дням."
            ),
            "zh-Hans": (
                "帮我规划接下来的一周。先问我优先级、截止日期和已有安排，然后给出平衡的每日日程。"
            ),
        },
    ),
    Preset(
        id="meeting_notes",
        icon="person.2",
        title={
            "en": "Meeting Notes",
            "ru": "Заметки со встречи",
            "zh-Hans": "会议纪要",
        },
        prompt={
            "en": (
                "Turn my raw meeting notes into a clean summary with key decisions, action items "
                "(with owners), and open questions. I'll paste the notes next."
            ),
            "ru": (
                "Преврати мои черновые заметки со встречи в аккуратное резюме: ключевые решения, "
                "задачи с ответственными и открытые вопросы. Я вставлю заметки следующим "
                "сообщением."
            ),
            "zh-Hans": (
                "把我的会议草稿整理成清晰纪要：关键决策、待办（含负责人）和未决问题。"
                "我接下来会粘贴笔记。"
            ),
        },
    ),
    Preset(
        id="tasks_from_photo",
        icon="camera",
        title={
            "en": "Tasks from Photo",
            "ru": "Задачи с фото",
            "zh-Hans": "从照片提取任务",
        },
        prompt={
            "en": (
                "I'll attach a photo of a note, whiteboard, or list. Extract every actionable task "
                "from it and return them as a clear checklist."
            ),
            "ru": (
                "Я прикреплю фото заметки, доски или списка. Выдели из него все конкретные "
                "задачи и верни их в виде понятного чек-листа."
            ),
            "zh-Hans": (
                "我会附上一张便签、白板或清单的照片。请从中提取所有可执行任务，并以清晰的清单返回。"
            ),
        },
    ),
    Preset(
        id="design_brief",
        icon="paintbrush",
        title={
            "en": "Design Brief",
            "ru": "Дизайн-бриф",
            "zh-Hans": "设计简报",
        },
        prompt={
            "en": (
                "Help me write a concise design brief. Ask me about the goal, audience, scope, "
                "constraints, and success criteria, then draft the brief."
            ),
            "ru": (
                "Помоги составить лаконичный дизайн-бриф. Расспроси меня о цели, аудитории, объёме "
                "работ, ограничениях и критериях успеха, а затем подготовь бриф."
            ),
            "zh-Hans": (
                "帮我写一份简洁的设计简报。先问我目标、受众、范围、限制和成功标准，然后起草简报。"
            ),
        },
    ),
    Preset(
        id="daily_review",
        icon="checklist",
        title={
            "en": "Daily Review",
            "ru": "Итоги дня",
            "zh-Hans": "每日回顾",
        },
        prompt={
            "en": (
                "Guide me through a short daily review: what I accomplished, what's still open, "
                "and the top 3 priorities for tomorrow."
            ),
            "ru": (
                "Проведи меня через короткий разбор дня: что удалось сделать, что осталось "
                "незавершённым и какие три главных приоритета на завтра."
            ),
            "zh-Hans": (
                "带我做一次简短的每日回顾：今天完成了什么、还有什么未完成，以及明天的三个优先事项。"
            ),
        },
    ),
    Preset(
        id="summarize_text",
        icon="doc.text",
        title={
            "en": "Summarize Text",
            "ru": "Краткое изложение",
            "zh-Hans": "文本摘要",
        },
        prompt={
            "en": (
                "Summarize the text I provide. Give a 3-sentence overview, then key points as "
                "bullets. I'll paste the text next."
            ),
            "ru": (
                "Кратко изложи текст, который я пришлю. Дай обзор в трёх предложениях, а затем "
                "ключевые мысли списком. Я вставлю текст следующим сообщением."
            ),
            "zh-Hans": (
                "总结我提供的文本。先用三句话概述，再用要点列出关键信息。我接下来会粘贴文本。"
            ),
        },
    ),
    Preset(
        id="project_structure",
        icon="folder",
        title={
            "en": "Project Structure",
            "ru": "Структура проекта",
            "zh-Hans": "项目结构",
        },
        prompt={
            "en": (
                "Help me design a project structure. Ask about the project type and goals, then "
                "propose a folder/file layout with a short rationale."
            ),
            "ru": (
                "Помоги продумать структуру проекта. Расспроси о типе проекта и целях, а затем "
                "предложи структуру папок и файлов с кратким обоснованием."
            ),
            "zh-Hans": (
                "帮我设计项目结构。先问项目类型和目标，然后给出文件夹/文件布局并简要说明理由。"
            ),
        },
    ),
)


def preset_catalog(locale: str) -> list[dict[str, Any]]:
    """Machine-readable catalog of prompt presets for the given locale (ADR-035, ADR-049 §2).

    Pure (no I/O): iterates the static ``_PRESETS`` registry in declaration order (= chip order)
    and returns a list of ``{id, title, icon, prompt}`` dicts. ``title``/``prompt`` are resolved
    for ``locale`` with a per-field EN fallback — an unknown locale or a field missing for the
    locale degrades to ``DEFAULT_PRESET_LOCALE`` (never an empty string). ``id``/``icon`` are
    locale-independent. Locale resolution itself is the router's concern (ADR-049 §3), not here.
    """
    return [
        {
            "id": p.id,
            "title": p.title.get(locale) or p.title[DEFAULT_PRESET_LOCALE],
            "icon": p.icon,
            "prompt": p.prompt.get(locale) or p.prompt[DEFAULT_PRESET_LOCALE],
        }
        for p in _PRESETS
    ]
