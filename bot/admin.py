"""Скрытые админ-команды CreamcheckBot.

Видны только тем, чьи id перечислены в ADMIN_IDS (по умолчанию — Лена и Лиза).
Для всех остальных команды не существуют: бот молча ничего не отвечает,
и в списке команд Telegram они не публикуются.

    /stats     — сводка: люди, разборы, кэш, ошибки, расход
    /users     — кто чем пользуется
    /flow      — живой поток последних событий
    /errors    — последние ошибки
    /logs      — последние строки лога (файлом)
    /export    — выгрузка событий в CSV
    /dash      — ссылка на веб-дашборд
    /cache     — состояние кэша; /cache_clear — очистить
    /adminhelp — эта справка
"""

from __future__ import annotations

import html
import io
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from aiogram import Dispatcher
from aiogram.filters import BaseFilter, Command
from aiogram.types import BufferedInputFile, Message

from . import analytics, config, dashboard

logger = logging.getLogger(__name__)

TG_LIMIT = 3900  # с запасом до телеграмных 4096


class IsAdmin(BaseFilter):
    """Пропускает только админов; остальным команда «не существует»."""

    async def __call__(self, message: Message) -> bool:
        user = message.from_user
        return bool(user and user.id in config.ADMIN_IDS)


def _e(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=False)


def _dur(ms: Optional[int]) -> str:
    ms = int(ms or 0)
    if not ms:
        return "—"
    return f"{ms / 1000:.1f} с" if ms >= 1000 else f"{ms} мс"


