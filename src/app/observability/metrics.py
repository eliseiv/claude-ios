"""Prometheus metrics (01-architecture.md#наблюдаемость)."""

from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

chat_run_latency_seconds = Histogram(
    "chat_run_latency_seconds",
    "Latency of chat orchestration (policy + orchestrator + db), excluding Anthropic.",
)
blocked_requests_total = Counter(
    "blocked_requests_total",
    "Count of business-blocked requests by reason.",
    ["reason"],
)
wallet_debit_total = Counter(
    "wallet_debit_total",
    "Count of wallet debit attempts by result.",
    ["result"],
)
tool_call_roundtrip_latency_seconds = Histogram(
    "tool_call_roundtrip_latency_seconds",
    "Latency from tool_call initiation to tool_result handling.",
)
byok_usage_share = Gauge(
    "byok_usage_share",
    "Share of chat requests using BYOK mode.",
)
token_usage_total = Counter(
    "token_usage_total",
    "Total tokens by direction and model.",
    ["direction", "model"],
)
# Admin (ADM-7): grant outcomes by result (success | conflict | not_found).
admin_grant_total = Counter(
    "admin_grant_total",
    "Count of admin credit-grant attempts by result.",
    ["result"],
)
# Token purchase (ADR-015): consumable purchase outcomes by result
# (granted | replay | unknown_product | invalid_transaction | forbidden).
token_purchase_total = Counter(
    "token_purchase_total",
    "Count of consumable token-purchase attempts by result.",
    ["result"],
)
# Website builder (WB-8).
site_file_write_total = Counter(
    "site_file_write_total",
    "Count of site.write_file tool executions by result.",
    ["result"],
)
preview_request_total = Counter(
    "preview_request_total",
    "Count of preview endpoint requests by result (ok | forbidden | not_found).",
    ["result"],
)
# quiz.generate outcomes (ADR-065 §3): bounded-enum label only, never quiz content.
# Required rather than nice-to-have: quiz.generate is the first tool whose contract EXPECTS
# failures and DESIGNS a retry, so a systematically malformed model burns up to
# MAX_SERVER_TOOL_ROUNDS upstream calls per turn, ends the turn with an error and debits NO credit
# — the operator pays and nothing else signals it (blocked_requests_total does not move: it is not
# a policy block; llm_upstream_errors_total does not move: upstream answers 200). Without this
# counter a degrading model is indistinguishable from silence.
quiz_generate_total = Counter(
    "quiz_generate_total",
    "Count of quiz.generate tool executions by result (ok | invalid_quiz | tool_not_available).",
    ["result"],
)
# Anthropic upstream errors (TD-014): bounded enum labels only (no user-content).
# status_code is the numeric HTTP status or "none" for timeout/connection errors;
# error_type is the Anthropic error.type (or "unknown" when the body has none).
# KEPT for existing dashboards/tests; the generalized provider-labeled metric below is the
# ADR-033 §10 unified series (both are incremented on the Anthropic path).
anthropic_upstream_errors_total = Counter(
    "anthropic_upstream_errors_total",
    "Count of Anthropic upstream errors by status_code and error_type.",
    ["status_code", "error_type"],
)
# Generalized LLM upstream errors (ADR-033 §10): provider-labeled unified series for both
# Anthropic and OpenAI. provider ∈ {anthropic, openai}; status_code is the numeric HTTP status or
# "none" for timeout/connection errors; error_type is the provider error.type / exception class
# (or "unknown"). Bounded enum labels only (no user-content).
llm_upstream_errors_total = Counter(
    "llm_upstream_errors_total",
    "Count of LLM upstream errors by provider, status_code and error_type.",
    ["provider", "status_code", "error_type"],
)
# Unpriceable chat step (ADR-079 §1, rule `None ≠ 0`): producer — `report_chat_step_pricing`
# (`app.pricing.provider_prices`), called from the chat WRITE path once per LLM call, next to
# `token_usage_total`; consumer — GET /metrics.
#
# The write path is the point of the placement, not an implementation detail: it is where a STEP
# happens, which is what the series counts. The CRM read path prices the same stored step on every
# render (and twice per card — row plus revenue roll-up), and reports nothing at all while no
# operator has CRM open — a fault signal that only fires when someone is already looking is not one.
#
# Required rather than nice-to-have: an unpriceable step makes the WHOLE turn's cost `None`, and
# the operator sees that as an empty «Себестоимость» cell — indistinguishable from "this instance
# has no chat traffic". Nothing else moves: the call succeeded, no credit was refused, no upstream
# error was raised. Without this series a model drifting out of the price table (a provider
# renaming its snapshot, an allowlist naming a model the table never heard of) is silent.
#
# `model` is the name as stored in `chat_steps.usage.model` — a provider model id, the same
# bounded set `token_usage_total` already labels by; "none" when the step carries no model name.
# `reason` ∈ {unknown_model | no_model | no_token_counts}.
chat_unpriced_steps_total = Counter(
    "chat_unpriced_steps_total",
    "Count of chat usage steps that have no purchase price, by model and reason (ADR-079).",
    ["model", "reason"],
)


