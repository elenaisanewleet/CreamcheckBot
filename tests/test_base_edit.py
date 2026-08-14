"""Правка базы комедогенов из админки.

Главное, что здесь проверяется, — что эталон неприкосновенен:
без правок база побайтово равна эталонной, а «Сбросить к эталону» возвращает
её в исходное состояние из любого состояния.

Плюс то, что легко упустить: правка обязана сбрасывать кэш готовых разборов,
иначе люди продолжат видеть вердикты, посчитанные по прежней базе.
"""

import asyncio

import pytest

from agent.agent import classify_ingredient_strict
from agent.comedogen_base import conditional_comedogens, hard_comedogens
from bot import admin, analytics, comedogen_store as store

HARD = store.KIND_HARD
COND = store.KIND_COND


class FakeUser:
    id = 41082373  # Лена, есть в ADMIN_IDS по умолчанию
    username = "elena"
    full_name = "Лена"


class Outsider:
    id = 999999
    username = "stranger"
    full_name = "Чужой"


class FakeChat:
    id = 555


class FakeMessage:
    def __init__(self):
        self.from_user = FakeUser()
        self.chat = FakeChat()
        self.sent = []
        self.edits = []

    async def answer(self, text, reply_markup=None, **kw):
        self.sent.append((text, reply_markup))
        return self

    async def edit_text(self, text, reply_markup=None, **kw):
        self.edits.append((text, reply_markup))
        return self


class FakeCallback:
    def __init__(self, data, user=None):
        self.data = data
        self.from_user = user or FakeUser()
        self.message = FakeMessage()
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


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


def run(coro):
    return asyncio.run(coro)


def buttons(markup):
    return [b.text for row in markup.inline_keyboard for b in row]


