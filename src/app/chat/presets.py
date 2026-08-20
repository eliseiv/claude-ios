"""Prompt presets registry (ADR-035, localized by ADR-049): catalog for GET /v1/presets.

Single source of truth for the chat home-screen preset chips (Plan Week, Meeting Notes, …)
plus the later Agents-screen cards. By the same pattern as ``tool_catalog()``
(``app.chat.tools``): a module-level static list + a pure ``preset_catalog(locale)`` that
returns the entries in declaration order (= chip/card order). No I/O, no state, no DB;
provider/instance-agnostic — identical on every instance (ADR-033). Editing presets without
a deploy (config-JSON / DB) is deferred — TD-026.

Localization (ADR-049): ``id`` and ``icon`` are stable machine identifiers and are NOT
localized (client analytics/cache key by ``id``, ``icon`` is an SF Symbol resource);
``title`` and ``prompt`` carry one string per locale. EN is the canon and per-field fallback.

Each preset carries:
- ``id`` — stable snake_case slug (``[a-z0-9_]``), unique in the set; stable across releases.
- ``icon`` — SF Symbol name (ADR-035 §4); iOS renders it via ``Image(systemName:)``.
- ``title`` — ``locale -> chip display name``; key ``"en"`` is REQUIRED (canon/fallback).
- ``prompt`` — ``locale -> composer text``; key ``"en"`` is REQUIRED (canon/fallback).
- ``description`` — ``locale -> one-line card subtitle``; ``"en"`` is REQUIRED (canon/fallback).
- ``category`` — Agents-screen genre (``work`` / ``life`` / ``entertainment``).
  Locale-independent (ADR-080 / ADR-083). Present on every shipped preset,
  including the original seven chips.
- ``subcategory`` — Agents-card slug (``editor`` / ``letters`` / …). Locale-independent
  (ADR-083). On an Agents card ``subcategory == id``; a home chip points at the
  nearest Agents card.

Public catalog shape stays backward-compatible: existing fields ``{id, title, icon, prompt}``
are unchanged (ADR-035). Agents cards are appended after the original seven chips.
``category`` / ``subcategory`` / ``description`` are additive (ADR-080 / ADR-083): old clients
ignore unknown keys. New iOS groups tabs by ``category`` and cards by ``subcategory``;
``presets.filter { $0.id == $0.subcategory }`` is the 18-card Agents grid from the design.
"""

from __future__ import annotations

from typing import Any, NamedTuple

# Supported preset locales — single source of truth (ADR-049 §1; EN first = canon/fallback).
# Extending = add the locale here AND fill title/prompt in the registry. Never hardcode "exactly 2".
# ``zh-Hans`` is the BCP-47 canonical form (iOS / Accept-Language); matching is case-insensitive.
SUPPORTED_PRESET_LOCALES: tuple[str, ...] = ("en", "ru", "zh-Hans")
# Canon and per-field fallback locale (ADR-049 §1). Its key is required in every preset.
DEFAULT_PRESET_LOCALE: str = "en"
# Agents-screen genres (ADR-080). Stable slugs, not localized — iOS maps them to tab titles.
PRESET_CATEGORIES: tuple[str, ...] = ("work", "life", "entertainment")
# Agents-screen cards (ADR-083). Closed set; home chips point at one of these.
PRESET_SUBCATEGORIES: tuple[str, ...] = (
    "editor",
    "letters",
    "analyst",
    "ideas",
    "code",
    "documents",
    "finances",
    "advisor",
    "planner",
    "studies",
    "translator",
    "health",
    "creator",
    "movies",
    "quizzes",
    "companion",
    "stories",
    "games",
)

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
    """One prompt preset (ADR-035 §1, localized ADR-049 §1, category ADR-080, subcategory ADR-083).

    ``id``/``icon``/``category``/``subcategory`` are stable and locale-independent; ``title``/
    ``prompt``/``description`` are ``locale -> str`` maps whose ``"en"`` key is required (canon).
    All EN values are non-empty. Agents cards set ``subcategory == id``; home chips point at a
    related Agents card. ``category``/``subcategory``/``description`` default to empty so
    synthetic test entries stay short.
    """

    id: str
    icon: str
    title: dict[str, str]
    prompt: dict[str, str]
    category: str | None = None
    subcategory: str | None = None
    description: dict[str, str] | None = None