def render_metrics() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST


# ADR-086 §10: producer — места вызова модерации (ChatOrchestrator.run,
# MediaGenerationService.submit / _advance / upload_reference_image); consumer — GET /metrics.
moderation_decisions_total = Counter(
    "moderation_decisions_total",
    "Moderation verdicts by surface/stage/decision (ADR-086)",
    ["surface", "stage", "decision"],
)
moderation_errors_total = Counter(
    "moderation_errors_total",
    "Moderation provider failures by reason (ADR-086)",
    ["reason"],
)

# ADR-100 §10: единственный счётчик исходов `POST /v1/chat/speech`.
# PRODUCER — точка формирования ответа в роутере (`app.api_gateway.routers.voices`), на РАБОЧЕМ
# пути; НЕ хелпер синтеза: инкремент в хелпере не покрыл бы отказы, до него не дошедшие
# (`disabled`, `not_configured`, `nothing_to_speak`). CONSUMER — панель расходов и алерт на
# устойчивую долю `upstream_error`.
#
# Классификация — по предикату «что случилось с деньгами пользователя», вычисляемому из
# наблюдаемых фактов пути, а не по суждению заполняющего:
#   ok               — синтез успешен И ключ идемпотентности создан ЭТИМ вызовом (списано);
#   repeat           — синтез успешен И ключ идемпотентности уже существовал (не тронуты);
#   nothing_to_speak — очищенный текст пуст (не тронуты);
#   upstream_error   — поставщик не вернул звук ⇒ списание не выполнялось (не тронуты);
#   timeout          — то же, по таймауту (не тронуты);
#   disabled         — флаг инстанса снят (не тронуты);
#   not_configured   — ключ пуст (не тронуты).
# Строки «списано, но не доставлено» здесь НЕТ: порядок §9 (списание после успешного синтеза, в
# той же транзакции) делает такой исход невозможным по построению; её появление было бы дефектом,
# а не новым случаем. Симметричный критерий против ПЕРЕОЦЕНКИ: `nothing_to_speak` и `disabled` —
# не аварии и в алерт не входят, иначе шум обесценит канал вместе с дорогим `upstream_error`.
# Балансовые и авторизационные отказы (409/404/403/429) счётчиком НЕ помечаются: они не про
# работу поставщика и не про деньги, потраченные нами.
speech_synthesis_total = Counter(
    "speech_synthesis_total",
    "Assistant speech-synthesis outcomes (ADR-100)",
    ["outcome"],
)


