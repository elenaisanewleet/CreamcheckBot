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
