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

import hashlib

from aiogram import Dispatcher, F
from aiogram.filters import BaseFilter, Command
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from . import analytics, comedogen_store, config, dashboard

logger = logging.getLogger(__name__)

TG_LIMIT = 3900  # с запасом до телеграмных 4096


def is_admin(user: Any) -> bool:
    return bool(user and getattr(user, "id", None) in config.ADMIN_IDS)


class IsAdmin(BaseFilter):
    """Пропускает только админов; остальным команда «не существует»."""

    async def __call__(self, message: Message) -> bool:
        return is_admin(message.from_user)


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
        "/panel — панель управления с кнопками\n"
        "/stats — сводка: люди, разборы, кэш, ошибки, расход\n"
        "/users — кто чем пользуется\n"
        "/flow [N] — последние N событий (по умолчанию 40)\n"
        "/errors — последние ошибки\n"
        "/logs [N] — строки лога\n"
        "/export — все события в CSV\n"
        "/dash — ссылка на веб-дашборд\n"
        "/base — база комедогенов и её правка\n"
        "/cache — состояние кэша, /cache_clear — очистить\n\n"
        f"Админы: <code>{', '.join(str(i) for i in sorted(config.ADMIN_IDS))}</code>"
    )


# ─────────────────────────────────────────────────────────────
# Панель: /panel
# ─────────────────────────────────────────────────────────────

