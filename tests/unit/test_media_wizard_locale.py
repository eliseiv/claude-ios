"""Язык ответа мастера генерации и граница «показать ≠ создать».

Оба дефекта найдены на проде 2026-09-02 и живут в одном месте — в пути выбора параметров
генерации, который отвечает БЕЗ обращения к модели.

1. ЯЗЫК. Тексты этого пути собирает сам сервер, поэтому `locale` из context — тот самый, что на
   обычном ходе доносит язык до модели, — здесь не действовал вовсе: клиент передавал русскую
   локаль, а ответ приходил по-английски, и повлиять на это было нечем.
2. НАМЕРЕНИЕ. Условие вызова мастера было сформулировано через ЖЕЛАНИЕ («хочет фото»), а не через
   намерение создать. «Найди фото котиков» под него подпадало и открывало карточку ПЛАТНЫХ
   моделей — человек оказывался в оплате, ничего не подтвердив.
"""

from __future__ import annotations

import pytest

from app.chat.orchestrator import _MEDIA_GENERATE_INSTRUCTION, _media_wizard_text
from app.chat.tools import TOOL_DESCRIPTIONS


@pytest.mark.parametrize("locale", ["ru", "ru-RU", "ru_RU", "RU"])
def test_russian_is_recognized_in_every_form_the_client_may_send(locale: str) -> None:
    """Сопоставление по префиксу, а не по точному равенству.

    Клиент присылает BCP-47-подобное значение, и точное сравнение промахнулось бы на двух
    формах из трёх — ответ молча уезжал бы в английский.
    """
    assert "Генерация запущена" in _media_wizard_text("started", locale, credits=8)
    assert _media_wizard_text("next", locale) == "Выберите следующий вариант."


@pytest.mark.parametrize("locale", ["en", "en-US", None, "", "de", "zz"])
def test_unknown_language_falls_back_to_english_not_to_a_crash(locale: str | None) -> None:
    """Неизвестный язык — английский. Ключевое слово «not to a crash»: таблица никогда не
    покрывает все локали мира, и отсутствующая обязана давать текст, а не исключение."""
    assert "Generation started" in _media_wizard_text("started", locale, credits=8)


def test_credits_land_in_the_message() -> None:
    """Число кредитов подставляется, а не теряется при локализации."""
    assert "(8 кр.)" in _media_wizard_text("started", "ru", credits=8)
    assert "(8 cr.)" in _media_wizard_text("started", "en", credits=8)


def test_showing_is_not_creating() -> None:
    """Просьба ПОКАЗАТЬ не должна открывать платный выбор модели.

    Проверяется наличие границы в обоих местах сразу: инструкция и описание инструмента —
    два независимых источника для модели, и правка одного без другого оставляет противоречие,
    которое модель разрешает по своему усмотрению.
    """
    for text in (_MEDIA_GENERATE_INSTRUCTION, TOOL_DESCRIPTIONS["media.ask_params"]):
        assert "CREATE" in text, "нет требования намерения создать"
        lowered = text.lower()
        assert "show" in lowered and "find" in lowered, "не названы глаголы показа/поиска"
        assert "paid" in lowered, "не сказано, что мастер открывает платное действие"
