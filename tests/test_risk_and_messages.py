"""Уровень риска и сборка сообщений — поведение должно остаться прежним."""

from bot.bot import (
    build_composition_message,
    build_step1_brief_message,
    calc_risk_level_strict,
    normalize_product_key,
    step1_cache_key_images,
    step1_cache_key_text,
    step2_cache_key,
)


def ing(name, hard=False, cond=False):
    return {"name": name, "is_hard": hard, "is_conditional": cond}


# ── Уровень риска ──────────────────────────────────────────

def test_no_comedogens_is_none():
    assert calc_risk_level_strict([ing("Aqua"), ing("Glycerin")]) == "none"


def test_any_hard_is_high():
    assert calc_risk_level_strict([ing("Aqua"), ing("Beeswax", hard=True)]) == "high"


def test_two_early_conditionals_is_high():
    items = [ing("Aqua"), ing("Dimethicone", cond=True), ing("Squalane", cond=True)]
    assert calc_risk_level_strict(items) == "high"


def test_one_early_conditional_is_medium():
    items = [ing("Aqua"), ing("Dimethicone", cond=True), ing("Glycerin")]
    assert calc_risk_level_strict(items) == "medium"


def test_late_conditional_only_is_low():
    items = [ing(f"F{i}") for i in range(6)] + [ing("Squalane", cond=True)]
    assert calc_risk_level_strict(items) == "low"


def test_empty_list_is_none():
    assert calc_risk_level_strict([]) == "none"


# ── Сообщения ──────────────────────────────────────────────

def test_no_inci_message_mentions_missing_composition():
    text = build_step1_brief_message({"error": "no_inci", "product_name": "Крем X"})
    assert "Состав не найден" in text
    assert "Крем X" in text
    assert "@DrDubinsky" in text  # футер с врачом на месте


def test_brief_message_shows_risk_label():
    text = build_step1_brief_message({"product_name": "Крем", "risk_level": "high"})
    assert "Высокий риск комедогенности" in text
    assert "🧴 <b>Крем</b>" in text


def test_composition_hides_link_when_no_source():
    data = {"product_name": "Крем", "ingredients": [ing("Aqua")], "source_url": None}
    assert "Источник состава" not in build_composition_message(data)


def test_composition_shows_link_when_source_present():
    data = {
        "product_name": "Крем",
        "ingredients": [ing("Aqua")],
        "source_url": "https://incidecoder.com/products/x",
    }
    out = build_composition_message(data)
    assert "Источник состава" in out
    assert 'href="https://incidecoder.com/products/x"' in out


def test_composition_marks_comedogens():
    data = {
        "product_name": "Крем",
        "ingredients": [ing("Aqua"), ing("Beeswax", hard=True), ing("Squalane", cond=True)],
    }
    out = build_composition_message(data)
    assert "🔴 2. Beeswax" in out
    assert "🟠 3. Squalane" in out
    assert "⚪️ 1. Aqua" in out


# ── Ключи кэша ─────────────────────────────────────────────

def test_product_key_normalization():
    assert normalize_product_key("  CeraVe  Moisturising  Cream! ") == "cerave moisturising cream"
    assert normalize_product_key("CERAVE cream") == normalize_product_key("cerave   Cream")


def test_text_keys_match_for_same_product():
    assert step1_cache_key_text("La Roche-Posay Effaclar") == step1_cache_key_text("la roche-posay  effaclar")


def test_text_keys_differ_for_different_products():
    assert step1_cache_key_text("Cerave cream") != step1_cache_key_text("Cerave lotion")


def test_image_key_depends_on_bytes_and_caption():
    a = step1_cache_key_images([b"one"])
    b = step1_cache_key_images([b"two"])
    c = step1_cache_key_images([b"one"], "Cerave")
    assert a != b
    assert a != c
    assert a == step1_cache_key_images([b"one"])


def test_step2_key_is_stable_for_same_composition():
    data = {
        "product_name": "Крем",
        "risk_level": "high",
        "ingredients": [ing("Aqua"), ing("Beeswax", hard=True)],
    }
    assert step2_cache_key(data) == step2_cache_key(dict(data))


def test_step2_key_changes_with_composition():
    base = {"product_name": "Крем", "risk_level": "high", "ingredients": [ing("Aqua")]}
    other = {"product_name": "Крем", "risk_level": "high",
             "ingredients": [ing("Aqua"), ing("Beeswax", hard=True)]}
    assert step2_cache_key(base) != step2_cache_key(other)
