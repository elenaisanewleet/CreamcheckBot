"""Дашборд: health открыт, статистика — только по ключу."""

import pytest

from aiohttp.test_utils import TestClient, TestServer

from bot import dashboard


@pytest.fixture
async def client():
    app = await dashboard.build_app()
    async with TestClient(TestServer(app)) as c:
        yield c


async def test_health_stays_open(client):
    """Проверка живости не должна ломаться — на неё смотрит хостинг."""
    for path in ("/", "/health"):
        resp = await client.get(path)
        assert resp.status == 200
        assert (await resp.text()) == "ok"


async def test_stats_requires_key(client):
    assert (await client.get("/api/stats")).status == 403
    assert (await client.get("/api/stats?key=wrong")).status == 403


async def test_stats_with_key(client):
    resp = await client.get(f"/api/stats?key={dashboard.DASHBOARD_TOKEN}")
    assert resp.status == 200
    data = await resp.json()
    assert "audience" in data and "daily" in data


async def test_dash_page_requires_key(client):
    assert (await client.get("/dash")).status == 403
    ok = await client.get(f"/dash?key={dashboard.DASHBOARD_TOKEN}")
    assert ok.status == 200
    body = await ok.text()
    assert "CreamcheckBot" in body
    # страница обязана быть самодостаточной: без внешних скриптов
    assert "http://cdn" not in body and "<script src=" not in body


async def test_bearer_token_works(client):
    resp = await client.get(
        "/api/stats", headers={"Authorization": f"Bearer {dashboard.DASHBOARD_TOKEN}"}
    )
    assert resp.status == 200


async def test_csv_export_requires_key(client):
    assert (await client.get("/export.csv")).status == 403
    ok = await client.get(f"/export.csv?key={dashboard.DASHBOARD_TOKEN}")
    assert ok.status == 200
    assert "text/csv" in ok.headers["Content-Type"]


async def test_logs_endpoint(client):
    resp = await client.get(f"/api/logs?key={dashboard.DASHBOARD_TOKEN}")
    assert resp.status == 200
    assert "lines" in await resp.json()


# ─────────────────────────────────────────────────────────────
# База комедогенов в дашборде
# ─────────────────────────────────────────────────────────────

from bot import analytics, comedogen_store as store  # noqa: E402

KEY = dashboard.DASHBOARD_TOKEN
HDR = {"X-CC-Key": KEY}


@pytest.fixture(autouse=True)
def clean_base():
    analytics.init()
    analytics._exec("DELETE FROM base_overrides")
    store.invalidate()
    yield
    analytics._exec("DELETE FROM base_overrides")
    store.invalidate()


def test_non_ascii_token_does_not_crash_comparison():
    """hmac.compare_digest со строками падает TypeError на кириллице.

    Пароль вполне могут придумать по-русски — тогда дашборд отвечал бы
    500 на каждый запрос, включая health-проверку страницы.
    """
    assert dashboard._same("пароль", "пароль") is True
    assert dashboard._same("пароль", "другой") is False
    assert dashboard._same("ascii", "ascii") is True


async def test_base_requires_key(client):
    assert (await client.get("/api/base")).status == 403
    assert (await client.post("/api/base", json={"action": "reset"})).status == 403


async def test_base_lists_both_kinds(client):
    resp = await client.get(f"/api/base?key={KEY}")
    assert resp.status == 200
    data = await resp.json()
    assert len(data["hard"]) == len(store.BASELINE_HARD)
    assert len(data["cond"]) == len(store.BASELINE_COND)
    assert all("baseline" in i for i in data["hard"])


async def test_add_and_drop_through_api(client):
    r = await client.post(f"/api/base?key={KEY}", headers=HDR,
                          json={"action": "add", "kind": "hard", "name": "isopropyl myristate"})
    assert r.status == 200 and (await r.json())["result"] == "added"
    assert "isopropyl myristate" in store.effective()[0]

    r = await client.post(f"/api/base?key={KEY}", headers=HDR,
                          json={"action": "drop", "kind": "hard", "name": "petrolatum"})
    assert r.status == 200
    assert "petrolatum" not in store.effective()[0]


async def test_reset_returns_to_baseline(client):
    await client.post(f"/api/base?key={KEY}", headers=HDR,
                      json={"action": "add", "kind": "hard", "name": "isopropyl myristate"})
    r = await client.post(f"/api/base?key={KEY}", headers=HDR, json={"action": "reset"})
    assert r.status == 200
    assert store.effective()[0] == store.BASELINE_HARD


async def test_undo_through_api(client):
    await client.post(f"/api/base?key={KEY}", headers=HDR,
                      json={"action": "add", "kind": "hard", "name": "isopropyl myristate"})
    entry = store.history()[0]
    r = await client.post(f"/api/base?key={KEY}", headers=HDR,
                          json={"action": "undo", "id": entry["id"]})
    assert (await r.json())["result"] == "undone"
    assert "isopropyl myristate" not in store.effective()[0]


async def test_web_edits_are_marked_in_history(client):
    """В истории должно быть видно, что правка пришла из дашборда, а не из Telegram."""
    await client.post(f"/api/base?key={KEY}", headers=HDR,
                      json={"action": "add", "kind": "hard", "name": "isopropyl myristate"})
    assert store.history()[0]["author"] == "веб-дашборд"


async def test_cookie_alone_cannot_edit(client):
    """Защита от подделки запроса с чужого сайта.

    Cookie браузер отправляет сам, поэтому чужая страница могла бы отправить
    форму и поменять базу. Заголовок X-CC-Key чужой сайт выставить не может.
    """
    client.session.cookie_jar.update_cookies({"cc_key": KEY})
    r = await client.post("/api/base", json={"action": "add", "kind": "hard", "name": "x"})
    assert r.status == 403
    assert store.effective()[0] == store.BASELINE_HARD


async def test_bad_input_is_rejected(client):
    for payload in (
        {"action": "add", "kind": "выдумка", "name": "x"},
        {"action": "add", "kind": "hard", "name": ""},
        {"action": "add", "kind": "hard", "name": "x" * 41},
        {"action": "чепуха"},
    ):
        r = await client.post(f"/api/base?key={KEY}", headers=HDR, json=payload)
        assert r.status == 400, payload
    assert store.effective()[0] == store.BASELINE_HARD


async def test_page_contains_base_section(client):
    html = await (await client.get(f"/dash?key={KEY}")).text()
    assert 'id="base"' in html
    assert "loadBase" in html and "baseAdd" in html
