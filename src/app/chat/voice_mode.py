"""Ось включения голосового режима — ОДНА точка вычисления (ADR-104 §8).

Ось СОСТАВНАЯ: `VOICE_MODE_ENABLED` **и** `VOICE_INPUT_ENABLED` **и** `VOICE_OUTPUT_ENABLED`.
Голосовой режим — это вход **и** выход; включённый там, где выключена любая половина, он дал бы
сокет, который не слышит или молчит. Поэтому условие вычисляется здесь, а не проверяется в трёх
местах: три проверки расходятся на первой же правке одной из них.

**Три флага разрешаются СЕГОДНЯ двумя разными механизмами, и это названо, а не умолчано.**
`VOICE_INPUT_ENABLED` объявлен в реестре настроек инстанса как `chat.voice_input_enabled` и
разрешается порядком «оверлей → env → дефолт кода» (ADR-099 §2), то есть меняется из панели на
лету. `VOICE_OUTPUT_ENABLED` и `VOICE_MODE_ENABLED` читаются прямым `Settings` и правятся только
`.env` + рестарт (Q-099-5, TD-047). Названное следствие: оператор, снявший голосовой ввод из
панели, гасит голосовой режим немедленно; включить сам режим он из панели не может. Вычисление в
одной точке эту разницу не устраняет и не обязано: оно устраняет расхождение трёх ПРОВЕРОК, а не
разницу их ИСТОЧНИКОВ.
"""

from __future__ import annotations

from app import instance_config
from app.config import Settings, get_settings

# Порядок имён фиксирован: он же порядок в WARNING `voice_mode_misconfigured`, по которому
# оператор читает, чего именно не хватает.
FLAG_VOICE_MODE = "VOICE_MODE_ENABLED"
FLAG_VOICE_INPUT = "VOICE_INPUT_ENABLED"
FLAG_VOICE_OUTPUT = "VOICE_OUTPUT_ENABLED"


def voice_mode_missing_flags(*, settings: Settings | None = None) -> list[str]:
    """Какие из трёх флагов оси СНЯТЫ. Пустой список ⇔ голосовой режим доступен.

    Возвращается перечень, а не булево, потому что диагностика «включил, а не работает» обязана
    читаться по логу, а не по переписке (ADR-104 §8): WARNING `voice_mode_misconfigured` несёт
    именно этот список. Булев ответ даёт `voice_mode_available()` ниже — второго определения
    условия при этом не появляется, оно выводится из этого перечня.
    """
    resolved = settings if settings is not None else get_settings()
    missing: list[str] = []
    if not resolved.voice_mode_enabled:
        missing.append(FLAG_VOICE_MODE)
    # Половина ВВОДА резолвится через реестр настроек инстанса (оверлей → env → дефолт), поэтому
    # читается тем же аксессором, что и голосовой ввод на HTTP-пути, — не сырым `Settings`.
    if not instance_config.voice_input_enabled(settings=resolved):
        missing.append(FLAG_VOICE_INPUT)
    if not resolved.voice_output_enabled:
        missing.append(FLAG_VOICE_OUTPUT)
    return missing


def voice_mode_available(*, settings: Settings | None = None) -> bool:
    """Доступен ли на этом инстансе живой голосовой диалог `/v1/chat/voice` (ADR-104 §8).

    Это же значение отдаётся полем `voiceModeEnabled` в `GET /v1/voices` — единственным способом
    для приложения узнать, показывать ли кнопку голосового режима. Второго каталога и второй
    ручки под один флаг не заводится.
    """
    return not voice_mode_missing_flags(settings=settings)
