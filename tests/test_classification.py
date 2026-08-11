"""Проверяем, что логика оценки комедогенности НЕ изменилась.

Это защитный слой: если кто-то случайно тронет списки или правила совпадений,
тесты упадут раньше, чем пользователи увидят другой результат.
"""

from agent.agent import apply_comedogenic_flags_strict, classify_ingredient_strict
from bot import config


def flags(name: str, position: int = 1):
    r = classify_ingredient_strict(name, position)
    return r["is_hard"], r["is_conditional"]


# ── Жёсткие ────────────────────────────────────────────────

def test_hard_exact_terms():
    for name in ["Lanolin", "PETROLATUM", "Paraffinum Liquidum", "Ceresin", "Olive Oil"]:
        assert flags(name)[0] is True, name


def test_wax_as_separate_word_is_hard():
    for name in ["Candelilla Wax", "Carnauba wax", "Ozokerite Wax"]:
        assert flags(name)[0] is True, name


def test_beeswax_is_currently_not_flagged():
    """ЗАФИКСИРОВАННОЕ РАСХОЖДЕНИЕ (не баг тестов, а поведение бота).

    Промпт называет beeswax примером жёсткого комедогена, но код ищет «wax»
    только как отдельное слово, поэтому Beeswax и Cera Alba не отмечаются.
    Исправляется флагом COMEDOGEN_MATCH_V2=true — см. test_match_v2_*.
    """
    assert flags("Beeswax") == (False, False)
    assert flags("Synthetic Beeswax") == (False, False)
    assert flags("Cera Alba") == (False, False)


def test_free_acids_are_hard():
    for name in ["Stearic Acid", "Palmitic Acid", "Myristic acid"]:
        assert flags(name)[0] is True, name


def test_acid_derivatives_are_not_hard():
    """Isopropyl Palmitate и подобные — НЕ жёсткие комедогены."""
    for name in ["Isopropyl Palmitate", "Glyceryl Stearate", "Sorbitan Laurate",
                 "Isopropyl Myristate", "Caprylic/Capric Triglyceride"]:
        is_hard, _ = flags(name)
        assert is_hard is False, name


# ── Условные ───────────────────────────────────────────────

def test_conditional_terms():
    for name in ["Shea Butter", "Squalane", "Grape Seed Oil"]:
        assert flags(name) == (False, True), name


def test_methicone_partial_match():
    for name in ["Dimethicone", "Cyclomethicone", "Dimethicone Crosspolymer"]:
        assert flags(name) == (False, True), name


def test_sil_is_whole_word_only():
    """'sil' не должен ловить Silica — иначе половина составов станет условной."""
    assert flags("Silica") == (False, False)
    assert flags("Silicon Dioxide") == (False, False)


def test_polysilicone_is_currently_not_flagged():
    """ЗАФИКСИРОВАННОЕ РАСХОЖДЕНИЕ: промпт приводит Polysilicone-11 примером
    частичного совпадения по «sil», но код требует отдельного слова."""
    assert flags("Polysilicone-11") == (False, False)


# ── Опциональное исправление (COMEDOGEN_MATCH_V2) ──────────

def test_match_v2_catches_beeswax(monkeypatch):
    monkeypatch.setattr(config, "COMEDOGEN_MATCH_V2", True)
    assert flags("Beeswax")[0] is True
    assert flags("Synthetic Beeswax")[0] is True
    assert flags("Candelilla Wax")[0] is True  # старое поведение сохраняется


def test_match_v2_catches_polysilicone_but_not_silica(monkeypatch):
    monkeypatch.setattr(config, "COMEDOGEN_MATCH_V2", True)
    assert flags("Polysilicone-11") == (False, True)
    assert flags("Silica") == (False, False)  # Silica по-прежнему чистая


def test_match_v2_keeps_neutral_ingredients_neutral(monkeypatch):
    monkeypatch.setattr(config, "COMEDOGEN_MATCH_V2", True)
    for name in ["Aqua", "Glycerin", "Niacinamide", "Isopropyl Palmitate"]:
        assert flags(name) == (False, False), name


def test_neutral_ingredients():
    for name in ["Aqua", "Glycerin", "Niacinamide", "Sodium Hyaluronate", "Panthenol"]:
        assert flags(name) == (False, False), name


def test_generic_oil_is_not_flagged():
    """«Просто масло» без конкретики не отмечается — так задумано в промпте."""
    assert flags("Vegetable Oil") == (False, False)
    assert flags("Oil") == (False, False)


# ── Массовая простановка флагов ────────────────────────────

def test_apply_flags_sets_early_marker():
    ingredients = [
        {"name": "Aqua"},
        {"name": "Dimethicone"},
        {"name": "Glycerin"},
    ]
    apply_comedogenic_flags_strict(ingredients)
    assert ingredients[0]["is_hard"] is False and ingredients[0]["is_conditional"] is False
    assert ingredients[1]["is_conditional"] is True
    assert ingredients[1]["_early_conditional"] is True  # позиция 2 ≤ 5


def test_apply_flags_late_conditional_not_early():
    ingredients = [{"name": f"Filler {i}"} for i in range(6)] + [{"name": "Squalane"}]
    apply_comedogenic_flags_strict(ingredients)
    last = ingredients[-1]
    assert last["is_conditional"] is True
    assert last["_early_conditional"] is False  # позиция 7 > 5
