"""Обвязка над aiogram: ответ вместо тишины.

Сегодняшний прод показал, чем оборачивается упавший обработчик: aiogram
уносит исключение в лог и живёт дальше, а человек видит погасшую кнопку
и ни одного слова. Разбор при этом исправен, метрики зелёные, жалоба
приходит от пользователя.

Здесь две функции, которыми это закрывается, — общие для обычных экранов
и для админки, чтобы гарантия была одна на всех, а не переписывалась заново
в каждом модуле.
"""

from __future__ import annotations

import functools
import logging
from typing import Any, Awaitable, Callable

from . import analytics

logger = logging.getLogger(__name__)


async def quiet(request: Awaitable[Any]) -> Any:
    """Служебная отправка, сбой которой не должен отменять ответ по существу.

    Часики на кнопке и плашка «разбираю» — украшение. Telegram отказывает
    в них штатно: «query is too old», флуд-контроль, закрытый чат. Раньше
    такой отказ уносил весь обработчик, и экран не приходил из-за погасшей
    анимации.
    """
    try:
        return await request
    except Exception as exc:  # noqa: BLE001
        logger.debug("Telegram отказал в служебном сообщении: %s", exc)
        return None


def never_silent(error_text: str) -> Callable:
    """Гарантирует, что нажатие кнопки закончится словами, а не тишиной."""

    def decorator(handler: Callable) -> Callable:
        @functools.wraps(handler)
        async def wrapper(cb: Any, *args: Any, **kwargs: Any) -> Any:
            try:
                return await handler(cb, *args, **kwargs)
            except Exception as exc:  # noqa: BLE001
                logger.error("CALLBACK ERROR %s: %s", handler.__name__, exc, exc_info=True)
                await quiet(cb.answer())
                message = getattr(cb, "message", None)
                if message is not None:
                    await quiet(message.answer(error_text))
                await analytics.track(
                    kind="callback",
                    user=getattr(cb, "from_user", None),
                    chat_id=getattr(getattr(message, "chat", None), "id", None),
                    status="error",
                    detail=f"{handler.__name__}: {type(exc).__name__}: {exc}",
                )

        return wrapper

    return decorator
