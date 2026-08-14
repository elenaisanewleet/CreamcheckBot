"""Веб-дашборд CreamcheckBot: живой поток, аудитория, расходы.

Поднимается на том же порту, что и health-сервер (существующие проверки `/` и
`/health` работают как раньше). Всё новое закрыто токеном:

    http://<host>:<PORT>/dash?key=<DASHBOARD_TOKEN>

Страница самодостаточная: никаких внешних скриптов и шрифтов, графики — inline SVG.
"""

from __future__ import annotations

import hmac
import json
import logging
import secrets
from datetime import datetime, timezone
from typing import Optional

from aiohttp import web

from . import analytics, config

logger = logging.getLogger(__name__)

# Токен доступа: из .env или случайный на время жизни процесса.
DASHBOARD_TOKEN: str = config.DASHBOARD_TOKEN or secrets.token_urlsafe(18)
_TOKEN_IS_GENERATED = not bool(config.DASHBOARD_TOKEN)


def dashboard_url(host_hint: Optional[str] = None) -> str:
    base = config.PUBLIC_BASE_URL or host_hint or f"http://localhost:{config.RENDER_PORT}"
    return f"{base.rstrip('/')}/dash?key={DASHBOARD_TOKEN}"


# ─────────────────────────────────────────────────────────────
# Доступ
# ─────────────────────────────────────────────────────────────

def _authorized(request: web.Request) -> bool:
    supplied = request.query.get("key") or ""
    if not supplied:
        auth = request.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            supplied = auth[7:].strip()
    if not supplied:
        supplied = request.cookies.get("cc_key", "")
    return hmac.compare_digest(supplied, DASHBOARD_TOKEN)


def _guard(handler):
    async def wrapper(request: web.Request) -> web.Response:
        if not _authorized(request):
            return web.json_response({"error": "forbidden"}, status=403)
        resp = await handler(request)
        resp.headers["X-Robots-Tag"] = "noindex, nofollow"
        resp.headers["Cache-Control"] = "no-store"
        return resp

    return wrapper


# ─────────────────────────────────────────────────────────────
# JSON API
# ─────────────────────────────────────────────────────────────

@_guard
async def api_stats(request: web.Request) -> web.Response:
    return web.json_response(await analytics.snapshot(), dumps=lambda o: json.dumps(o, ensure_ascii=False))


@_guard
async def api_events(request: web.Request) -> web.Response:
    try:
        limit = min(1000, max(1, int(request.query.get("limit", "150"))))
    except ValueError:
        limit = 150
    kind = request.query.get("kind") or None
    return web.json_response(
        await analytics.recent_events(limit, kind),
        dumps=lambda o: json.dumps(o, ensure_ascii=False),
    )


@_guard
async def api_logs(request: web.Request) -> web.Response:
    try:
        limit = min(2000, max(1, int(request.query.get("limit", "300"))))
    except ValueError:
        limit = 300
    return web.json_response({"lines": analytics.recent_logs(limit)},
                             dumps=lambda o: json.dumps(o, ensure_ascii=False))


@_guard
async def export_csv(request: web.Request) -> web.Response:
    body = await analytics.export_csv()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    return web.Response(
        body=body.encode("utf-8-sig"),
        content_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="creamcheck-events-{stamp}.csv"'},
    )


# ─────────────────────────────────────────────────────────────
# HTML
# ─────────────────────────────────────────────────────────────

_KIND_LABELS = {
    "photo": "📷 Фото",
    "album": "🖼 Альбом",
    "text": "⌨️ Текст",
    "composition": "🧾 Состав",
    "step2": "📘 Подробнее",
    "start": "▶️ /start",
    "help": "❔ /help",
    "about": "ℹ️ /about",
    "contacts": "📇 /contacts",
    "base": "📚 /base",
    "admin": "🛠 Админ",
    "blocked": "🚫 Блок",
}

_RISK_LABELS = {
    "high": "🔴 высокий",
    "medium": "🟠 средний",
    "low": "🟡 низкий",
    "none": "⚪️ не выявлен",
}

