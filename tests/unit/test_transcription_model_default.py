"""Дефолт распознавания речи — не `whisper-1`.

Замер на проде 2026-09-09 (avelyra, одна и та же трёхсекундная запись, по два прогона):
`whisper-1` — 1.10 и 1.20 с; `gpt-4o-mini-transcribe` — 0.59 и 0.63 с; `gpt-4o-transcribe` —
0.68 и 0.86 с. Текст у mini совпал с эталоном точнее.

Страж от тихого отката: значение читается из настроек ОДИН раз при создании клиента, поэтому
возврат к медленной модели не уронил бы ни один тест и не покраснил бы ни один гейт — он просто
удвоил бы задержку у каждого голосового сообщения.
"""

from __future__ import annotations

from app.config import Settings


def test_default_transcription_model_is_not_whisper_1() -> None:
    assert Settings().transcription_model != "whisper-1"


def test_default_transcription_model_is_the_measured_one() -> None:
    assert Settings().transcription_model == "gpt-4o-mini-transcribe"


def test_transcription_model_is_still_overridable_per_instance() -> None:
    """Оператор обязан мочь вернуть прежнюю модель на одном инстансе без выката."""
    assert Settings(TRANSCRIPTION_MODEL="whisper-1").transcription_model == "whisper-1"