def _panel_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="🧪 База комедогенов", callback_data="a:base")],
        [
            InlineKeyboardButton(text="📊 Сводка", callback_data="a:stats"),
            InlineKeyboardButton(text="👥 Люди", callback_data="a:users"),
        ],
        [
            InlineKeyboardButton(text="🌊 Поток", callback_data="a:flow"),
            InlineKeyboardButton(text="⚠️ Ошибки", callback_data="a:errors"),
        ],
        [InlineKeyboardButton(text="🗃 Кэш", callback_data="a:cache")],
    ]
    if config.DASHBOARD_ENABLED:
        rows.insert(
            1, [InlineKeyboardButton(text="📈 Открыть дашборд", url=dashboard.dashboard_url())]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _panel_text() -> str:
    st = comedogen_store.stats()
    lines = [
        "🛠 <b>Панель управления</b>",
        "",
        f"База: 🔴 {st['hard']} жёстких, 🟠 {st['cond']} условных",
    ]
    if st["overrides"]:
        lines.append(f"Правок поверх эталона: {st['overrides']}")
    lines += ["", "Всё то же есть командами — /adminhelp."]
    return "\n".join(lines)


async def cmd_panel(msg: Message) -> None:
    await msg.answer(_panel_text(), reply_markup=_panel_keyboard(),
                     disable_web_page_preview=True)
    await analytics.track(kind="admin", user=msg.from_user, chat_id=msg.chat.id, detail="/panel")


# ─────────────────────────────────────────────────────────────
# База комедогенов: просмотр и правка
# ─────────────────────────────────────────────────────────────

# Кого сейчас ждём с текстом названия: {user_id: kind}
_awaiting_add: Dict[int, str] = {}

PAGE = 10

_KIND_LABEL = {
    comedogen_store.KIND_HARD: "🔴 Жёсткие",
    comedogen_store.KIND_COND: "🟠 Условные",
}


def _name_hash(name: str) -> str:
    """Короткая подпись названия: ловит рассинхрон списка между показом и нажатием."""
    return hashlib.sha256(comedogen_store.normalize(name).encode()).hexdigest()[:6]


def _base_text() -> str:
    st = comedogen_store.stats()
    lines = [
        "🧪 <b>База комедогенов</b>",
        "",
        f"Эталон: {st['baseline_hard']} жёстких, {st['baseline_cond']} условных",
    ]
    if st["overrides"]:
        parts = []
        if st["added"]:
            parts.append(f"добавлено {st['added']}")
        if st["dropped"]:
            parts.append(f"убрано {st['dropped']}")
        lines.append("Ваши правки: " + ", ".join(parts))
    else:
        lines.append("Правок нет — база в эталонном состоянии.")
    lines += [
        f"<b>В работе: 🔴 {st['hard']} · 🟠 {st['cond']}</b>",
        "",
        "По этой базе код считает риск. Правка меняет вердикты "
        "для всех пользователей, а кэш готовых разборов сбрасывается.",
    ]
    if not comedogen_store.enabled():
        lines += ["", "⚠️ <i>ANALYTICS_ENABLED=false — правки сохранять некуда, "
                      "база работает только по эталону.</i>"]
    return "\n".join(lines)


def _base_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="🔴 Жёсткие", callback_data="a:list:hard:0"),
            InlineKeyboardButton(text="🟠 Условные", callback_data="a:list:conditional:0"),
        ]
    ]
    if comedogen_store.enabled():
        rows.append([InlineKeyboardButton(text="✏️ История правок", callback_data="a:hist")])
    rows.append([InlineKeyboardButton(text="← В панель", callback_data="a:panel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _list_text(kind: str, page: int) -> str:
    items = comedogen_store.listing(kind)
    total_pages = max(1, (len(items) + PAGE - 1) // PAGE)
    page = max(0, min(page, total_pages - 1))
    chunk = items[page * PAGE : (page + 1) * PAGE]

    lines = [
        f"<b>{_KIND_LABEL[kind]} · {len(items)}</b>",
        f"страница {page + 1} из {total_pages}",
        "",
    ]
    for it in chunk:
        mark = "" if it["baseline"] else "  ✚ добавлено"
        lines.append(f"• <code>{_e(it['name'])}</code>{mark}")
    lines += ["", "Нажми на компонент, чтобы убрать его из базы."]
    return "\n".join(lines)


def _list_keyboard(kind: str, page: int) -> InlineKeyboardMarkup:
    items = comedogen_store.listing(kind)
    total_pages = max(1, (len(items) + PAGE - 1) // PAGE)
    page = max(0, min(page, total_pages - 1))
    start = page * PAGE
    chunk = items[start : start + PAGE]

    rows = []
    for offset, it in enumerate(chunk):
        idx = start + offset
        rows.append([
            InlineKeyboardButton(
                text=f"✕  {it['name']}",
                callback_data=f"a:rm:{kind}:{idx}:{_name_hash(it['name'])}",
            )
        ])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="←", callback_data=f"a:list:{kind}:{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="→", callback_data=f"a:list:{kind}:{page + 1}"))
    if nav:
        rows.append(nav)

    if comedogen_store.enabled():
        rows.append([InlineKeyboardButton(text="➕ Добавить компонент",
                                          callback_data=f"a:add:{kind}")])
    rows.append([InlineKeyboardButton(text="← К базе", callback_data="a:base")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _history_text() -> str:
    rows = comedogen_store.history(20)
    if not rows:
        return "✏️ <b>История правок</b>\n\nПравок ещё не было — база в эталонном состоянии."

    lines = ["✏️ <b>История правок</b>", ""]
    for r in rows:
        sign = "＋" if r["action"] == comedogen_store.ACTION_ADD else "−"
        kind = "жёсткий" if r["kind"] == comedogen_store.KIND_HARD else "условный"
        state = "" if r["active"] else "  <i>(отменена)</i>"
        lines.append(
            f"{sign} <code>{_e(r['name'])}</code> · {kind}{state}\n"
            f"   {_e(r['author'])}, {_when(r['ts'] or 0)}"
        )
    return "\n".join(lines)


def _history_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for r in comedogen_store.history(20):
        if not r["active"]:
            continue
        sign = "＋" if r["action"] == comedogen_store.ACTION_ADD else "−"
        rows.append([
            InlineKeyboardButton(
                text=f"↩️ отменить: {sign} {r['name']}"[:60],
                callback_data=f"a:undo:{r['id']}",
            )
        ])
    if rows:
        rows.append([InlineKeyboardButton(text="↩️ Сбросить всё к эталону",
                                          callback_data="a:reset")])
    rows.append([InlineKeyboardButton(text="← К базе", callback_data="a:base")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def cmd_base(msg: Message) -> None:
    await msg.answer(_base_text(), reply_markup=_base_keyboard())
    await analytics.track(kind="admin", user=msg.from_user, chat_id=msg.chat.id, detail="/base")


ADD_RESULT = {
    "added": "✅ <code>{name}</code> добавлен в базу ({kind}).",
    "restored": "✅ <code>{name}</code> возвращён в базу ({kind}) — прежнее удаление отменено.",
    "exists": "Компонент <code>{name}</code> уже есть в базе ({kind}).",
    "empty": "Пустое название.",
    "bad_kind": "Неизвестный список.",
}

DROP_RESULT = {
    "dropped": "✅ <code>{name}</code> убран из базы ({kind}).",
    "reverted": "✅ <code>{name}</code> убран — это была ваша же правка, теперь отменена.",
    "missing": "Компонента <code>{name}</code> в базе ({kind}) нет.",
    "bad_kind": "Неизвестный список.",
}


async def _refresh(cb: CallbackQuery, text: str, markup: InlineKeyboardMarkup) -> None:
    """Перерисовывает экран на месте; если текст тот же — Telegram ругается, это не ошибка."""
    try:
        await cb.message.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
    except Exception:  # noqa: BLE001
        await cb.message.answer(text, reply_markup=markup, disable_web_page_preview=True)


async def on_callback(cb: CallbackQuery) -> None:
    """Все кнопки админки. Права проверяются здесь же, а не только на командах."""
    if not is_admin(cb.from_user):
        await cb.answer()
        return

    data = cb.data or ""
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""

    if action == "panel":
        await cb.answer()
        await _refresh(cb, _panel_text(), _panel_keyboard())
        return

    if action == "base":
        await cb.answer()
        _awaiting_add.pop(cb.from_user.id, None)
        await _refresh(cb, _base_text(), _base_keyboard())
        return

    if action == "list" and len(parts) >= 4:
        kind, page = parts[2], int(parts[3] or 0)
        if kind not in comedogen_store.KINDS:
            await cb.answer("Неизвестный список.", show_alert=True)
            return
        await cb.answer()
        await _refresh(cb, _list_text(kind, page), _list_keyboard(kind, page))
        return

    if action == "hist":
        await cb.answer()
        await _refresh(cb, _history_text(), _history_keyboard())
        return

    if action == "add" and len(parts) >= 3:
        kind = parts[2]
        if kind not in comedogen_store.KINDS:
            await cb.answer("Неизвестный список.", show_alert=True)
            return
        if not comedogen_store.enabled():
            await cb.answer("Правки недоступны: ANALYTICS_ENABLED=false.", show_alert=True)
            return
        _awaiting_add[cb.from_user.id] = kind
        await cb.answer()
        await cb.message.answer(
            f"Пришли название компонента одним сообщением — добавлю в список "
            f"«{_KIND_LABEL[kind]}».\n\n"
            "Пиши как в INCI, латиницей: например <code>isopropyl myristate</code>.\n"
            "Отмена — /panel"
        )
        return

    if action == "rm" and len(parts) >= 5:
        kind, idx, sig = parts[2], int(parts[3] or 0), parts[4]
        items = comedogen_store.listing(kind)
        if idx >= len(items) or _name_hash(items[idx]["name"]) != sig:
            await cb.answer("Список изменился — открой заново.", show_alert=True)
            return
        name = items[idx]["name"]
        await cb.answer()
        await cb.message.answer(
            f"Убрать <code>{_e(name)}</code> из списка «{_KIND_LABEL[kind]}»?\n\n"
            "Вердикты пересчитаются для всех, кэш разборов сбросится.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✕ Да, убрать",
                                      callback_data=f"a:rmy:{kind}:{idx}:{sig}")],
                [InlineKeyboardButton(text="← Отмена", callback_data=f"a:list:{kind}:0")],
            ]),
        )
        return

    if action == "rmy" and len(parts) >= 5:
        kind, idx, sig = parts[2], int(parts[3] or 0), parts[4]
        items = comedogen_store.listing(kind)
        if idx >= len(items) or _name_hash(items[idx]["name"]) != sig:
            await cb.answer("Список изменился — открой заново.", show_alert=True)
            return
        name = items[idx]["name"]
        code = await comedogen_store.drop(name, kind, cb.from_user)
        await cb.answer()
        await cb.message.answer(
            DROP_RESULT.get(code, "Не получилось.").format(name=_e(name), kind=_KIND_LABEL[kind])
        )
        await _log_base_change(cb.from_user, f"drop {kind} {name} → {code}")
        await cb.message.answer(_base_text(), reply_markup=_base_keyboard())
        return

    if action == "undo" and len(parts) >= 3:
        ok = await comedogen_store.undo(int(parts[2] or 0))
        if ok:
            await comedogen_store._applied()
        await cb.answer("Правка отменена." if ok else "Не нашла эту правку.", show_alert=not ok)
        await _log_base_change(cb.from_user, f"undo {parts[2]} → {ok}")
        await _refresh(cb, _history_text(), _history_keyboard())
        return

    if action == "reset":
        await cb.answer()
        await cb.message.answer(
            "Сбросить <b>все</b> правки и вернуть базу к эталону?\n\n"
            "Сами правки останутся в истории, но перестанут действовать.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="↩️ Да, к эталону", callback_data="a:resety")],
                [InlineKeyboardButton(text="← Отмена", callback_data="a:base")],
            ]),
        )
        return

    if action == "resety":
        n = await comedogen_store.reset()
        await cb.answer()
        await cb.message.answer(f"↩️ База возвращена к эталону, отменено правок: {n}.")
        await _log_base_change(cb.from_user, f"reset → {n}")
        await cb.message.answer(_base_text(), reply_markup=_base_keyboard())
        return

    # Кнопки-ярлыки к существующим отчётам
    shortcut = {
        "stats": cmd_stats, "users": cmd_users, "flow": cmd_flow,
        "errors": cmd_errors, "cache": cmd_cache,
    }.get(action)
    if shortcut:
        await cb.answer()
        await shortcut(cb.message)
        return

    await cb.answer()


async def _log_base_change(user: Any, detail: str) -> None:
    logger.info("БАЗА: %s (%s)", detail, getattr(user, "id", None))
    await analytics.track(kind="base_edit", user=user, detail=detail)


async def on_add_text(msg: Message) -> None:
    """Ловит название компонента после нажатия «Добавить»."""
    kind = _awaiting_add.pop(msg.from_user.id, None)
    if not kind:
        return
    name = (msg.text or "").strip()
    if len(name) > 40:
        await msg.answer("Слишком длинное название — до 40 символов.")
        return

    code = await comedogen_store.add(name, kind, msg.from_user)
    await msg.answer(
        ADD_RESULT.get(code, "Не получилось.").format(
            name=_e(name), kind=_KIND_LABEL.get(kind, kind)
        )
    )
    await _log_base_change(msg.from_user, f"add {kind} {name} → {code}")
    await msg.answer(_base_text(), reply_markup=_base_keyboard())


def waiting_for_name(user: Any) -> bool:
    """Ждём ли от этого админа название компонента (чтобы не искать его в косметике)."""
    return getattr(user, "id", None) in _awaiting_add


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
        (cmd_panel, "panel"),
        (cmd_base, "base"),
        (cmd_cache_clear, "cache_clear"),
        (cmd_cache, "cache"),
        (cmd_admin_help, "adminhelp"),
    ]
    for handler, name in handlers:
        dp.message.register(handler, Command(name), admin)

    # Ввод названия компонента перехватываем раньше обычного текста,
    # иначе бот примет «isopropyl myristate» за название косметики.
    dp.message.register(on_add_text, F.text, admin, lambda m: waiting_for_name(m.from_user))
    dp.callback_query.register(on_callback, F.data.startswith("a:"))

    logger.info("Админ-команды включены для id: %s", sorted(config.ADMIN_IDS))
