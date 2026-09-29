"""Закрытие HTTP-пула пересозданного клиента ПОСЛЕ завершения идущих на нём вызовов (ADR-116 §5).

При смене ключа из CRM процессный клиент пересоздаётся, а прежний выводится из оборота. Закрыть
его пул сразу нельзя: вызов, уже идущий на нём, обязан завершиться на нём. Признак «вызовов на
прежнем клиенте больше нет» — отсутствие ссылок на объект-обёртку: идущий вызов держит её
кадром своего метода (``self``), держатель ответа — ссылкой. Когда последняя ссылка отпадает,
``weakref.finalize`` срабатывает, и закрытие пула ставится задачей в цикл событий, из которого
клиент выведен. Счётчика вызовов и таймера нет: ни гонки «закрыли под активным запросом», ни
утечки пула, пока обёртка жива.

⚠️ Колбэк ``finalize`` НЕ держит обёртку (иначе она не умерла бы никогда): он держит только
КОНТЕЙНЕР внутренних SDK-клиентов, который обёртка разделяет с ним. Контейнер читается в момент
срабатывания — клиент, лениво созданный обёрткой уже после вывода из оборота, тоже будет закрыт.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import weakref
from collections.abc import Callable
from typing import Any

logger = logging.getLogger("app.instance_config.client_retirement")

# Задачи закрытия держатся здесь до завершения: цикл событий хранит на задачу только слабую ссылку.
_pending_closes: set[asyncio.Task[None]] = set()


async def _close_all(clients: list[Any]) -> None:
    for client in clients:
        close = getattr(client, "close", None)
        if close is None:
            continue
        try:
            result = close()
            if asyncio.iscoroutine(result):
                await result
        except Exception:  # noqa: BLE001 — сбой закрытия прежнего пула не должен ронять процесс
            logger.warning("retired_client_close_failed", exc_info=True)


def _schedule_close(loop: asyncio.AbstractEventLoop, clients_of: Callable[[], list[Any]]) -> None:
    clients = [client for client in clients_of() if client is not None]
    if not clients or loop.is_closed():
        return

    def _spawn() -> None:
        task = loop.create_task(_close_all(clients))
        _pending_closes.add(task)
        task.add_done_callback(_pending_closes.discard)

    # `finalize` может сработать в любом потоке (сборщик мусора), поэтому задача передаётся в
    # цикл событий потокобезопасно.
    with contextlib.suppress(RuntimeError):  # цикл закрыт между проверкой и вызовом
        loop.call_soon_threadsafe(_spawn)


def retire_when_unreferenced(wrapper: object, clients_of: Callable[[], list[Any]]) -> None:
    """Закрыть внутренние клиенты ``wrapper``, когда на неё не останется ссылок.

    ``clients_of`` НЕ должен ссылаться на ``wrapper`` (замыкание по обёртке держало бы её живой):
    передавайте функцию над контейнером, который обёртка разделяет. Вне цикла событий (процесс
    завершается, синхронный тест) закрывать нечем — ничего не делается.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    weakref.finalize(wrapper, _schedule_close, loop, clients_of)


def sdk_clients_of(wrapper: object, *attributes: str) -> Callable[[], list[Any]]:
    """Функция чтения внутренних клиентов, НЕ удерживающая обёртку.

    Захватывает ``__dict__`` обёртки (сам словарь, а не обёртку): атрибуты, присвоенные позже,
    видны в момент срабатывания, а словарь живёт после смерти обёртки, пока на него ссылается
    финализатор.
    """
    namespace = vars(wrapper)
    return lambda: [namespace.get(attribute) for attribute in attributes]
