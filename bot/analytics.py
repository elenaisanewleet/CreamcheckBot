"""Аналитика, логи и кэш результатов CreamcheckBot.

Хранилище — SQLite (стандартная библиотека, никаких новых зависимостей).
Главный принцип: аналитика НИКОГДА не должна ронять бота. Любая ошибка здесь
логируется и глотается — пользователь этого не замечает.

Что собираем:
  • events   — поток событий: кто, когда, что нажал, сколько заняло, сколько стоило
  • users    — карточка пользователя: первый/последний визит, счётчики по функциям
  • cache    — готовые ответы шага 1/шага 2 (ускорение + экономия на API)
  • url_ok   — результат проверки source_url (чтобы не ходить в сеть повторно)
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import os
import sqlite3
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, Deque, Dict, List, Optional, Sequence

from . import config

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# Кольцевой буфер логов (для /logs и дашборда)
# ─────────────────────────────────────────────────────────────

LOG_RING: Deque[str] = deque(maxlen=max(50, config.LOG_RING_SIZE))


class RingBufferHandler(logging.Handler):
    """Держит последние N строк лога в памяти — чтобы показывать их без SSH."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            LOG_RING.append(self.format(record))
        except Exception:  # noqa: BLE001 — логгер не имеет права падать
            pass


def recent_logs(limit: int = 200) -> List[str]:
    items = list(LOG_RING)
    return items[-limit:]


def setup_logging() -> None:
    """Консоль + файл с ротацией + кольцевой буфер в памяти."""
    root = logging.getLogger()
    root.setLevel(getattr(logging, config.LOG_LEVEL, logging.INFO))

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if not any(isinstance(h, logging.StreamHandler) for h in root.handlers):
        stream = logging.StreamHandler()
        stream.setFormatter(fmt)
        root.addHandler(stream)
    else:
        for h in root.handlers:
            h.setFormatter(fmt)

    ring = RingBufferHandler()
    ring.setFormatter(fmt)
    root.addHandler(ring)

    if config.LOG_FILE:
        try:
            os.makedirs(os.path.dirname(config.LOG_FILE) or ".", exist_ok=True)
            from logging.handlers import RotatingFileHandler

            fh = RotatingFileHandler(
                config.LOG_FILE,
                maxBytes=config.LOG_FILE_MAX_BYTES,
                backupCount=config.LOG_FILE_BACKUPS,
                encoding="utf-8",
            )
            fh.setFormatter(fmt)
            root.addHandler(fh)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Не удалось включить файловый лог %s: %s", config.LOG_FILE, exc)

    # aiogram/httpx болтливы на DEBUG — приглушаем, чтобы поток событий читался
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


# ─────────────────────────────────────────────────────────────
# Соединение с SQLite
# ─────────────────────────────────────────────────────────────

_conn: Optional[sqlite3.Connection] = None
_lock = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            REAL    NOT NULL,
    day           TEXT    NOT NULL,
    user_id       INTEGER,
    username      TEXT,
    full_name     TEXT,
    chat_id       INTEGER,
    kind          TEXT    NOT NULL,
    status        TEXT,
    source        TEXT,
    product       TEXT,
    risk          TEXT,
    latency_ms    INTEGER DEFAULT 0,
    cached        INTEGER DEFAULT 0,
    model         TEXT,
    in_tokens     INTEGER DEFAULT 0,
    cached_tokens INTEGER DEFAULT 0,
    out_tokens    INTEGER DEFAULT 0,
    tool_calls    INTEGER DEFAULT 0,
    cost_usd      REAL    DEFAULT 0,
    detail        TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts   ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_day  ON events(day);
CREATE INDEX IF NOT EXISTS idx_events_user ON events(user_id);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind);

CREATE TABLE IF NOT EXISTS users (
    user_id      INTEGER PRIMARY KEY,
    username     TEXT,
    full_name    TEXT,
    first_seen   REAL,
    last_seen    REAL,
    requests     INTEGER DEFAULT 0,
    photos       INTEGER DEFAULT 0,
    texts        INTEGER DEFAULT 0,
    compositions INTEGER DEFAULT 0,
    details      INTEGER DEFAULT 0,
    errors       INTEGER DEFAULT 0,
    cost_usd     REAL    DEFAULT 0
);