def _when(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%d.%m %H:%M")


def _who(row: Dict[str, Any]) -> str:
    if row.get("username"):
        return "@" + _e(row["username"])
    if row.get("full_name"):
        return _e(row["full_name"])
    return f"#{row.get('user_id') or '—'}"


async def _send_long(msg: Message, text: str, filename: str = "report.txt") -> None:
    """Длинные отчёты уходят файлом, короткие — сообщением."""
    if len(text) <= TG_LIMIT:
        await msg.answer(text)
        return
    plain = io.StringIO()
    plain.write(text.replace("<b>", "").replace("</b>", "").replace("<code>", "").replace("</code>", ""))
    await msg.answer_document(
        BufferedInputFile(plain.getvalue().encode("utf-8"), filename=filename),
        caption="Отчёт получился длинным — отправляю файлом.",
    )


# ─────────────────────────────────────────────────────────────
# /stats
# ─────────────────────────────────────────────────────────────

def _num(value: Any) -> str:
    """1234567 → «1 234 567» (узкий неразрывный пробел телеграм не ломает)."""
    return f"{int(value or 0):,}".replace(",", " ")


def _period_block(title: str, p: Dict[str, Any]) -> List[str]:
    tokens = f"{_num(p.get('in_tokens'))} in / {_num(p.get('out_tokens'))} out"
    cost = f"${float(p.get('cost_usd') or 0):.2f}"
    return [
        f"<b>{title}</b>",
        f"• разборов: <b>{p.get('analyses', 0)}</b> (людей: {p.get('users', 0)})",
        f"• из кэша: {p.get('from_cache', 0)} ({p.get('cache_hit_rate', 0)}%)",
        f"• среднее время: {_dur(p.get('avg_ms_live'))}",
        f"• состав не найден: {p.get('no_inci', 0)} · ошибок: {p.get('errors', 0)}",
        f"• токены: {tokens} · расход ≈ {cost}",
        "",
    ]


async def cmd_stats(msg: Message) -> None:
    a = await analytics.audience()
    today = await analytics.period_stats(1)
    week = await analytics.period_stats(7)
    month = await analytics.period_stats(30)
    total = await analytics.period_stats(0)

    lines: List[str] = [
        "📊 <b>CreamcheckBot — статистика</b>",
        "",
        "<b>Аудитория</b>",
        f"• всего людей: <b>{a.get('total_users', 0)}</b>",
        f"• активны сегодня: {a.get('dau', 0)} · за неделю: {a.get('wau', 0)} · за месяц: {a.get('mau', 0)}",
        f"• новых сегодня: {a.get('new_today', 0)}",
        f"• возвращались в разные дни: {a.get('returning', 0)}",
        f"• сделали 2+ разбора: {a.get('engaged', 0)}",
        "",
    ]
    lines += _period_block("Сегодня", today)
    lines += _period_block("7 дней", week)
    lines += _period_block("30 дней", month)
    lines += _period_block("Всё время", total)

    by_kind = total.get("by_kind") or {}
    if by_kind:
        lines.append("<b>Что нажимают (всё время)</b>")
        names = {
            "photo": "📷 фото", "album": "🖼 альбом", "text": "⌨️ текст",
            "composition": "🧾 состав", "step2": "📘 подробнее",
            "start": "▶️ /start", "help": "❔ /help", "about": "ℹ️ /about",
            "contacts": "📇 /contacts", "base": "📚 /base", "admin": "🛠 админ",
            "blocked": "🚫 ограничение",
        }
        for k, v in sorted(by_kind.items(), key=lambda kv: -kv[1]):
            lines.append(f"• {names.get(k, _e(k))}: {v}")
        lines.append("")

    by_risk = total.get("by_risk") or {}
    if by_risk:
        risk_names = {"high": "🔴 высокий", "medium": "🟠 средний",
                      "low": "🟡 низкий", "none": "⚪️ не выявлен"}
        lines.append("<b>Результаты</b>")
        for k, v in sorted(by_risk.items(), key=lambda kv: -kv[1]):
            lines.append(f"• {risk_names.get(k, _e(k))}: {v}")
        lines.append("")

    tops = total.get("top_products") or []
    if tops:
        lines.append("<b>Частые продукты</b>")
        for item in tops[:10]:
            lines.append(f"• {_e(item['name'])} — {item['count']}")
        lines.append("")

    lines.append("Подробнее и в реальном времени: /dash")
    await _send_long(msg, "\n".join(lines), "creamcheck-stats.txt")
    await analytics.track(kind="admin", user=msg.from_user, chat_id=msg.chat.id, detail="/stats")


# ─────────────────────────────────────────────────────────────
# /users
# ─────────────────────────────────────────────────────────────

async def cmd_users(msg: Message) -> None:
    rows = await analytics.top_users(30)
    if not rows:
        await msg.answer("Пока никто не пользовался ботом.")
        return

    lines = ["👥 <b>Пользователи</b> (по числу разборов)", ""]
    for i, u in enumerate(rows, start=1):
        lines.append(
            f"{i}. {_who(u)} <code>#{u.get('user_id')}</code>\n"
            f"    разборов {u.get('requests', 0)} "
            f"(📷 {u.get('photos', 0)} / ⌨️ {u.get('texts', 0)}) · "
            f"🧾 {u.get('compositions', 0)} · 📘 {u.get('details', 0)}"
            + (f" · ⚠️ {u['errors']}" if u.get("errors") else "")
            + f"\n    последний раз: {_when(u.get('last_seen') or 0)} · ≈${u.get('cost_usd', 0):.2f}"
        )
    await _send_long(msg, "\n".join(lines), "creamcheck-users.txt")
    await analytics.track(kind="admin", user=msg.from_user, chat_id=msg.chat.id, detail="/users")


# ─────────────────────────────────────────────────────────────
# /flow — живой поток
# ─────────────────────────────────────────────────────────────

_KIND_ICON = {
    "photo": "📷", "album": "🖼", "text": "⌨️", "composition": "🧾", "step2": "📘",
    "start": "▶️", "help": "❔", "about": "ℹ️", "contacts": "📇", "base": "📚",
    "admin": "🛠", "blocked": "🚫",
}


async def cmd_flow(msg: Message) -> None:
    parts = (msg.text or "").split()
    try:
        limit = min(200, max(5, int(parts[1]))) if len(parts) > 1 else 40
    except ValueError:
        limit = 40

    rows = await analytics.recent_events(limit)
    if not rows:
        await msg.answer("Событий пока нет.")
        return

    lines = [f"🌊 <b>Последние {len(rows)} событий</b>", ""]
    for r in rows:
        icon = _KIND_ICON.get(r.get("kind") or "", "•")
        bits = [f"{_when(r['ts'])} {icon} {_who(r)}"]
        if r.get("product"):
            bits.append(f"«{_e(r['product'])}»")
        if r.get("risk"):
            bits.append(str(r["risk"]))
        if r.get("status") and r["status"] != "ok":
            bits.append(f"⚠️{_e(r['status'])}")
        if r.get("cached"):
            bits.append("кэш")
        if r.get("latency_ms"):
            bits.append(_dur(r["latency_ms"]))
        lines.append(" · ".join(bits))

    await _send_long(msg, "\n".join(lines), "creamcheck-flow.txt")
    await analytics.track(kind="admin", user=msg.from_user, chat_id=msg.chat.id, detail="/flow")


# ─────────────────────────────────────────────────────────────
# /errors
# ─────────────────────────────────────────────────────────────

async def cmd_errors(msg: Message) -> None:
    rows = await analytics.recent_errors(30)
    if not rows:
        await msg.answer("✅ Ошибок не зафиксировано.")
        return
    lines = ["⚠️ <b>Последние ошибки</b>", ""]
    for r in rows:
        lines.append(
            f"{_when(r['ts'])} · {_who(r)} · {_e(r.get('kind'))}\n"
            f"    {_e(r.get('detail') or 'без описания')}"
        )
    await _send_long(msg, "\n".join(lines), "creamcheck-errors.txt")
    await analytics.track(kind="admin", user=msg.from_user, chat_id=msg.chat.id, detail="/errors")


# ─────────────────────────────────────────────────────────────
# /logs
# ─────────────────────────────────────────────────────────────

async def cmd_logs(msg: Message) -> None:
    parts = (msg.text or "").split()
    try:
        limit = min(2000, max(20, int(parts[1]))) if len(parts) > 1 else 300
    except ValueError:
        limit = 300

    lines = analytics.recent_logs(limit)
    if not lines:
        await msg.answer("Лог пока пуст.")
        return

    body = "\n".join(lines)
    if len(body) <= TG_LIMIT:
        await msg.answer(f"<b>Лог ({len(lines)} строк)</b>\n<code>{html.escape(body)}</code>")
    else:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
        await msg.answer_document(
            BufferedInputFile(body.encode("utf-8"), filename=f"creamcheck-log-{stamp}.txt"),
            caption=f"Последние {len(lines)} строк лога.",
        )
    await analytics.track(kind="admin", user=msg.from_user, chat_id=msg.chat.id, detail="/logs")


# ─────────────────────────────────────────────────────────────
# /export
# ─────────────────────────────────────────────────────────────

async def cmd_export(msg: Message) -> None:
    csv_text = await analytics.export_csv()
    if not csv_text:
        await msg.answer("Выгружать пока нечего.")
        return
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    await msg.answer_document(
        BufferedInputFile(csv_text.encode("utf-8-sig"), filename=f"creamcheck-events-{stamp}.csv"),
        caption="Все события: кто, когда, что запросил, сколько заняло и стоило.",
    )
    await analytics.track(kind="admin", user=msg.from_user, chat_id=msg.chat.id, detail="/export")


# ─────────────────────────────────────────────────────────────
# /dash
# ─────────────────────────────────────────────────────────────

async def cmd_dash(msg: Message) -> None:
    if not config.DASHBOARD_ENABLED:
        await msg.answer("Дашборд выключен (DASHBOARD_ENABLED=false).")
        return
    url = dashboard.dashboard_url()
    note = ""
    if not config.PUBLIC_BASE_URL:
        note = (
            "\n\n<i>PUBLIC_BASE_URL не задан — подставь адрес сервера вручную, "
            "например http://193.168.199.122:%s</i>" % config.RENDER_PORT
        )
    await msg.answer(
        "📈 <b>Веб-дашборд</b>\n\n"
        f'<a href="{url}">Открыть статистику</a>\n\n'
        f"<code>{html.escape(url)}</code>\n\n"
        "Ссылка содержит ключ доступа — не публикуй её." + note,
        disable_web_page_preview=True,
    )
    await analytics.track(kind="admin", user=msg.from_user, chat_id=msg.chat.id, detail="/dash")


# ─────────────────────────────────────────────────────────────
# /cache
# ─────────────────────────────────────────────────────────────

async def cmd_cache(msg: Message) -> None:
    st = await analytics.cache_stats()
    by_kind = st.get("by_kind") or {}
    lines = [
        "🗃 <b>Кэш результатов</b>",
        "",
        f"• записей: <b>{st.get('entries', 0)}</b>",
        f"• попаданий: {st.get('hits', 0)}",
        f"• срок жизни: {config.CACHE_TTL_DAYS} дн.",
    ]
    if by_kind:
        lines.append("• по типам: " + ", ".join(f"{_e(k)} — {v}" for k, v in by_kind.items()))
    lines += ["", "Очистить: /cache_clear"]
    await msg.answer("\n".join(lines))


async def cmd_cache_clear(msg: Message) -> None:
    n = await analytics.cache_clear()
    await msg.answer(f"🧹 Кэш очищен: удалено {n} записей.")
    await analytics.track(kind="admin", user=msg.from_user, chat_id=msg.chat.id, detail="/cache_clear")


# ─────────────────────────────────────────────────────────────
# /adminhelp
# ─────────────────────────────────────────────────────────────

async def cmd_admin_help(msg: Message) -> None:
    await msg.answer(
        "🛠 <b>Скрытые команды</b> (видят только админы)\n\n"
        "/stats — сводка: люди, разборы, кэш, ошибки, расход\n"
        "/users — кто чем пользуется\n"
        "/flow [N] — последние N событий (по умолчанию 40)\n"
        "/errors — последние ошибки\n"
        "/logs [N] — строки лога\n"
        "/export — все события в CSV\n"
        "/dash — ссылка на веб-дашборд\n"
        "/cache — состояние кэша, /cache_clear — очистить\n\n"
        f"Админы: <code>{', '.join(str(i) for i in sorted(config.ADMIN_IDS))}</code>"
    )


def register(dp: Dispatcher) -> None:
    """Регистрирует админ-команды. Вызывать ДО обычных текстовых хендлеров."""
    admin = IsAdmin()
    handlers = [
        (cmd_stats, "stats"),
        (cmd_users, "users"),
        (cmd_flow, "flow"),
        (cmd_errors, "errors"),
        (cmd_logs, "logs"),
        (cmd_export, "export"),
        (cmd_dash, "dash"),
        (cmd_cache_clear, "cache_clear"),
        (cmd_cache, "cache"),
        (cmd_admin_help, "adminhelp"),
    ]
    for handler, name in handlers:
        dp.message.register(handler, Command(name), admin)

    logger.info("Админ-команды включены для id: %s", sorted(config.ADMIN_IDS))