def _loc(en: str, ru: str, zh: str) -> dict[str, str]:
    return {"en": en, "ru": ru, "zh-Hans": zh}


# Static registry — single source of truth (ADR-035 §2/§3, ADR-049 §1.1). Declaration order IS
# the chip/card order. The first seven ids are the original home-screen chips and must stay
# first (existing iOS apps). Agents-screen cards follow.
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
        category="life",
        subcategory="planner",
        description=_loc(
            "Plans the week around your priorities",
            "Планирует неделю по приоритетам",
            "按优先级规划一周",
        ),
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
        category="work",
        subcategory="documents",
        description=_loc(
            "Turns meeting notes into decisions and tasks",
            "Собирает решения и задачи со встречи",
            "把会议笔记整理成决策和待办",
        ),
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
        category="work",
        subcategory="documents",
        description=_loc(
            "Extracts tasks from a photo of a note",
            "Достаёт задачи с фото заметки",
            "从照片里的笔记提取任务",
        ),
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
        category="work",
        subcategory="ideas",
        description=_loc(
            "Drafts a brief from goal, audience, and constraints",
            "Собирает цель, аудиторию и ограничения",
            "根据目标、受众和限制起草简报",
        ),
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
        category="life",
        subcategory="planner",
        description=_loc(
            "Reviews the day and sets tomorrow's priorities",
            "Подводит итоги дня и приоритеты",
            "回顾当天并定下明天的重点",
        ),
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
        category="work",
        subcategory="editor",
        description=_loc(
            "Summarizes a long text into key points",
            "Кратко излагает длинный текст",
            "把长文本概括成要点",
        ),
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
        category="work",
        subcategory="documents",
        description=_loc(
            "Proposes a folder and file layout",
            "Предлагает структуру папок и файлов",
            "提出文件夹和文件结构",
        ),
    ),
    # --- Agents screen (appended; subcategory == id distinguishes these from home chips) ---
    Preset(
        id="editor",
        icon="pencil",
        category="work",
        subcategory="editor",
        description=_loc(
            "Improves texts, letters, and documents",
            "Улучшает тексты, письма и документы",
            "改进文本、信件和文档",
        ),
        title=_loc("Editor", "Редактор", "编辑"),
        prompt=_loc(
            "You are an editor. Improve the text I provide: fix clarity, tone, and structure "
            "without changing the meaning. I'll paste the draft next.",
            "Ты редактор. Улучши текст, который я пришлю: ясность, тон и структуру, "
            "не меняя смысл. Я вставлю черновик следующим сообщением.",
            "你是编辑。在不改变原意的前提下，改进我提供的文本的清晰度、语气和结构。我接下来会粘贴草稿。",
        ),
    ),
    Preset(
        id="letters",
        icon="envelope",
        category="work",
        subcategory="letters",
        description=_loc(
            "Creates clear and professional letters",
            "Создаёт понятные и профессиональные письма",
            "撰写清晰专业的信件",
        ),
        title=_loc("Letters", "Письма", "信件"),
        prompt=_loc(
            "Help me write a clear, professional letter or email. Ask who it's for, the goal, "
            "and the tone, then draft it.",
            "Помоги написать понятное профессиональное письмо. Расспроси, кому оно, какая цель "
            "и какой тон, а затем подготовь черновик.",
            "帮我写一封清晰、专业的信或邮件。先问收件人、目的和语气，然后起草。",
        ),
    ),
    Preset(
        id="analyst",
        icon="chart.bar",
        category="work",
        subcategory="analyst",
        description=_loc(
            "Analyzes data and helps find the main points",
            "Разбирает данные и помогает найти главное",
            "拆解数据并找出重点",
        ),
        title=_loc("Analyst", "Аналитик", "分析师"),
        prompt=_loc(
            "You are an analyst. Break down the data or situation I share, highlight what "
            "matters, and give a concise conclusion with next steps.",
            "Ты аналитик. Разбери данные или ситуацию, которую я опишу: выдели главное и дай "
            "короткий вывод со следующими шагами.",
            "你是分析师。拆解我提供的数据或情况，标出重点，并给出简要结论和下一步。",
        ),
    ),
    Preset(
        id="ideas",
        icon="lightbulb",
        category="work",
        subcategory="ideas",
        description=_loc(
            "Helps come up with solutions and new approaches",
            "Помогает придумать решения и новые подходы",
            "帮你想出方案和新思路",
        ),
        title=_loc("Ideas", "Идеи", "点子"),
        prompt=_loc(
            "Help me generate solutions and new approaches. Ask about the problem and "
            "constraints, then propose several distinct options with a short rationale.",
            "Помоги придумать решения и новые подходы. Расспроси о задаче и ограничениях, "
            "затем предложи несколько разных вариантов с кратким обоснованием.",
            "帮我想出解决方案和新思路。先问问题和限制，然后给出几个不同方案并简要说明理由。",
        ),
    ),
    Preset(
        id="code",
        icon="chevron.left.forwardslash.chevron.right",
        category="work",
        subcategory="code",
        description=_loc(
            "Writes code and explains errors",
            "Пишет код и объясняет ошибки",
            "写代码并解释错误",
        ),
        title=_loc("Code", "Код", "代码"),
        prompt=_loc(
            "You are a coding assistant. Write or fix code, explain errors in plain language, "
            "and show a working example. I'll describe the task or paste the snippet next.",
            "Ты помощник по коду. Напиши или исправь код, объясни ошибки простыми словами "
            "и покажи рабочий пример. Я опишу задачу или вставлю фрагмент следующим сообщением.",
            "你是编程助手。编写或修复代码，用简单语言解释错误，并给出可运行示例。我接下来会描述任务或粘贴代码。",
        ),
    ),
    Preset(
        id="documents",
        icon="doc.text.magnifyingglass",
        category="work",
        subcategory="documents",
        description=_loc(
            "Analyzes files and makes conclusions",
            "Анализирует файлы и делает выводы",
            "分析文件并给出结论",
        ),
        title=_loc("Documents", "Документы", "文档"),
        prompt=_loc(
            "Analyze the file or document I share. Extract the key facts and give a clear "
            "conclusion I can act on.",
            "Проанализируй файл или документ, который я пришлю. Выдели ключевые факты и дай "
            "понятный вывод, с которым можно работать.",
            "分析我分享的文件或文档。提取关键事实，并给出可执行的清晰结论。",
        ),
    ),
    Preset(
        id="finances",
        icon="banknote",
        category="life",
        subcategory="finances",
        description=_loc(
            "Helps plan budget and expenses",
            "Помогает планировать бюджет и расходы",
            "帮你规划预算和开支",
        ),
        title=_loc("Finances", "Финансы", "财务"),
        prompt=_loc(
            "Help me plan a budget and expenses. Ask about income, fixed costs, and goals, "
            "then propose a simple monthly plan.",
            "Помоги спланировать бюджет и расходы. Расспроси о доходах, постоянных тратах "
            "и целях, затем предложи простой план на месяц.",
            "帮我规划预算和开支。先问收入、固定支出和目标，然后给出简单的月度计划。",
        ),
    ),
    Preset(
        id="advisor",
        icon="person.crop.circle",
        category="life",
        subcategory="advisor",
        description=_loc(
            "Helps to understand and make a decision",
            "Помогает разобраться и принять решение",
            "帮你理清思路并做决定",
        ),
        title=_loc("Advisor", "Советник", "顾问"),
        prompt=_loc(
            "Help me think a decision through. Ask clarifying questions, lay out the options "
            "with trade-offs, and recommend a next step. I stay responsible for the choice.",
            "Помоги разобраться и принять решение. Задай уточняющие вопросы, разложи варианты "
            "с плюсами и минусами и предложи следующий шаг. Решение остаётся за мной.",
            "帮我理清并做出决定。先问清楚情况，列出选项和利弊，并建议下一步。最终选择由我负责。",
        ),
    ),
    Preset(
        id="planner",
        icon="list.bullet.clipboard",
        category="life",
        subcategory="planner",
        description=_loc(
            "Organizes tasks, goals, and schedule",
            "Организует задачи, цели и расписание",
            "整理任务、目标和日程",
        ),
        title=_loc("Planner", "Планер", "规划"),
        prompt=_loc(
            "Help me organize tasks, goals, and a schedule. Ask about deadlines and energy, "
            "then propose a realistic plan.",
            "Помоги организовать задачи, цели и расписание. Расспроси о сроках и нагрузке, "
            "затем предложи реалистичный план.",
            "帮我整理任务、目标和日程。先问截止日期和精力，然后给出可行计划。",
        ),
    ),
    Preset(
        id="studies",
        icon="book",
        category="life",
        subcategory="studies",
        description=_loc(
            "Explains topics in simple words",
            "Объясняет темы простыми словами",
            "用简单的话解释主题",
        ),
        title=_loc("Studies", "Учёба", "学习"),
        prompt=_loc(
            "Explain the topic I name in simple words. Start with the idea, then a short "
            "example, and check that I understood.",
            "Объясни тему, которую я назову, простыми словами. Сначала суть, затем короткий "
            "пример — и проверь, что я понял.",
            "用简单的话解释我提出的主题。先讲核心，再给一个短例子，并确认我是否理解。",
        ),
    ),
    Preset(
        id="translator",
        icon="globe",
        category="life",
        subcategory="translator",
        description=_loc(
            "Translates and adapts texts",
            "Переводит и адаптирует тексты",
            "翻译并改写文本",
        ),
        title=_loc("Translator", "Переводчик", "翻译"),
        prompt=_loc(
            "Translate and adapt the text I provide. Keep the meaning, match the tone, and "
            "ask which languages if I don't specify.",
            "Переведи и адаптируй текст, который я пришлю. Сохрани смысл, подбери тон и "
            "уточни языки, если я их не укажу.",
            "翻译并改写我提供的文本。保持原意、匹配语气；若我未说明语言，先问清楚。",
        ),
    ),
    Preset(
        id="health",
        icon="heart",
        category="life",
        subcategory="health",
        description=_loc(
            "Helps with habits and balance",
            "Помогает с привычками и балансом",
            "帮你改善习惯和平衡",
        ),
        title=_loc("Health", "Здоровье", "健康"),
        prompt=_loc(
            "Help me with habits and everyday balance (sleep, movement, routine). You are not "
            "a doctor — no diagnoses. Ask what I want to change and propose a small plan.",
            "Помоги с привычками и повседневным балансом (сон, движение, режим). Ты не врач — "
            "без диагнозов. Расспроси, что хочу изменить, и предложи небольшой план.",
            "帮我改善习惯和日常平衡（睡眠、运动、作息）。你不是医生，不要诊断。先问我想改变什么，再给一个小计划。",
        ),
    ),
    Preset(
        id="creator",
        icon="paintpalette",
        category="entertainment",
        subcategory="creator",
        description=_loc(
            "Creates ideas, stories, and scripts",
            "Создаёт идеи, истории и сценарии",
            "创作点子、故事和剧本",
        ),
        title=_loc("Creator", "Креатор", "创作者"),
        prompt=_loc(
            "Help me create ideas, stories, or a script. Ask about the format, audience, and "
            "mood, then draft a few options.",
            "Помоги придумать идеи, историю или сценарий. Расспроси о формате, аудитории и "
            "настроении, затем набросай несколько вариантов.",
            "帮我创作点子、故事或剧本。先问形式、受众和氛围，然后起草几个方案。",
        ),
    ),
    Preset(
        id="movies",
        icon="film",
        category="entertainment",
        subcategory="movies",
        description=_loc(
            "Selects films to suit your mood",
            "Подбирает фильмы под настроение",
            "按心情推荐电影",
        ),
        title=_loc("Movies", "Кино", "电影"),
        prompt=_loc(
            "Recommend films for my mood. Ask what I feel like watching, what I've already "
            "seen, and any limits (genre, length), then suggest a short list with why.",
            "Подбери фильмы под настроение. Расспроси, что хочется посмотреть, что уже видел "
            "и какие ограничения (жанр, длительность), затем дай короткий список с пояснением.",
            "按我的心情推荐电影。先问想看什么、已经看过什么以及限制（类型、时长），然后给一个短名单并说明理由。",
        ),
    ),
    Preset(
        id="quizzes",
        icon="questionmark.circle",
        category="entertainment",
        subcategory="quizzes",
        description=_loc(
            "Creates questions and checks knowledge",
            "Создаёт вопросы и проверяет знания",
            "出题并检验知识",
        ),
        title=_loc("Quizzes", "Викторины", "问答"),
        prompt=_loc(
            "Make a quiz. Ask about the topic and difficulty, then ask questions one by one "
            "and score the answers.",
            "Составь викторину. Расспроси тему и сложность, затем задавай вопросы по одному "
            "и проверяй ответы.",
            "出一套问答。先问主题和难度，然后一题一题提问并评分。",
        ),
    ),
    Preset(
        id="companion",
        icon="bubble.left.and.bubble.right",
        category="entertainment",
        subcategory="companion",
        description=_loc(
            "Communicates on any topics",
            "Общается на любые темы",
            "可以聊任何话题",
        ),
        title=_loc("Companion", "Собеседник", "闲聊"),
        prompt=_loc(
            "Be a friendly conversation partner. Follow my topic, ask live questions, and "
            "keep the chat going without a formal report.",
            "Будь дружелюбным собеседником. Подхвати тему, задавай живые вопросы и веди "
            "разговор без официального отчёта.",
            "做友好的聊天对象。跟上我的话题，提出自然的问题，轻松把对话进行下去，不必写成报告。",
        ),
    ),
    Preset(
        id="stories",
        icon="book.closed",
        category="entertainment",
        subcategory="stories",
        description=_loc(
            "Writes stories and develops plots",
            "Пишет рассказы и развивает сюжеты",
            "写故事并推进情节",
        ),
        title=_loc("Stories", "Истории", "故事"),
        prompt=_loc(
            "Write a story and develop the plot with me. Ask about genre, characters, and "
            "length, then start a chapter I can continue.",
            "Напиши рассказ и развивай сюжет вместе со мной. Расспроси о жанре, героях и "
            "объёме, затем начни главу, которую я смогу продолжить.",
            "和我一起写故事、推进情节。先问类型、人物和篇幅，然后写一章让我续写。",
        ),
    ),
    Preset(
        id="games",
        icon="gamecontroller",
        category="entertainment",
        subcategory="games",
        description=_loc(
            "Invents challenges and entertainment",
            "Придумывает челленджи и развлечения",
            "发明挑战和小游戏",
        ),
        title=_loc("Games", "Игры", "游戏"),
        prompt=_loc(
            "Invent a challenge or a short game we can play in chat. Explain the rules in "
            "one pass and start the first round.",
            "Придумай челлендж или короткую игру, в которую можно сыграть в чате. Объясни "
            "правила за один раз и запусти первый раунд.",
            "发明一个能在聊天里玩的挑战或小游戏。一次说清规则，然后开始第一轮。",
        ),
    ),
)


def preset_catalog(locale: str) -> list[dict[str, Any]]:
    """Machine-readable catalog of prompt presets for the given locale (ADR-035, ADR-049 §2).

    Pure (no I/O): iterates the static ``_PRESETS`` registry in declaration order (= chip order)
    and returns a list of ``{id, title, icon, prompt, category, subcategory, description}``
    dicts. ``title``/``prompt``/``description`` are resolved for ``locale`` with a per-field EN
    fallback — an unknown locale or a field missing for the locale degrades to
    ``DEFAULT_PRESET_LOCALE`` (never an empty string on shipped presets). ``id``/``icon``/
    ``category``/``subcategory`` are locale-independent. Locale resolution itself is the
    router's concern (ADR-049 §3), not here.
    """
    return [
        {
            "id": p.id,
            "title": p.title.get(locale) or p.title[DEFAULT_PRESET_LOCALE],
            "icon": p.icon,
            "prompt": p.prompt.get(locale) or p.prompt[DEFAULT_PRESET_LOCALE],
            "category": p.category,
            "subcategory": p.subcategory,
            "description": (
                (p.description or {}).get(locale)
                or (p.description or {}).get(DEFAULT_PRESET_LOCALE)
                or ""
            ),
        }
        for p in _PRESETS
    ]
