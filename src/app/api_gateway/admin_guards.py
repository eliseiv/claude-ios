"""Гейты admin-поверхности, общие для всех её роутеров (ADR-009 §6, ADR-099 §10.1).

Модуль отдельный, потому что гейт нужен ДВУМ роутерам сразу (`admin.py` и вложенному
`crm_admin.py`), а `admin.py` включает `crm_admin.py` — объявление в любом из них сделало бы
импорт односторонним и подтолкнуло ко второй копии. Копия здесь опаснее обычного: гейт защищает
границу, и разошедшиеся копии дали бы разные границы на соседних ручках одной поверхности.
"""

from __future__ import annotations

from fastapi import Request

from app.config import get_settings
from app.errors import PayloadTooLargeError


def enforce_admin_body_size(request: Request) -> None:
    """Отвергнуть admin-тело сверх строгого предела поверхности (≤ 8 КБ) → ``413``.

    ⚠️ Это НЕ middleware, а явный вызов в теле хендлера — ровно как лимит частоты: путь, где
    его забыли, остаётся без предела МОЛЧА, без ошибки и без лога. Поэтому вызов обязателен на
    каждой пишущей ручке поверхности, а не «там, где тело выглядит большим».

    Заголовок отсутствует или не число — проверка пропускается: транспортный предел остаётся за
    общим middleware, здесь мы лишь ужесточаем его для admin-поверхности.
    """
    content_length = request.headers.get("content-length")
    if content_length is None:
        return
    try:
        declared = int(content_length)
    except ValueError:
        return
    if declared > get_settings().admin_size_limit_body:
        raise PayloadTooLargeError("admin request body exceeds limit")