# ADR-104 §12. Три серии голосового режима; у каждой producer лежит на РАБОЧЕМ пути обработчика
# сокета, иначе серия была бы объявлена и никогда не заполнена.
#
# producer: точка `accept` и точка закрытия сокета (inc/dec); consumer: панель нагрузки,
# калибровка idle-таймаута (Q-104-1) и вопрос о лимите соединений (Q-104-2).
voice_mode_connections = Gauge(
    "voice_mode_connections",
    "Currently open /v1/chat/voice WebSocket connections (ADR-104).",
)
# producer: точка закрытия хода в обработчике сокета; consumer: доля прерванных ходов, алерт на
# `upstream_error`. Предикат отнесения вычисляется из НАБЛЮДАЕМЫХ фактов пути, не из суждения:
#   ok             — ход закрыт `done`, status ∈ {assistant_message, tool_call}, прерывания не было;
#   interrupted    — получен кадр `interrupt`, ход закрыт по ADR-104 §5;
#   blocked        — status="blocked" (policy, кредиты, max_tokens) — штатный бизнес-исход;
#   upstream_error — провайдер не вернул результат, ход закрыт пометкой turnFailed — АВАРИЯ;
#   disconnected   — сокет закрыт до `done`, ход доведён до конца — наблюдение, не авария.
# Против недооценки: `upstream_error` не сливается с `disconnected` — первое поломка у
# поставщика, второе штатная мобильная сеть. Против переоценки: `interrupted` — САМЫЙ ЧАСТЫЙ
# штатный исход голосового режима, и отнесение его к тревожным обесценило бы всю серию: шум
# внутри класса приучает игнорировать класс, и вместе с шумом перестают замечать `upstream_error`.
voice_mode_turns_total = Counter(
    "voice_mode_turns_total",
    "Voice-mode turn outcomes (ADR-104 §12).",
    ["outcome"],
)
# producer: точка отправки `audio.end` и точка отказа синтеза; consumer: доля `capped` →
# калибровка потолка и VOICE_MODE_SEGMENT_MIN_CHARS (Q-104-1), алерт на `upstream_error`.
# Предикат вычисляется из наблюдаемых фактов СЕГМЕНТА, а не хода:
#   ok             — `audio.end` сегмента отправлен, truncated: false;
#   capped         — совокупный TTS_MAX_CHARS хода исчерпан НА ЭТОМ КАНДИДАТЕ: либо сегмент
#                    отдан обрезанным (`audio.end` с truncated: true), либо не отдан вовсе
#                    (остаток бюджета ноль, синтезатор не вызывался). ОДИН инкремент на ход —
#                    дальнейшие кандидаты гасятся уже выставленным признаком (ADR-104 §13.8);
#   skipped_empty  — кандидат после чистки пуст, синтезатор НЕ вызывался;
#   rate_limited   — токен бакета `rl:speech` не выдан перед ПЕРВЫМ обращением этого шага;
#                    синтезатор не вызывался, `audio.end` не отправлен, ушёл
#                    `error {code:"rate_limited", scope:"speech"}`. ОДИН инкремент на погашенный
#                    ШАГ — единица бакета совпадает с единицей списания (ADR-104 §13.2);
#   interrupted    — синтез сегмента оборван кадром `interrupt`, `audio.end` НЕ отправлен;
#   upstream_error — синтезатор отказал на этом сегменте — АВАРИЯ.
# Против недооценки: `upstream_error` не сливается ни с `skipped_empty` (там синтезатор не звали —
# произносить было нечего), ни с `interrupted` (там отмену инициировал пользователь), ни с
# `rate_limited` (там поставщика НЕ ЗВАЛИ ВОВСЕ, и о его исправности мы ничего не знаем) — только
# он означает поломку поставщика синтеза, и только по нему строится алерт. Против переоценки:
# `capped` — ШТАТНЫЙ исход длинного ответа, ровно то, ради чего потолок и существует;
# `interrupted` — штатный и самый частый; `rate_limited` — РАБОТАЮЩАЯ ЗАЩИТА бюджета, а не
# поломка. Доля `capped` и доля `rate_limited` — продуктовые сигналы калибровки TTS_MAX_CHARS и
# TTS_RATE_LIMIT_PER_MIN (Q-104-1), а не алерты.
voice_mode_speech_segments_total = Counter(
    "voice_mode_speech_segments_total",
    "Voice-mode streaming-synthesis segment outcomes (ADR-104 §12).",
    ["outcome"],
)


# ADR-099 §10. У каждой метрики назван producer -> consumer; producer лежит на РАБОЧЕМ пути,
# иначе серия была бы объявлена и никогда не заполнена.
#
# producer: обновление снимка оверлеев (app.instance_config.snapshot); consumer: /metrics,
# отвечает на вопрос «правит ли кто-то этот инстанс из CRM».
admin_overrides_active = Gauge(
    "admin_overrides_active",
    "Number of operator overrides currently in effect, by scope (ADR-099).",
    ["scope"],
)
# producer: то же обновление снимка; consumer: алерт «фоновый обновитель умер». Возраст больше
# 3x окна означает, что правки оператора НЕ применяются, при том что запись проходит успешно.
admin_overrides_snapshot_age_seconds = Gauge(
    "admin_overrides_snapshot_age_seconds",
    "Age of the in-process instance-config snapshot in seconds (ADR-099).",
)
# producer: ветка отказа обновления снимка; consumer: разбор инцидента.
admin_overrides_refresh_failures_total = Counter(
    "admin_overrides_refresh_failures_total",
    "Failed instance-config snapshot refreshes by reason (ADR-099).",
    ["reason"],
)
# producer: валидация PATCH/POST admin-поверхности; consumer: разбор «CRM шлёт то, что мы
# отвергаем» — то есть расхождение нашего объявления с нашей же валидацией.
admin_override_rejected_total = Counter(
    "admin_override_rejected_total",
    "Rejected operator overrides by scope and reason (ADR-099).",
    ["scope", "reason"],
)
# producer: сборка каталога GET /v1/media/models; consumer: сигнал «выпущенные сборки
# показывают цену ВЫШЕ фактической». На дефолтной таблице серия равна НУЛЮ — это её нормативное
# состояние, а не «обычно ноль»: единица на нетронутом инстансе означает дефект вывода.
media_price_legacy_overquote = Gauge(
    "media_price_legacy_overquote",
    "1 when the legacy multiplier triple over-quotes at least one price cell (ADR-099 §4.4).",
    ["model"],
)
