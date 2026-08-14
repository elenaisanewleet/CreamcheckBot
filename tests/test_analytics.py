"""Аналитика: события, счётчики, кэш, устойчивость к сбоям."""

import asyncio

import pytest

from bot import analytics


class FakeUser:
    def __init__(self, uid, username=None, full_name=None):
        self.id = uid
        self.username = username
        self.full_name = full_name


@pytest.fixture(autouse=True)
def clean_db():
    analytics.init()
    analytics._exec("DELETE FROM events")
    analytics._exec("DELETE FROM users")
    analytics._exec("DELETE FROM cache")
    analytics._exec("DELETE FROM url_ok")
    yield


def run(coro):
    return asyncio.run(coro)


# ── События и счётчики ─────────────────────────────────────

def test_track_creates_user_and_event():
    run(analytics.track(kind="text", user=FakeUser(1, "lena"), chat_id=1, product="Крем"))
    stats = run(analytics.period_stats(0))
    assert stats["analyses"] == 1
    assert stats["users"] == 1

    people = run(analytics.top_users())
    assert people[0]["user_id"] == 1
    assert people[0]["username"] == "lena"
    assert people[0]["requests"] == 1
    assert people[0]["texts"] == 1


def test_counters_split_by_function():
    u = FakeUser(2, "liza")
    run(analytics.track(kind="photo", user=u, chat_id=2))
    run(analytics.track(kind="composition", user=u, chat_id=2))
    run(analytics.track(kind="step2", user=u, chat_id=2))
    run(analytics.track(kind="step2", user=u, chat_id=2))

    row = run(analytics.top_users())[0]
    assert row["photos"] == 1
    assert row["compositions"] == 1
    assert row["details"] == 2


def test_errors_are_counted():
    run(analytics.track(kind="text", user=FakeUser(3), chat_id=3, status="error", detail="boom"))
    assert run(analytics.period_stats(0))["errors"] == 1
    assert run(analytics.recent_errors())[0]["detail"] == "boom"


def test_cache_hit_rate():
    u = FakeUser(4)
    run(analytics.track(kind="text", user=u, chat_id=4))
    run(analytics.track(kind="text", user=u, chat_id=4, cached=True))
    stats = run(analytics.period_stats(0))
    assert stats["analyses"] == 2
    assert stats["from_cache"] == 1
    assert stats["cache_hit_rate"] == 50.0


def test_audience_counts_distinct_people():
    run(analytics.track(kind="text", user=FakeUser(10), chat_id=1))
    run(analytics.track(kind="text", user=FakeUser(11), chat_id=1))
    run(analytics.track(kind="text", user=FakeUser(10), chat_id=1))
    a = run(analytics.audience())
    assert a["total_users"] == 2
    assert a["dau"] == 2
    assert a["engaged"] == 1  # только у 10 два разбора


def test_service_commands_are_not_counted_as_analyses():
    run(analytics.track(kind="start", user=FakeUser(20), chat_id=1))
    run(analytics.track(kind="help", user=FakeUser(20), chat_id=1))
    stats = run(analytics.period_stats(0))
    assert stats["analyses"] == 0
    assert stats["events"] == 2


# ── Стоимость ──────────────────────────────────────────────

def test_cost_estimation_uses_cached_discount():
    full = analytics.estimate_cost(in_tokens=1_000_000, out_tokens=0)
    discounted = analytics.estimate_cost(in_tokens=1_000_000, cached_tokens=1_000_000, out_tokens=0)
    assert full > discounted > 0


def test_cost_counts_output_tokens():
    assert analytics.estimate_cost(out_tokens=1_000_000) > 0


# ── Кэш ────────────────────────────────────────────────────

def test_cache_roundtrip():
    run(analytics.cache_put("k1", "step1", {"product_name": "Крем"}))
    assert run(analytics.cache_get("k1"))["product_name"] == "Крем"
    assert run(analytics.cache_get("missing")) is None


def test_cache_clear():
    run(analytics.cache_put("k1", "step1", {"a": 1}))
    run(analytics.cache_put("k2", "step1", {"a": 2}))
    assert run(analytics.cache_clear()) == 2
    assert run(analytics.cache_get("k1")) is None


def test_cache_counts_hits():
    run(analytics.cache_put("k", "step1", {"a": 1}))
    run(analytics.cache_get("k"))
    run(analytics.cache_get("k"))
    assert run(analytics.cache_stats())["hits"] == 2


def test_url_check_cache():
    run(analytics.url_check_put("https://example.com", True))
    assert run(analytics.url_check_get("https://example.com")) is True
    assert run(analytics.url_check_get("https://other.com")) is None


# ── Устойчивость ───────────────────────────────────────────

def test_track_never_raises_on_broken_db(monkeypatch):
    """Даже если база недоступна, бот обязан продолжить работу."""
    def boom(*args, **kwargs):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(analytics, "_write_event", boom)
    run(analytics.track(kind="text", user=FakeUser(99), chat_id=1))  # не должно бросить


def test_export_csv_has_header():
    run(analytics.track(kind="text", user=FakeUser(5, "x"), chat_id=1, product="Крем"))
    csv_text = run(analytics.export_csv())
    assert csv_text.splitlines()[0].startswith("id,ts,day,user_id")
    assert "Крем" in csv_text


def test_recent_events_newest_first():
    run(analytics.track(kind="text", user=FakeUser(6), chat_id=1, product="Первый"))
    run(analytics.track(kind="text", user=FakeUser(6), chat_id=1, product="Второй"))
    rows = run(analytics.recent_events(10))
    assert rows[0]["product"] == "Второй"


def test_daily_series_covers_30_days():
    snap = run(analytics.snapshot())
    assert len(snap["daily"]) == 30
