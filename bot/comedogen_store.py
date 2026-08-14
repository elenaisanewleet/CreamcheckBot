# bot/comedogen_store.py
"""Правки базы комедогенов поверх неизменяемого эталона.

Эталонные списки живут в `agent/comedogen_base.py` и кодом не меняются никогда.
Правки врача складываются отдельным слоем в SQLite, а рабочая база собирается
как «эталон плюс правки».

Зачем так, а не редактировать списки напрямую:

  • эталон нельзя испортить — «Сбросить к эталону» возвращает выверенный
    список в любой момент;
  • каждая правка подписана автором и временем, любую можно отменить;
  • если том с данными потеряется, бот вернётся к эталону, а не останется
    без базы вообще.

Пока правок нет, `effective()` отдаёт ровно эталонные объекты — оценка риска
работает в точности как до появления этого модуля.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

from agent.comedogen_base import conditional_comedogens, hard_comedogens

from . import analytics, config

logger = logging.getLogger(__name__)

# Значение по умолчанию для добавленных условных компонентов: «ранняя позиция»
# в эталоне у всех одинаковая, поэтому новым присваиваем ту же.
DEFAULT_CUTOFF = 5

KIND_HARD = "hard"
KIND_COND = "conditional"
KINDS = (KIND_HARD, KIND_COND)

ACTION_ADD = "add"
ACTION_DROP = "drop"

BASELINE_HARD: FrozenSet[str] = frozenset(hard_comedogens)
BASELINE_COND: Dict[str, int] = dict(conditional_comedogens)

# Собранная база держится в памяти: classify_ingredient_strict вызывается
# на каждый ингредиент, ходить в SQLite на каждый — расточительно.
_cache: Optional[Tuple[FrozenSet[str], Dict[str, int]]] = None


def normalize(name: str) -> str:
    return " ".join((name or "").strip().lower().split())


def invalidate() -> None:
    """Сбрасывает собранную базу; следующий вызов effective() пересоберёт её."""
    global _cache
    _cache = None


def enabled() -> bool:
    """Правки требуют базы данных: без неё их негде хранить."""
    return bool(config.ANALYTICS_ENABLED)


def _overrides() -> List[Dict[str, Any]]:
    """Активные правки. При недоступной базе _query вернёт пустой список,
    и бот поедет на эталоне — это безопасное поведение по умолчанию."""
    if not enabled():
        return []
    rows = analytics._query(
        "SELECT id, name, kind, action, author, author_id, ts "
        "FROM base_overrides WHERE active = 1 ORDER BY ts"
    )
    return [dict(r) for r in rows]


def effective() -> Tuple[FrozenSet[str], Dict[str, int]]:
    """Рабочая база: эталон плюс активные правки."""
    global _cache
    if _cache is not None:
        return _cache

    rows = _overrides()
    if not rows:
        # Ни одной правки — отдаём эталон как есть.
        _cache = (BASELINE_HARD, BASELINE_COND)
        return _cache

    hard = set(BASELINE_HARD)
    cond = dict(BASELINE_COND)

    for row in rows:
        name = normalize(row.get("name") or "")
        if not name:
            continue
        kind = row.get("kind")
        action = row.get("action")

        if kind == KIND_HARD:
            hard.add(name) if action == ACTION_ADD else hard.discard(name)
        elif kind == KIND_COND:
            if action == ACTION_ADD:
                cond[name] = DEFAULT_CUTOFF
            else:
                cond.pop(name, None)

    _cache = (frozenset(hard), cond)
    return _cache


def stats() -> Dict[str, Any]:
    hard, cond = effective()
    rows = _overrides()
    return {
        "baseline_hard": len(BASELINE_HARD),
        "baseline_cond": len(BASELINE_COND),
        "hard": len(hard),
        "cond": len(cond),
        "added": sum(1 for r in rows if r.get("action") == ACTION_ADD),
        "dropped": sum(1 for r in rows if r.get("action") == ACTION_DROP),
        "overrides": len(rows),
    }


def is_baseline(name: str, kind: str) -> bool:
    n = normalize(name)
    return n in BASELINE_HARD if kind == KIND_HARD else n in BASELINE_COND


def contains(name: str, kind: str) -> bool:
    hard, cond = effective()
    n = normalize(name)
    return n in hard if kind == KIND_HARD else n in cond


async def _write(sql: str, params: Any) -> None:
    await analytics._run(analytics._exec, sql, params)


async def _record(name: str, kind: str, action: str, user: Any) -> None:
    await _write(
        "INSERT INTO base_overrides (name, kind, action, author, author_id, ts, active) "
        "VALUES (?, ?, ?, ?, ?, ?, 1)",
        (
            normalize(name),
            kind,
            action,
            getattr(user, "full_name", None) or getattr(user, "username", None) or "—",
            getattr(user, "id", None),
            time.time(),
        ),
    )
    await _applied()


async def _applied() -> None:
    """После любой правки база пересобирается, а кэш разборов сбрасывается.

    В кэше лежат готовые вердикты, посчитанные по прежней базе. Не сбросить
    их — значит показывать людям устаревшую оценку риска до истечения TTL.
    """
    invalidate()
    removed = await analytics.cache_clear()
    logger.info("База комедогенов изменена, сброшено записей кэша: %d", removed)


async def add(name: str, kind: str, user: Any = None) -> str:
    """Добавляет компонент. Возвращает код результата для сообщения админу."""
    if kind not in KINDS:
        return "bad_kind"
    n = normalize(name)
    if not n:
        return "empty"
    if contains(n, kind):
        return "exists"

    # Если компонент когда-то убирали, добавление — это отмена того удаления.
    await _write(
        "UPDATE base_overrides SET active = 0 "
        "WHERE active = 1 AND name = ? AND kind = ? AND action = ?",
        (n, kind, ACTION_DROP),
    )
    if is_baseline(n, kind):
        await _applied()
        return "restored"

    await _record(n, kind, ACTION_ADD, user)
    return "added"


async def drop(name: str, kind: str, user: Any = None) -> str:
    if kind not in KINDS:
        return "bad_kind"
    n = normalize(name)
    if not contains(n, kind):
        return "missing"

    # Убрать своё же добавление — не правка, а её отмена.
    await _write(
        "UPDATE base_overrides SET active = 0 "
        "WHERE active = 1 AND name = ? AND kind = ? AND action = ?",
        (n, kind, ACTION_ADD),
    )
    if not is_baseline(n, kind):
        await _applied()
        return "reverted"

    await _record(n, kind, ACTION_DROP, user)
    return "dropped"


async def undo(override_id: int) -> bool:
    if not enabled():
        return False
    row = await analytics._run(
        analytics._one, "SELECT id FROM base_overrides WHERE id = ? AND active = 1", (override_id,)
    )
    if not row:
        return False
    await _write("UPDATE base_overrides SET active = 0 WHERE id = ?", (override_id,))
    # Сброс кэша — часть правки, а не забота вызывающего: пока про него помнили
    # админка и дашборд по отдельности, любой третий вход показывал бы людям
    # вердикты, посчитанные по прежней базе.
    await _applied()
    return True


async def reset() -> int:
    """Отменяет все правки разом. Эталон при этом никак не затрагивается."""
    rows = _overrides()
    if rows:
        await _write("UPDATE base_overrides SET active = 0 WHERE active = 1", ())
        await _applied()
    return len(rows)


def history(limit: int = 20) -> List[Dict[str, Any]]:
    if not enabled():
        return []
    rows = analytics._query(
        "SELECT id, name, kind, action, author, ts, active FROM base_overrides "
        "ORDER BY ts DESC LIMIT ?",
        (int(limit),),
    )
    return [dict(r) for r in rows]


def listing(kind: str) -> List[Dict[str, Any]]:
    """Компоненты выбранного вида с пометкой, откуда каждый взялся."""
    hard, cond = effective()
    names = sorted(hard if kind == KIND_HARD else cond)
    return [{"name": n, "baseline": is_baseline(n, kind)} for n in names]
