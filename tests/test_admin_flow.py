"""Админка целиком: /panel, правка базы и тот же путь через веб-дашборд.

`test_base_edit.py` проверяет саму механику правок. Здесь — то, что она
делает в руках человека и в обоих входах сразу:

  • каждая кнопка панели рисует экран, а не молчит;
  • после ЛЮБОЙ правки — из Telegram или из дашборда — кэш готовых разборов
    пуст, иначе люди продолжат видеть вердикты по прежней базе;
  • эталон в `agent/comedogen_base.py` не меняется ни при каком сценарии.

Фейки Telegram — общие (`tools/tg_fakes.py`) и повторяют aiogram 3:
`answer()` и `edit_text()` возвращают awaitable-объект, а не корутину.
"""

import asyncio

import pytest
from aiohttp.test_utils import TestClient, TestServer

from agent.comedogen_base import conditional_comedogens, hard_comedogens
from bot import admin, analytics, comedogen_store as store, dashboard
from tools.tg_fakes import FakeCallbackQuery, FakeMessage, FakeUser, buttons, screens, texts

HARD = store.KIND_HARD
COND = store.KIND_COND
KEY = dashboard.DASHBOARD_TOKEN
HDR = {"X-CC-Key": KEY}

ADMIN = FakeUser(41082373, "elena")          # есть в ADMIN_IDS по умолчанию
OUTSIDER = FakeUser(999999, "stranger")

NEW_COMPONENT = "isopropyl myristate"


@pytest.fixture(autouse=True)
def clean():
    analytics.init()
    analytics._exec("DELETE FROM base_overrides")
    analytics._exec("DELETE FROM cache")
    analytics._exec("DELETE FROM events")
    store.invalidate()
    admin._awaiting_add.clear()
    yield
    analytics._exec("DELETE FROM base_overrides")
    store.invalidate()
    admin._awaiting_add.clear()


@pytest.fixture
async def client():
    app = await dashboard.build_app()
    async with TestClient(TestServer(app)) as c:
        yield c


def run(coro):
    return asyncio.run(coro)


def press(data, user=ADMIN):
    """Нажатие кнопки админки; возвращает callback со всем, что он показал."""
    cb = FakeCallbackQuery(data, user=user)
    run(admin.on_callback(cb))
    return cb


def shown(cb):
    return [m.text for m in screens(cb.message)]


def seed_cache():
    """Кладём готовый разбор — правка базы обязана его выбросить."""
    run(analytics.cache_put("step1:тест", "step1", {"risk_level": "none"}))
    assert run(analytics.cache_get("step1:тест")) is not None


def cache_is_empty():
    return run(analytics.cache_get("step1:тест")) is None


# ─────────────────────────────────────────────────────────────
# /panel: каждая кнопка рисует экран
# ─────────────────────────────────────────────────────────────

def test_panel_opens(monkeypatch):
    msg = FakeMessage(text="/panel")
    run(admin.cmd_panel(msg))
    assert texts(msg), "/panel промолчал"
    labels = [label for label, _ in buttons(msg.sent[-1].reply_markup)]
    assert "🧪 База комедогенов" in labels


@pytest.mark.parametrize(
    "data",
    ["a:panel", "a:base", "a:hist", "a:list:hard:0", "a:list:conditional:0", "a:reset"],
)
def test_every_panel_button_draws_a_screen(data):
    cb = press(data)
    assert shown(cb), f"кнопка {data} ничего не показала"


def test_unknown_list_says_so_instead_of_silence():
    cb = press("a:list:выдумка:0")
    assert cb.answers[-1]["show_alert"] is True


def test_outsider_sees_nothing(monkeypatch):
    seed_cache()
    cb = press("a:base", user=OUTSIDER)
    assert not shown(cb), "чужому показали админский экран"
    assert not cache_is_empty(), "чужой смог тронуть базу"