def callbacks(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


# ─────────────────────────────────────────────────────────────
# Эталон неприкосновенен
# ─────────────────────────────────────────────────────────────

def test_without_overrides_base_is_exactly_the_baseline():
    hard, cond = store.effective()
    assert hard == frozenset(hard_comedogens)
    assert cond == dict(conditional_comedogens)


def test_reset_returns_to_baseline_from_any_state():
    run(store.add("isopropyl myristate", HARD, FakeUser()))
    run(store.drop("petrolatum", HARD, FakeUser()))
    run(store.add("jojoba oil", COND, FakeUser()))
    hard, _ = store.effective()
    assert "isopropyl myristate" in hard and "petrolatum" not in hard

    assert run(store.reset()) == 3
    hard, cond = store.effective()
    assert hard == frozenset(hard_comedogens)
    assert cond == dict(conditional_comedogens)


def test_baseline_module_is_never_mutated():
    """Правки не должны протекать в импортированные из кода списки."""
    before_hard = set(hard_comedogens)
    before_cond = dict(conditional_comedogens)

    run(store.add("isopropyl myristate", HARD, FakeUser()))
    run(store.drop("lanolin", HARD, FakeUser()))
    store.effective()

    assert set(hard_comedogens) == before_hard
    assert dict(conditional_comedogens) == before_cond


# ─────────────────────────────────────────────────────────────
# Правка действительно меняет вердикт
# ─────────────────────────────────────────────────────────────

def test_added_component_starts_being_flagged():
    assert classify_ingredient_strict("Isopropyl Myristate", 3)["is_hard"] is False

    assert run(store.add("isopropyl myristate", HARD, FakeUser())) == "added"
    assert classify_ingredient_strict("Isopropyl Myristate", 3)["is_hard"] is True


def test_dropped_component_stops_being_flagged():
    assert classify_ingredient_strict("Petrolatum", 2)["is_hard"] is True

    assert run(store.drop("petrolatum", HARD, FakeUser())) == "dropped"
    assert classify_ingredient_strict("Petrolatum", 2)["is_hard"] is False


def test_wax_special_rule_obeys_the_base():
    """Отдельное правило про wax тоже должно слушаться базы, а не жить своей жизнью."""
    assert classify_ingredient_strict("Candelilla Wax", 4)["is_hard"] is True

    run(store.drop("wax", HARD, FakeUser()))
    assert classify_ingredient_strict("Candelilla Wax", 4)["is_hard"] is False


def test_conditional_added_gets_default_cutoff():
    run(store.add("jojoba oil", COND, FakeUser()))
    early = classify_ingredient_strict("Jojoba Oil", 2)
    late = classify_ingredient_strict("Jojoba Oil", 20)
    assert early["is_conditional"] and early["early_conditional"]
    assert late["is_conditional"] and not late["early_conditional"]


# ─────────────────────────────────────────────────────────────
# Отмены и повторы
# ─────────────────────────────────────────────────────────────

def test_adding_back_a_dropped_component_is_a_revert():
    run(store.drop("petrolatum", HARD, FakeUser()))
    assert run(store.add("petrolatum", HARD, FakeUser())) == "restored"
    assert "petrolatum" in store.effective()[0]
    # Обе правки погашены — в истории не должно остаться активных.
    assert [r for r in store.history() if r["active"]] == []


def test_dropping_your_own_addition_is_a_revert():
    run(store.add("isopropyl myristate", HARD, FakeUser()))
    assert run(store.drop("isopropyl myristate", HARD, FakeUser())) == "reverted"
    assert "isopropyl myristate" not in store.effective()[0]
    assert [r for r in store.history() if r["active"]] == []


def test_duplicate_add_is_rejected():
    assert run(store.add("petrolatum", HARD, FakeUser())) == "exists"
    assert run(store.drop("nonexistent thing", HARD, FakeUser())) == "missing"
    assert run(store.add("", HARD, FakeUser())) == "empty"
    assert run(store.add("x", "странный вид", FakeUser())) == "bad_kind"


def test_undo_restores_previous_state():
    run(store.add("isopropyl myristate", HARD, FakeUser()))
    entry = store.history()[0]
    assert "isopropyl myristate" in store.effective()[0]

    assert run(store.undo(entry["id"])) is True
    run(store._applied())
    assert "isopropyl myristate" not in store.effective()[0]
    assert run(store.undo(entry["id"])) is False  # повторно нельзя


def test_names_are_normalized():
    run(store.add("  ISOPROPYL   Myristate ", HARD, FakeUser()))
    assert "isopropyl myristate" in store.effective()[0]


def test_history_records_author():
    run(store.add("isopropyl myristate", HARD, FakeUser()))
    entry = store.history()[0]
    assert entry["author"] == "Лена"
    assert entry["action"] == store.ACTION_ADD


# ─────────────────────────────────────────────────────────────
# Кэш
# ─────────────────────────────────────────────────────────────

def test_edit_clears_the_analysis_cache():
    """Иначе люди увидят вердикты, посчитанные по прежней базе."""
    run(analytics.cache_put("s2:demo", "step2", {"summary": "старое"}))
    assert run(analytics.cache_get("s2:demo")) is not None

    run(store.add("isopropyl myristate", HARD, FakeUser()))
    assert run(analytics.cache_get("s2:demo")) is None


def test_reset_also_clears_the_cache():
    run(store.add("isopropyl myristate", HARD, FakeUser()))
    run(analytics.cache_put("s2:demo", "step2", {"summary": "после правки"}))
    run(store.reset())
    assert run(analytics.cache_get("s2:demo")) is None


# ─────────────────────────────────────────────────────────────
# Экраны и кнопки админки
# ─────────────────────────────────────────────────────────────

def test_listing_marks_added_components():
    run(store.add("isopropyl myristate", HARD, FakeUser()))
    items = {i["name"]: i["baseline"] for i in store.listing(HARD)}
    assert items["isopropyl myristate"] is False
    assert items["petrolatum"] is True


def test_panel_and_base_screens_render():
    cb = FakeCallback("a:panel")
    run(admin.on_callback(cb))
    assert "Панель управления" in cb.message.edits[-1][0]

    cb = FakeCallback("a:base")
    run(admin.on_callback(cb))
    text, markup = cb.message.edits[-1]
    assert "База комедогенов" in text
    assert "a:list:hard:0" in callbacks(markup)


def test_list_screen_paginates():
    cb = FakeCallback("a:list:hard:0")
    run(admin.on_callback(cb))
    text, markup = cb.message.edits[-1]
    assert "страница 1 из" in text
    assert any(c.startswith("a:rm:hard:") for c in callbacks(markup))
    assert "a:list:hard:1" in callbacks(markup)  # есть кнопка «вперёд»


def test_removal_requires_confirmation():
    cb = FakeCallback("a:list:hard:0")
    run(admin.on_callback(cb))
    rm = next(c for c in callbacks(cb.message.edits[-1][1]) if c.startswith("a:rm:hard:"))

    confirm = FakeCallback(rm)
    run(admin.on_callback(confirm))
    text, markup = confirm.message.sent[-1]
    assert "Убрать" in text
    assert any(c.startswith("a:rmy:") for c in callbacks(markup))
    # Пока не подтвердили — база не тронута.
    assert store.effective()[0] == frozenset(hard_comedogens)


def test_confirmed_removal_applies():
    cb = FakeCallback("a:list:hard:0")
    run(admin.on_callback(cb))
    rm = next(c for c in callbacks(cb.message.edits[-1][1]) if c.startswith("a:rm:hard:"))
    _, _, kind, idx, sig = rm.split(":")
    name = store.listing(kind)[int(idx)]["name"]

    run(admin.on_callback(FakeCallback(f"a:rmy:{kind}:{idx}:{sig}")))
    assert name not in store.effective()[0]


def test_stale_index_is_refused():
    """Подпись названия ловит рассинхрон: список изменился между показом и нажатием."""
    cb = FakeCallback("a:rmy:hard:0:deadbe")
    run(admin.on_callback(cb))
    assert cb.answers[-1][1] is True  # show_alert
    assert store.effective()[0] == frozenset(hard_comedogens)


def test_adding_via_panel_waits_for_the_name():
    cb = FakeCallback("a:add:hard")
    run(admin.on_callback(cb))
    assert admin.waiting_for_name(FakeUser())

    msg = FakeMessage()
    msg.text = "isopropyl myristate"
    run(admin.on_add_text(msg))

    assert "isopropyl myristate" in store.effective()[0]
    assert not admin.waiting_for_name(FakeUser())  # состояние снято


def test_too_long_name_is_rejected():
    run(admin.on_callback(FakeCallback("a:add:hard")))
    msg = FakeMessage()
    msg.text = "x" * 41
    run(admin.on_add_text(msg))
    assert "Слишком длинное" in msg.sent[-1][0]
    assert store.effective()[0] == frozenset(hard_comedogens)


def test_outsider_cannot_touch_the_base():
    """Кнопки админки проверяют права сами, а не полагаются на фильтр команды."""
    cb = FakeCallback("a:rmy:hard:0:deadbe", user=Outsider())
    run(admin.on_callback(cb))
    assert cb.message.sent == [] and cb.message.edits == []

    cb2 = FakeCallback("a:add:hard", user=Outsider())
    run(admin.on_callback(cb2))
    assert not admin.waiting_for_name(Outsider())
    assert store.effective()[0] == frozenset(hard_comedogens)


def test_reset_button_needs_confirmation():
    run(store.add("isopropyl myristate", HARD, FakeUser()))

    cb = FakeCallback("a:reset")
    run(admin.on_callback(cb))
    assert "Сбросить" in cb.message.sent[-1][0]
    assert "isopropyl myristate" in store.effective()[0]  # ещё не сброшено

    run(admin.on_callback(FakeCallback("a:resety")))
    assert store.effective()[0] == frozenset(hard_comedogens)