CREATE TABLE IF NOT EXISTS cache (
    key      TEXT PRIMARY KEY,
    kind     TEXT,
    payload  TEXT,
    created  REAL,
    hits     INTEGER DEFAULT 0,
    last_hit REAL
);
CREATE INDEX IF NOT EXISTS idx_cache_created ON cache(created);

CREATE TABLE IF NOT EXISTS url_ok (
    url     TEXT PRIMARY KEY,
    ok      INTEGER,
    checked REAL
);

-- Правки базы комедогенов поверх эталона из agent/comedogen_base.py.
-- Записи не удаляются: отмена правки — это active = 0, чтобы история
-- изменений оценки риска сохранялась целиком. Подробности в comedogen_store.py.
CREATE TABLE IF NOT EXISTS base_overrides (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    name      TEXT NOT NULL,
    kind      TEXT NOT NULL,   -- hard | conditional
    action    TEXT NOT NULL,   -- add | drop
    author    TEXT,
    author_id INTEGER,
    ts        REAL,
    active    INTEGER DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_overrides_active ON base_overrides(active);
"""


def _connect() -> Optional[sqlite3.Connection]:
    global _conn
    if _conn is not None:
        return _conn
    try:
        path = config.ANALYTICS_DB
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        conn = sqlite3.connect(path, check_same_thread=False, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(_SCHEMA)
        conn.commit()
        _conn = conn
        logger.info("Аналитика: база готова (%s)", path)
        return _conn
    except Exception as exc:  # noqa: BLE001
        logger.error("Аналитика отключена, база недоступна: %s", exc)
        return None


def _exec(sql: str, params: Sequence[Any] = ()) -> None:
    conn = _connect()
    if conn is None:
        return
    with _lock:
        conn.execute(sql, params)
        conn.commit()


def _query(sql: str, params: Sequence[Any] = ()) -> List[sqlite3.Row]:
    conn = _connect()
    if conn is None:
        return []
    with _lock:
        cur = conn.execute(sql, params)
        return cur.fetchall()


def _one(sql: str, params: Sequence[Any] = ()) -> Optional[sqlite3.Row]:
    rows = _query(sql, params)
    return rows[0] if rows else None


async def _run(fn, *args, **kwargs):
    """Выполнить синхронную работу с БД в потоке, не блокируя event loop."""
    if not config.ANALYTICS_ENABLED:
        return None
    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Аналитика: ошибка (%s): %s", getattr(fn, "__name__", fn), exc)
        return None


def init() -> None:
    """Инициализация при старте (создание таблиц + чистка старого)."""
    if not config.ANALYTICS_ENABLED:
        logger.info("Аналитика выключена (ANALYTICS_ENABLED=false)")
        return
    _connect()
    _cleanup()


def _cleanup() -> None:
    try:
        if config.ANALYTICS_RETENTION_DAYS > 0:
            edge = time.time() - config.ANALYTICS_RETENTION_DAYS * 86400
            _exec("DELETE FROM events WHERE ts < ?", (edge,))
        ttl = config.CACHE_TTL_DAYS * 86400
        if ttl > 0:
            _exec("DELETE FROM cache WHERE created < ?", (time.time() - ttl,))
        url_ttl = config.URL_CACHE_TTL_HOURS * 3600
        if url_ttl > 0:
            _exec("DELETE FROM url_ok WHERE checked < ?", (time.time() - url_ttl,))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Аналитика: чистка не удалась: %s", exc)


# ─────────────────────────────────────────────────────────────
# Запись событий
# ─────────────────────────────────────────────────────────────

# Какие поля users увеличивать для какого типа события
_USER_COUNTERS = {
    "photo": ("requests", "photos"),
    "album": ("requests", "photos"),
    "text": ("requests", "texts"),
    "composition": ("compositions",),
    "step2": ("details",),
}


def estimate_cost(
    in_tokens: int = 0,
    cached_tokens: int = 0,
    out_tokens: int = 0,
    tool_calls: int = 0,
) -> float:
    """Оценка стоимости запроса в USD по ценам из config."""
    fresh_in = max(0, int(in_tokens) - int(cached_tokens))
    cost = (
        fresh_in / 1_000_000 * config.PRICE_IN_PER_1M
        + int(cached_tokens) / 1_000_000 * config.PRICE_CACHED_IN_PER_1M
        + int(out_tokens) / 1_000_000 * config.PRICE_OUT_PER_1M
        + int(tool_calls) / 1_000 * config.PRICE_WEB_SEARCH_PER_1K
    )
    return round(cost, 6)


def _write_event(row: Dict[str, Any]) -> None:
    _exec(
        """
        INSERT INTO events (ts, day, user_id, username, full_name, chat_id, kind, status,
                            source, product, risk, latency_ms, cached, model,
                            in_tokens, cached_tokens, out_tokens, tool_calls, cost_usd, detail)
        VALUES (:ts, :day, :user_id, :username, :full_name, :chat_id, :kind, :status,
                :source, :product, :risk, :latency_ms, :cached, :model,
                :in_tokens, :cached_tokens, :out_tokens, :tool_calls, :cost_usd, :detail)
        """,
        row,
    )

    uid = row.get("user_id")
    if not uid:
        return

    conn = _connect()
    if conn is None:
        return
    with _lock:
        conn.execute(
            """
            INSERT INTO users (user_id, username, full_name, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username  = COALESCE(excluded.username, users.username),
                full_name = COALESCE(excluded.full_name, users.full_name),
                last_seen = excluded.last_seen
            """,
            (uid, row.get("username"), row.get("full_name"), row["ts"], row["ts"]),
        )
        for field in _USER_COUNTERS.get(row.get("kind") or "", ()):
            conn.execute(f"UPDATE users SET {field} = {field} + 1 WHERE user_id = ?", (uid,))
        if row.get("status") == "error":
            conn.execute("UPDATE users SET errors = errors + 1 WHERE user_id = ?", (uid,))
        if row.get("cost_usd"):
            conn.execute(
                "UPDATE users SET cost_usd = cost_usd + ? WHERE user_id = ?",
                (row["cost_usd"], uid),
            )
        conn.commit()


async def track(
    *,
    kind: str,
    user: Any = None,
    chat_id: Optional[int] = None,
    status: str = "ok",
    source: Optional[str] = None,
    product: Optional[str] = None,
    risk: Optional[str] = None,
    latency_ms: int = 0,
    cached: bool = False,
    model: Optional[str] = None,
    in_tokens: int = 0,
    cached_tokens: int = 0,
    out_tokens: int = 0,
    tool_calls: int = 0,
    cost_usd: Optional[float] = None,
    detail: Optional[str] = None,
) -> None:
    """Записать событие. Никогда не бросает исключений."""
    now = time.time()
    if cost_usd is None:
        cost_usd = estimate_cost(in_tokens, cached_tokens, out_tokens, tool_calls)

    row = {
        "ts": now,
        "day": datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d"),
        "user_id": getattr(user, "id", None),
        "username": getattr(user, "username", None),
        "full_name": getattr(user, "full_name", None),
        "chat_id": chat_id,
        "kind": kind,
        "status": status,
        "source": source,
        "product": (product or None) and str(product)[:200],
        "risk": risk,
        "latency_ms": int(latency_ms or 0),
        "cached": 1 if cached else 0,
        "model": model,
        "in_tokens": int(in_tokens or 0),
        "cached_tokens": int(cached_tokens or 0),
        "out_tokens": int(out_tokens or 0),
        "tool_calls": int(tool_calls or 0),
        "cost_usd": float(cost_usd or 0.0),
        "detail": (detail or None) and str(detail)[:500],
    }

    # Дублируем в обычный лог — так поток виден и в /logs, и в docker logs
    logger.info(
        "EVENT %s user=%s(@%s) status=%s src=%s product=%s risk=%s %sms cached=%s tok=%s/%s $%.4f",
        kind,
        row["user_id"],
        row["username"],
        status,
        source or "-",
        (row["product"] or "-")[:60],
        risk or "-",
        row["latency_ms"],
        row["cached"],
        row["in_tokens"],
        row["out_tokens"],
        row["cost_usd"],
    )

    await _run(_write_event, row)


# ─────────────────────────────────────────────────────────────
# Кэш результатов
# ─────────────────────────────────────────────────────────────

def _cache_get(key: str, ttl_sec: float) -> Optional[Dict[str, Any]]:
    row = _one("SELECT payload, created FROM cache WHERE key = ?", (key,))
    if not row:
        return None
    if ttl_sec > 0 and time.time() - float(row["created"]) > ttl_sec:
        _exec("DELETE FROM cache WHERE key = ?", (key,))
        return None
    try:
        data = json.loads(row["payload"])
    except Exception:  # noqa: BLE001
        _exec("DELETE FROM cache WHERE key = ?", (key,))
        return None
    _exec("UPDATE cache SET hits = hits + 1, last_hit = ? WHERE key = ?", (time.time(), key))
    return data


def _cache_put(key: str, kind: str, payload: Dict[str, Any]) -> None:
    _exec(
        """
        INSERT INTO cache (key, kind, payload, created, hits, last_hit)
        VALUES (?, ?, ?, ?, 0, NULL)
        ON CONFLICT(key) DO UPDATE SET
            payload = excluded.payload,
            kind    = excluded.kind,
            created = excluded.created
        """,
        (key, kind, json.dumps(payload, ensure_ascii=False), time.time()),
    )


async def cache_get(key: str) -> Optional[Dict[str, Any]]:
    if not config.CACHE_ENABLED:
        return None
    return await _run(_cache_get, key, config.CACHE_TTL_DAYS * 86400)


async def cache_put(key: str, kind: str, payload: Dict[str, Any]) -> None:
    if not config.CACHE_ENABLED:
        return
    await _run(_cache_put, key, kind, payload)


async def cache_clear() -> int:
    def _clear() -> int:
        row = _one("SELECT COUNT(*) AS c FROM cache")
        n = int(row["c"]) if row else 0
        _exec("DELETE FROM cache", ())
        return n

    return int(await _run(_clear) or 0)


async def cache_stats() -> Dict[str, Any]:
    def _stats() -> Dict[str, Any]:
        row = _one(
            "SELECT COUNT(*) AS entries, COALESCE(SUM(hits), 0) AS hits, "
            "MIN(created) AS oldest FROM cache"
        )
        by_kind = _query("SELECT kind, COUNT(*) AS c FROM cache GROUP BY kind")
        return {
            "entries": int(row["entries"]) if row else 0,
            "hits": int(row["hits"]) if row else 0,
            "oldest": float(row["oldest"]) if row and row["oldest"] else None,
            "by_kind": {r["kind"] or "?": int(r["c"]) for r in by_kind},
        }

    return await _run(_stats) or {"entries": 0, "hits": 0, "by_kind": {}}


# ─────────────────────────────────────────────────────────────
# Кэш проверки source_url
# ─────────────────────────────────────────────────────────────

def _url_get(url: str, ttl: float) -> Optional[bool]:
    row = _one("SELECT ok, checked FROM url_ok WHERE url = ?", (url,))
    if not row:
        return None
    if ttl > 0 and time.time() - float(row["checked"]) > ttl:
        return None
    return bool(row["ok"])


def _url_put(url: str, ok: bool) -> None:
    _exec(
        "INSERT INTO url_ok (url, ok, checked) VALUES (?, ?, ?) "
        "ON CONFLICT(url) DO UPDATE SET ok = excluded.ok, checked = excluded.checked",
        (url, 1 if ok else 0, time.time()),
    )


async def url_check_get(url: str) -> Optional[bool]:
    if not config.CACHE_ENABLED:
        return None
    return await _run(_url_get, url, config.URL_CACHE_TTL_HOURS * 3600)


async def url_check_put(url: str, ok: bool) -> None:
    if not config.CACHE_ENABLED:
        return
    await _run(_url_put, url, ok)


# ─────────────────────────────────────────────────────────────
# Отчёты
# ─────────────────────────────────────────────────────────────

_ANALYSIS_KINDS = ("photo", "album", "text")


def _period_stats(days: int) -> Dict[str, Any]:
    """Сводка за последние N дней (0 = за всё время)."""
    since = 0.0 if days <= 0 else time.time() - days * 86400

    totals = _one(
        """
        SELECT COUNT(*)                       AS events,
               COUNT(DISTINCT user_id)        AS users,
               COALESCE(SUM(cost_usd), 0)     AS cost,
               COALESCE(SUM(in_tokens), 0)    AS in_tokens,
               COALESCE(SUM(out_tokens), 0)   AS out_tokens,
               COALESCE(SUM(cached_tokens),0) AS cached_tokens
        FROM events WHERE ts >= ?
        """,
        (since,),
    )

    placeholders = ",".join("?" * len(_ANALYSIS_KINDS))
    analyses = _one(
        f"""
        SELECT COUNT(*)                                        AS total,
               COALESCE(SUM(cached), 0)                        AS from_cache,
               COALESCE(SUM(status = 'error'), 0)              AS errors,
               COALESCE(SUM(status = 'no_inci'), 0)            AS no_inci,
               COALESCE(AVG(NULLIF(latency_ms, 0)), 0)         AS avg_ms
        FROM events WHERE ts >= ? AND kind IN ({placeholders})
        """,
        (since, *_ANALYSIS_KINDS),
    )

    live_ms = _one(
        f"""
        SELECT COALESCE(AVG(NULLIF(latency_ms, 0)), 0) AS avg_ms
        FROM events WHERE ts >= ? AND kind IN ({placeholders}) AND cached = 0 AND status = 'ok'
        """,
        (since, *_ANALYSIS_KINDS),
    )

    by_kind = _query(
        "SELECT kind, COUNT(*) AS c FROM events WHERE ts >= ? GROUP BY kind ORDER BY c DESC",
        (since,),
    )
    by_risk = _query(
        f"SELECT risk, COUNT(*) AS c FROM events WHERE ts >= ? AND risk IS NOT NULL "
        f"AND kind IN ({placeholders}) GROUP BY risk ORDER BY c DESC",
        (since, *_ANALYSIS_KINDS),
    )
    top_products = _query(
        f"""
        SELECT product, COUNT(*) AS c FROM events
        WHERE ts >= ? AND product IS NOT NULL AND kind IN ({placeholders})
        GROUP BY LOWER(product) ORDER BY c DESC LIMIT 15
        """,
        (since, *_ANALYSIS_KINDS),
    )

    total = int(analyses["total"]) if analyses else 0
    cached_n = int(analyses["from_cache"]) if analyses else 0

    return {
        "days": days,
        "events": int(totals["events"]) if totals else 0,
        "users": int(totals["users"]) if totals else 0,
        "cost_usd": round(float(totals["cost"]), 4) if totals else 0.0,
        "in_tokens": int(totals["in_tokens"]) if totals else 0,
        "out_tokens": int(totals["out_tokens"]) if totals else 0,
        "cached_tokens": int(totals["cached_tokens"]) if totals else 0,
        "analyses": total,
        "from_cache": cached_n,
        "cache_hit_rate": round(cached_n / total * 100, 1) if total else 0.0,
        "errors": int(analyses["errors"]) if analyses else 0,
        "no_inci": int(analyses["no_inci"]) if analyses else 0,
        "avg_ms": int(analyses["avg_ms"]) if analyses else 0,
        "avg_ms_live": int(live_ms["avg_ms"]) if live_ms else 0,
        "by_kind": {r["kind"]: int(r["c"]) for r in by_kind},
        "by_risk": {r["risk"]: int(r["c"]) for r in by_risk},
        "top_products": [{"name": r["product"], "count": int(r["c"])} for r in top_products],
    }


def _audience() -> Dict[str, Any]:
    """Сколько реальных людей пользуется ботом."""
    now = time.time()

    def _uniq(seconds: float) -> int:
        row = _one(
            "SELECT COUNT(DISTINCT user_id) AS c FROM events WHERE ts >= ? AND user_id IS NOT NULL",
            (now - seconds,),
        )
        return int(row["c"]) if row else 0

    total = _one("SELECT COUNT(*) AS c FROM users")
    new_today = _one(
        "SELECT COUNT(*) AS c FROM users WHERE first_seen >= ?",
        (datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp(),),
    )
    # «Вернувшиеся» — те, кто заходил больше чем в один день
    returning = _one(
        """
        SELECT COUNT(*) AS c FROM (
            SELECT user_id FROM events WHERE user_id IS NOT NULL
            GROUP BY user_id HAVING COUNT(DISTINCT day) > 1
        )
        """
    )
    engaged = _one(
        """
        SELECT COUNT(*) AS c FROM (
            SELECT user_id FROM events
            WHERE user_id IS NOT NULL AND kind IN ('photo', 'album', 'text')
            GROUP BY user_id HAVING COUNT(*) >= 2
        )
        """
    )

    return {
        "total_users": int(total["c"]) if total else 0,
        "new_today": int(new_today["c"]) if new_today else 0,
        "dau": _uniq(86400),
        "wau": _uniq(7 * 86400),
        "mau": _uniq(30 * 86400),
        "returning": int(returning["c"]) if returning else 0,
        "engaged": int(engaged["c"]) if engaged else 0,
    }


def _daily(days: int = 30) -> List[Dict[str, Any]]:
    since = time.time() - days * 86400
    rows = _query(
        """
        SELECT day,
               COUNT(DISTINCT user_id) AS users,
               COALESCE(SUM(kind IN ('photo', 'album', 'text')), 0) AS analyses,
               COALESCE(SUM(cost_usd), 0) AS cost
        FROM events WHERE ts >= ? GROUP BY day ORDER BY day
        """,
        (since,),
    )
    known = {r["day"]: r for r in rows}

    out: List[Dict[str, Any]] = []
    start = datetime.now(timezone.utc).date() - timedelta(days=days - 1)
    for i in range(days):
        d = (start + timedelta(days=i)).strftime("%Y-%m-%d")
        r = known.get(d)
        out.append(
            {
                "day": d,
                "users": int(r["users"]) if r else 0,
                "analyses": int(r["analyses"]) if r else 0,
                "cost": round(float(r["cost"]), 4) if r else 0.0,
            }
        )
    return out


def _top_users(limit: int = 25) -> List[Dict[str, Any]]:
    rows = _query(
        """
        SELECT user_id, username, full_name, first_seen, last_seen,
               requests, photos, texts, compositions, details, errors, cost_usd
        FROM users ORDER BY requests DESC, last_seen DESC LIMIT ?
        """,
        (limit,),
    )
    return [dict(r) for r in rows]


def _recent_events(limit: int = 100, kind: Optional[str] = None) -> List[Dict[str, Any]]:
    if kind:
        rows = _query(
            "SELECT * FROM events WHERE kind = ? ORDER BY id DESC LIMIT ?", (kind, limit)
        )
    else:
        rows = _query("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
    return [dict(r) for r in rows]


def _recent_errors(limit: int = 30) -> List[Dict[str, Any]]:
    rows = _query(
        "SELECT * FROM events WHERE status = 'error' ORDER BY id DESC LIMIT ?", (limit,)
    )
    return [dict(r) for r in rows]


def _snapshot() -> Dict[str, Any]:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "audience": _audience(),
        "today": _period_stats(1),
        "week": _period_stats(7),
        "month": _period_stats(30),
        "all": _period_stats(0),
        "daily": _daily(30),
        "top_users": _top_users(25),
        "recent": _recent_events(60),
    }


async def snapshot() -> Dict[str, Any]:
    return await _run(_snapshot) or {}


async def period_stats(days: int) -> Dict[str, Any]:
    return await _run(_period_stats, days) or {}


async def audience() -> Dict[str, Any]:
    return await _run(_audience) or {}


async def top_users(limit: int = 25) -> List[Dict[str, Any]]:
    return await _run(_top_users, limit) or []


async def recent_events(limit: int = 100, kind: Optional[str] = None) -> List[Dict[str, Any]]:
    return await _run(_recent_events, limit, kind) or []


async def recent_errors(limit: int = 30) -> List[Dict[str, Any]]:
    return await _run(_recent_errors, limit) or []


_CSV_FIELDS = [
    "id", "ts", "day", "user_id", "username", "full_name", "chat_id", "kind", "status",
    "source", "product", "risk", "latency_ms", "cached", "model",
    "in_tokens", "cached_tokens", "out_tokens", "tool_calls", "cost_usd", "detail",
]


def _export_csv(limit: int = 20000) -> str:
    rows = _query("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_CSV_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        item = dict(r)
        item["ts"] = datetime.fromtimestamp(item["ts"], tz=timezone.utc).isoformat(timespec="seconds")
        writer.writerow(item)
    return buf.getvalue()


async def export_csv(limit: int = 20000) -> str:
    return await _run(_export_csv, limit) or ""


async def maintenance() -> None:
    """Периодическая уборка (вызывается фоновой задачей раз в сутки)."""
    await _run(_cleanup)


def close() -> None:
    global _conn
    with _lock:
        if _conn is not None:
            try:
                _conn.close()
            except Exception:  # noqa: BLE001
                pass
            _conn = None