def test_outsider_learns_nothing_even_when_telegram_fails():
    """Отказ Telegram не должен превращать отказ в доступе в сообщение.

    Иначе посторонний, нажавший чужую кнопку, узнаёт из текста ошибки,
    что панель существует.
    """
    cb = FakeCallbackQuery("a:base", user=OUTSIDER, answer_error=RuntimeError("query is too old"))
    run(admin.on_callback(cb))
    assert not shown(cb)


def test_admin_button_never_answers_with_silence(monkeypatch):
    """Упавшая кнопка админки — та же тишина, что была на фото."""

    def _boom():
        raise RuntimeError("сломался рендер")

    monkeypatch.setattr(admin, "_base_text", _boom)
    cb = press("a:base")
    assert shown(cb), "админ нажал кнопку и не получил ничего"
    assert admin.ADMIN_ERROR in shown(cb)


# ─────────────────────────────────────────────────────────────
# Правка из Telegram: добавить, убрать, отменить, сбросить
# ─────────────────────────────────────────────────────────────

def test_add_through_the_panel_applies_and_clears_cache():
    seed_cache()

    cb = press(f"a:add:{HARD}")
    assert admin.waiting_for_name(ADMIN), "панель не стала ждать название"
    assert shown(cb), "не объяснили, что делать дальше"

    msg = FakeMessage(text=NEW_COMPONENT)
    run(admin.on_add_text(msg))

    assert NEW_COMPONENT in store.effective()[0]
    assert texts(msg), "о добавлении не сказали ни слова"
    assert cache_is_empty(), "кэш разборов остался — люди увидят старый вердикт"


def test_remove_through_the_panel_asks_then_applies():
    seed_cache()
    items = store.listing(HARD)
    idx = next(i for i, it in enumerate(items) if it["name"] == "petrolatum")
    sig = admin._name_hash("petrolatum")

    ask = press(f"a:rm:{HARD}:{idx}:{sig}")
    assert any("Убрать" in t for t in shown(ask)), "удаление не спросило подтверждения"
    assert "petrolatum" in store.effective()[0], "убрали без подтверждения"

    done = press(f"a:rmy:{HARD}:{idx}:{sig}")
    assert shown(done)
    assert "petrolatum" not in store.effective()[0]
    assert cache_is_empty()


def test_undo_through_the_panel_restores_and_clears_cache():
    run(store.add(NEW_COMPONENT, HARD, ADMIN))
    assert NEW_COMPONENT in store.effective()[0]
    entry = store.history()[0]

    seed_cache()
    cb = press(f"a:undo:{entry['id']}")

    assert NEW_COMPONENT not in store.effective()[0]
    assert shown(cb) or cb.answers, "отмена правки прошла молча"
    assert cache_is_empty(), "после отмены правки кэш не сбросили"


def test_reset_through_the_panel_needs_confirmation():
    run(store.add(NEW_COMPONENT, HARD, ADMIN))
    seed_cache()

    ask = press("a:reset")
    assert any("Сбросить" in t for t in shown(ask))
    assert NEW_COMPONENT in store.effective()[0], "сбросили без подтверждения"

    done = press("a:resety")
    assert shown(done)
    assert store.effective()[0] == store.BASELINE_HARD
    assert cache_is_empty()


# ─────────────────────────────────────────────────────────────
# Тот же путь через веб-дашборд
# ─────────────────────────────────────────────────────────────

async def _post(client, payload):
    return await client.post(f"/api/base?key={KEY}", headers=HDR, json=payload)


async def test_web_add_clears_the_analysis_cache(client):
    await analytics.cache_put("step1:тест", "step1", {"risk_level": "none"})
    r = await _post(client, {"action": "add", "kind": HARD, "name": NEW_COMPONENT})

    assert r.status == 200
    assert NEW_COMPONENT in store.effective()[0]
    assert await analytics.cache_get("step1:тест") is None


