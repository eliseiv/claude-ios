"""Формат заплатки `files.patch`: отбраковка на сервере вместо отказа на машине человека.

Прод 2026-08-31, инстанс `fanappsnew`: ВОСЕМЬ вызовов из восьми провалились. Модель выдавала
человекочитаемый набросок — `@@` без диапазонов, — клиент отдавал его системному `patch(1)`, тот
отвечал «I can't seem to find a patch in there anywhere», и модель, увидев отказ ИСПОЛНЕНИЯ,
переставала пытаться и диктовала правку словами. Со стороны это выглядело как «сервис не умеет
менять файлы», хотя инструменты предлагались и вызывались.

Требование выведено из ПОВЕДЕНИЯ утилиты, а не из описания формата: проверено, что `patch`
принимает кусок без заголовков `---`/`+++` (путь идёт отдельным аргументом) и отвергает
заголовок куска без диапазонов. Тест закрепляет обе границы — иначе запрет либо не поймает
набросок, либо отсечёт годную заплатку.
"""

from __future__ import annotations

import pytest

from app.chat.tools import (
    ARGS_DEGRADE_TOOLS,
    PATCH_FORMAT_HINT,
    TOOL_FILES_PATCH,
    validate_tool_args,
)

# Дословно то, что модель прислала на проде.
_SKETCH = "@@\n-    let a = 1\n+    let a = 2"


def _args(patch: str) -> dict[str, str]:
    return {"path": "/Users/me/proj/App.swift", "patch": patch}


def test_production_sketch_is_rejected() -> None:
    with pytest.raises(ValueError):
        validate_tool_args(TOOL_FILES_PATCH, _args(_SKETCH))


@pytest.mark.parametrize(
    "patch",
    [
        # Без заголовков файла — `patch(1)` это принимает, значит и мы обязаны.
        "@@ -2,4 +2,4 @@\n struct App {\n-    let a = 1\n+    let a = 2\n     let b = 2\n",
        # С заголовками файла.
        "--- a/App.swift\n+++ b/App.swift\n@@ -2,4 +2,4 @@\n-    let a = 1\n+    let a = 2\n",
        # Счётчик строк необязателен: односрочный кусок — валидный кусок.
        "@@ -12 +12 @@\n-old\n+new\n",
        # Несколько кусков в одной заплатке.
        "@@ -1,3 +1,3 @@\n-a\n+b\n@@ -20,3 +20,3 @@\n-c\n+d\n",
    ],
)
def test_real_unified_diffs_are_accepted(patch: str) -> None:
    assert validate_tool_args(TOOL_FILES_PATCH, _args(patch))["patch"] == patch


def test_rejection_tells_the_model_how_to_fix_it() -> None:
    """Отказ без подсказки бесполезен: модель узнает, что аргумент плох, но не чем именно."""
    with pytest.raises(ValueError, match="hunk header"):
        validate_tool_args(TOOL_FILES_PATCH, _args(_SKETCH))
    assert "@@ -12,7 +12,8 @@" in PATCH_FORMAT_HINT, "в подсказке нет образца формата"


def test_bad_patch_does_not_kill_the_turn() -> None:
    """Негодная заплатка — ОЖИДАЕМЫЙ исход, а не аномалия схемы.

    Вне этого набора кривые аргументы роняют ВЕСЬ ход в 422: пользователь остаётся без ответа,
    а модель — без возможности переслать заплатку. Здесь она получает отказ в том же ходе.
    """
    assert TOOL_FILES_PATCH in ARGS_DEGRADE_TOOLS


def test_hint_does_not_send_the_model_counting_lines() -> None:
    """Точность номеров не требуется — и подсказка обязана это сказать.

    Проверено на GNU patch: кусок с заголовком `@@ -40,3 +40,3 @@` применился к строке 5
    («Hunk #1 succeeded at 5, offset -35 lines») — место находится по КОНТЕКСТУ. Если не сказать
    этого прямо, модель тратит ход на пересчёт строк и всё равно ошибается; при этом ровно те
    усилия нужны в другом месте — в дословном цитировании соседних строк.
    """
    assert "need NOT be exact" in PATCH_FORMAT_HINT
    assert "context" in PATCH_FORMAT_HINT.lower()