_PAGE = """<!doctype html>
<html lang="ru"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>CreamcheckBot — статистика</title>
<style>
:root{
  --bg:#f7f8fb; --card:#fff; --ink:#151823; --muted:#6a7183; --line:#e6e9f0;
  --accent:#7c6cf5; --ok:#12a150; --warn:#e8a13a; --bad:#e2544c;
  --shadow:0 1px 2px rgba(16,20,40,.05),0 8px 24px rgba(16,20,40,.05);
}
@media (prefers-color-scheme: dark){
  :root{ --bg:#0f1117; --card:#171a22; --ink:#eceef4; --muted:#98a0b3; --line:#262a36;
         --shadow:0 1px 2px rgba(0,0,0,.3),0 8px 24px rgba(0,0,0,.35); }
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Inter,Arial,sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:24px 18px 64px}
header{display:flex;flex-wrap:wrap;gap:12px;align-items:baseline;justify-content:space-between;margin-bottom:20px}
h1{font-size:21px;margin:0;letter-spacing:-.01em}
h2{font-size:14px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin:28px 0 12px}
.sub{color:var(--muted);font-size:13px}
.grid{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(158px,1fr))}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px 16px;box-shadow:var(--shadow)}
.k{color:var(--muted);font-size:12px;letter-spacing:.02em}
.v{font-size:26px;font-weight:650;letter-spacing:-.02em;margin-top:4px}
.v small{font-size:13px;font-weight:500;color:var(--muted)}
.row{display:grid;gap:12px;grid-template-columns:1fr}
@media(min-width:900px){.row.two{grid-template-columns:1.35fr 1fr}}
table{width:100%;border-collapse:collapse;font-size:13.5px}
th{ text-align:left;color:var(--muted);font-weight:600;font-size:11.5px;text-transform:uppercase;
    letter-spacing:.05em;padding:8px 10px;border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}
tr:last-child td{border-bottom:0}
.scroll{overflow-x:auto;-webkit-overflow-scrolling:touch}
.tag{display:inline-block;padding:1px 8px;border-radius:999px;font-size:12px;background:var(--bg);border:1px solid var(--line)}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12px}
.ok{color:var(--ok)} .warn{color:var(--warn)} .bad{color:var(--bad)}
.bar{display:flex;align-items:flex-end;gap:3px;height:120px;margin-top:6px}
.bar i{flex:1;background:var(--accent);border-radius:3px 3px 0 0;min-height:2px;opacity:.85;display:block}
.bar i:hover{opacity:1}
.axis{display:flex;justify-content:space-between;color:var(--muted);font-size:11px;margin-top:6px}
pre{margin:0;white-space:pre-wrap;word-break:break-word;font-family:ui-monospace,Menlo,Consolas,monospace;
    font-size:11.5px;color:var(--muted);max-height:420px;overflow:auto}
.tabs{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px}
.tabs button{font:inherit;font-size:13px;padding:5px 12px;border-radius:999px;cursor:pointer;
  border:1px solid var(--line);background:var(--card);color:var(--muted)}
.tabs button[aria-selected="true"]{background:var(--accent);border-color:var(--accent);color:#fff}
a{color:var(--accent)}
.foot{margin-top:32px;color:var(--muted);font-size:12px}
</style></head>
<body><div class="wrap">
<header>
  <div>
    <h1>🫧 CreamcheckBot — статистика</h1>
    <div class="sub" id="gen"></div>
  </div>
  <div class="sub">
    <a id="csv" href="#">⬇️ Выгрузить CSV</a> ·
    <a href="#" onclick="load();return false">🔄 Обновить</a>
  </div>
</header>
<div id="app">Загружаю…</div>
<div class="foot">Обновляется автоматически раз в 30 секунд. Данные хранятся локально на сервере бота.</div>
</div>
<script>
const KEY = new URLSearchParams(location.search).get('key') || '';
document.getElementById('csv').href = '/export.csv?key=' + encodeURIComponent(KEY);

const KIND = __KIND__;
const RISK = __RISK__;
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const money = v => '$' + (Number(v||0)).toFixed(2);
const dur = ms => !ms ? '—' : (ms >= 1000 ? (ms/1000).toFixed(1) + ' с' : ms + ' мс');
const when = ts => new Date(ts*1000).toLocaleString('ru-RU', {day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'});
const who = e => e.username ? '@' + esc(e.username) : (e.full_name ? esc(e.full_name) : (e.user_id ? '#'+e.user_id : '—'));

function tile(k, v, sub){
  return `<div class="card"><div class="k">${k}</div><div class="v">${v}${sub?` <small>${sub}</small>`:''}</div></div>`;
}

function chart(daily){
  const max = Math.max(1, ...daily.map(d => d.analyses));
  const bars = daily.map(d =>
    `<i style="height:${Math.round(d.analyses / max * 100)}%" title="${d.day}: ${d.analyses} разборов, ${d.users} чел."></i>`
  ).join('');
  return `<div class="card"><div class="k">Разборов в день (30 дней) · максимум ${max}</div>
    <div class="bar">${bars}</div>
    <div class="axis"><span>${daily[0]?.day ?? ''}</span><span>${daily[daily.length-1]?.day ?? ''}</span></div></div>`;
}

function breakdown(title, obj, labels){
  const items = Object.entries(obj || {}).sort((a,b) => b[1]-a[1]);
  if (!items.length) return `<div class="card"><div class="k">${title}</div><div class="sub">пока пусто</div></div>`;
  const total = items.reduce((s, [,v]) => s+v, 0);
  const rows = items.map(([k,v]) =>
    `<tr><td>${esc(labels?.[k] ?? k)}</td><td class="mono">${v}</td>
     <td class="sub">${(v/total*100).toFixed(0)}%</td></tr>`).join('');
  return `<div class="card"><div class="k">${title}</div><table>${rows}</table></div>`;
}

function usersTable(users){
  if (!users?.length) return '<div class="card sub">Пользователей пока нет.</div>';
  const rows = users.map(u => `<tr>
    <td>${u.username ? '@'+esc(u.username) : esc(u.full_name||'')}<div class="sub mono">#${u.user_id}</div></td>
    <td class="mono">${u.requests||0}</td><td class="mono">${u.photos||0}</td><td class="mono">${u.texts||0}</td>
    <td class="mono">${u.compositions||0}</td><td class="mono">${u.details||0}</td>
    <td class="mono ${u.errors ? 'bad':''}">${u.errors||0}</td>
    <td class="mono">${money(u.cost_usd)}</td>
    <td class="sub">${when(u.last_seen)}</td></tr>`).join('');
  return `<div class="card scroll"><table>
    <thead><tr><th>Пользователь</th><th>Запросов</th><th>Фото</th><th>Текст</th>
    <th>Состав</th><th>Подробнее</th><th>Ошибок</th><th>Расход</th><th>Последний раз</th></tr></thead>
    <tbody>${rows}</tbody></table></div>`;
}

function feedTable(events){
  if (!events?.length) return '<div class="card sub">Событий пока нет.</div>';
  const rows = events.map(e => {
    const cls = e.status === 'error' ? 'bad' : (e.status === 'no_inci' ? 'warn' : 'ok');
    return `<tr>
      <td class="sub mono">${when(e.ts)}</td>
      <td>${who(e)}</td>
      <td><span class="tag">${esc(KIND[e.kind] ?? e.kind)}</span></td>
      <td>${esc(e.product || '')}</td>
      <td>${e.risk ? esc(RISK[e.risk] ?? e.risk) : ''}</td>
      <td class="${cls}">${esc(e.status || '')}${e.cached ? ' · из кэша' : ''}</td>
      <td class="mono sub">${dur(e.latency_ms)}</td>
      <td class="mono sub">${e.in_tokens||0}/${e.out_tokens||0}</td>
    </tr>`;
  }).join('');
  return `<div class="card scroll"><table>
    <thead><tr><th>Когда</th><th>Кто</th><th>Что</th><th>Продукт</th><th>Риск</th>
    <th>Итог</th><th>Время</th><th>Токены</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

let tab = 'week';
function render(s){
  const a = s.audience || {}, p = s[tab] || {};
  document.getElementById('gen').textContent = 'обновлено ' + new Date().toLocaleTimeString('ru-RU');

  const app = document.getElementById('app');
  app.innerHTML = `
  <h2>Аудитория</h2>
  <div class="grid">
    ${tile('Всего людей', a.total_users ?? 0)}
    ${tile('Сегодня', a.dau ?? 0, 'чел.')}
    ${tile('За неделю', a.wau ?? 0, 'чел.')}
    ${tile('За месяц', a.mau ?? 0, 'чел.')}
    ${tile('Новых сегодня', a.new_today ?? 0)}
    ${tile('Возвращаются', a.returning ?? 0, 'в разные дни')}
    ${tile('Постоянные', a.engaged ?? 0, '2+ разбора')}
  </div>

  <h2>Использование</h2>
  <div class="tabs">
    <button onclick="setTab('today')"  aria-selected="${tab==='today'}">Сегодня</button>
    <button onclick="setTab('week')"   aria-selected="${tab==='week'}">7 дней</button>
    <button onclick="setTab('month')"  aria-selected="${tab==='month'}">30 дней</button>
    <button onclick="setTab('all')"    aria-selected="${tab==='all'}">Всё время</button>
  </div>
  <div class="grid">
    ${tile('Разборов', p.analyses ?? 0)}
    ${tile('Людей', p.users ?? 0)}
    ${tile('Из кэша', (p.cache_hit_rate ?? 0) + '%', `${p.from_cache ?? 0} шт.`)}
    ${tile('Среднее время', dur(p.avg_ms_live), 'без кэша')}
    ${tile('Состав не найден', p.no_inci ?? 0)}
    ${tile('Ошибок', p.errors ?? 0)}
    ${tile('Токены', ((p.in_tokens ?? 0) + (p.out_tokens ?? 0)).toLocaleString('ru-RU'), 'in+out')}
    ${tile('Расход', money(p.cost_usd), 'оценка')}
  </div>

  <h2>Динамика</h2>
  ${chart(s.daily || [])}

  <h2>Что нажимают</h2>
  <div class="row two">
    ${breakdown('Функции', p.by_kind, KIND)}
    ${breakdown('Уровни риска', p.by_risk, RISK)}
  </div>

  <h2>Частые продукты</h2>
  <div class="card scroll"><table><thead><tr><th>Продукт</th><th>Запросов</th></tr></thead><tbody>
    ${(p.top_products||[]).map(t => `<tr><td>${esc(t.name)}</td><td class="mono">${t.count}</td></tr>`).join('')
      || '<tr><td class="sub">пока пусто</td><td></td></tr>'}
  </tbody></table></div>

  <h2>Люди</h2>
  ${usersTable(s.top_users)}

  <h2>Живой поток</h2>
  ${feedTable(s.recent)}

  <h2>Лог</h2>
  <div class="card"><pre id="logs">…</pre></div>`;
}

function setTab(t){ tab = t; if (window.__last) render(window.__last); }

async function load(){
  try{
    const r = await fetch('/api/stats?key=' + encodeURIComponent(KEY));
    if (!r.ok) { document.getElementById('app').innerHTML =
      '<div class="card">Нет доступа. Проверь ссылку с ключом.</div>'; return; }
    window.__last = await r.json();
    render(window.__last);
    const l = await fetch('/api/logs?key=' + encodeURIComponent(KEY) + '&limit=250');
    if (l.ok){ const d = await l.json();
      const el = document.getElementById('logs');
      if (el) el.textContent = (d.lines||[]).join('\\n') || 'Лог пока пуст.'; }
  }catch(e){ console.error(e); }
}
load();
setInterval(load, 30000);
</script></body></html>
"""