async def test_web_drop_clears_the_analysis_cache(client):
    await analytics.cache_put("step1:тест", "step1", {"risk_level": "none"})
    r = await _post(client, {"action": "drop", "kind": HARD, "name": "petrolatum"})

    assert r.status == 200
    assert "petrolatum" not in store.effective()[0]
    assert await analytics.cache_get("step1:тест") is None


async def test_web_undo_clears_the_analysis_cache(client):
    await _post(client, {"action": "add", "kind": HARD, "name": NEW_COMPONENT})
    entry = store.history()[0]
    await analytics.cache_put("step1:тест", "step1", {"risk_level": "none"})

    r = await _post(client, {"action": "undo", "id": entry["id"]})

    assert (await r.json())["result"] == "undone"
    assert NEW_COMPONENT not in store.effective()[0]
    assert await analytics.cache_get("step1:тест") is None


async def test_web_reset_clears_the_analysis_cache(client):
    await _post(client, {"action": "add", "kind": HARD, "name": NEW_COMPONENT})
    await analytics.cache_put("step1:тест", "step1", {"risk_level": "none"})

    r = await _post(client, {"action": "reset"})

    assert r.status == 200
    assert store.effective()[0] == store.BASELINE_HARD
    assert await analytics.cache_get("step1:тест") is None


async def test_web_and_telegram_edit_the_same_base(client):
    """Два входа — одна база: правка из дашборда видна в панели и наоборот."""
    baseline_count = len(store.BASELINE_HARD)

    await _post(client, {"action": "add", "kind": HARD, "name": NEW_COMPONENT})
    assert any(it["name"] == NEW_COMPONENT for it in store.listing(HARD))

    # Панель в Telegram показывает ту же добавленную правку.
    cb = FakeCallbackQuery("a:base", user=ADMIN)
    await admin.on_callback(cb)
    base_screen = "\n".join(shown(cb))
    assert str(baseline_count + 1) in base_screen, base_screen

    # И обратно: правка из Telegram видна в дашборде.
    await store.drop(NEW_COMPONENT, HARD, ADMIN)
    data = await (await client.get(f"/api/base?key={KEY}")).json()
    assert all(it["name"] != NEW_COMPONENT for it in data["hard"])
    assert data["stats"]["hard"] == baseline_count


# ─────────────────────────────────────────────────────────────
# Эталон неприкосновенен — при любом сценарии
# ─────────────────────────────────────────────────────────────

async def test_baseline_module_survives_every_kind_of_edit(client):
    """Правки живут отдельным слоем; сами списки в agent/ не трогаются."""
    hard_before = set(hard_comedogens)
    cond_before = dict(conditional_comedogens)

    await _post(client, {"action": "add", "kind": HARD, "name": NEW_COMPONENT})
    await _post(client, {"action": "drop", "kind": HARD, "name": "petrolatum"})
    await _post(client, {"action": "add", "kind": COND, "name": "polysilicone-11"})
    await store.add("beeswax", HARD, ADMIN)
    await store.drop("lanolin", HARD, ADMIN)

    assert set(hard_comedogens) == hard_before, "эталонный список жёстких изменился"
    assert dict(conditional_comedogens) == cond_before, "эталонный список условных изменился"
    assert store.BASELINE_HARD == frozenset(hard_before)

    await _post(client, {"action": "reset"})
    assert store.effective()[0] == store.BASELINE_HARD
    assert store.effective()[1] == store.BASELINE_COND
    assert set(hard_comedogens) == hard_before


async def test_reset_returns_to_baseline_from_any_mess(client):
    for i in range(5):
        await _post(client, {"action": "add", "kind": HARD, "name": f"выдумка {i}"})
    await _post(client, {"action": "drop", "kind": HARD, "name": "petrolatum"})
    await store.add("ещё одна", COND, ADMIN)

    await _post(client, {"action": "reset"})

    assert store.effective()[0] == store.BASELINE_HARD
    assert store.effective()[1] == store.BASELINE_COND