async def page(request: web.Request) -> web.Response:
    if not _authorized(request):
        return web.Response(
            text="<h1>403</h1><p>Нужен правильный ключ: /dash?key=…</p>",
            content_type="text/html",
            status=403,
        )
    body = (
        _PAGE
        .replace("__KIND__", json.dumps(_KIND_LABELS, ensure_ascii=False))
        .replace("__RISK__", json.dumps(_RISK_LABELS, ensure_ascii=False))
    )
    return web.Response(
        text=body,
        content_type="text/html",
        headers={"X-Robots-Tag": "noindex, nofollow", "Cache-Control": "no-store"},
    )


# ─────────────────────────────────────────────────────────────
# Сервер
# ─────────────────────────────────────────────────────────────

async def build_app() -> web.Application:
    app = web.Application()

    async def health(request: web.Request) -> web.Response:
        return web.Response(text="ok")

    # Существующие проверки живости — без изменений
    app.router.add_get("/", health)
    app.router.add_get("/health", health)

    if config.DASHBOARD_ENABLED:
        app.router.add_get("/dash", page)
        app.router.add_get("/api/stats", api_stats)
        app.router.add_get("/api/events", api_events)
        app.router.add_get("/api/logs", api_logs)
        app.router.add_get("/export.csv", export_csv)

    return app


_runner: Optional[web.AppRunner] = None


async def start() -> None:
    global _runner
    app = await build_app()
    _runner = web.AppRunner(app)
    await _runner.setup()
    site = web.TCPSite(_runner, host="0.0.0.0", port=config.RENDER_PORT)
    await site.start()
    logging.info("Health server started on port %s", config.RENDER_PORT)
    if config.DASHBOARD_ENABLED:
        note = " (сгенерирован, задай DASHBOARD_TOKEN в .env чтобы он не менялся)" if _TOKEN_IS_GENERATED else ""
        logging.info("Дашборд: %s%s", dashboard_url(), note)


async def stop() -> None:
    global _runner
    if _runner is not None:
        try:
            await _runner.cleanup()
        except Exception:  # noqa: BLE001
            pass
        _runner = None
