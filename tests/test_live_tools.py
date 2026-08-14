"""Проверки, которыми живые прогоны меряют качество.

Инструменты в `tools/` вызываются руками и на живой модели, поэтому их
собственные ошибки легко принять за брак бота. Так и вышло: рекомендацию
с прямо названным «Zinc PCA» проверка объявила общим местом — у этого
названия нет ни одного куска длиннее четырёх букв.
"""

from tools.live_check import mentions_composition

NAMES = [
    "Aqua",
    "Niacinamide",
    "Pentylene Glycol",
    "Zinc PCA",
    "Tamarindus Indica Seed Gum",
]


def test_long_name_is_recognized():
    assert mentions_composition("Niacinamide стоит на второй позиции.", NAMES)


def test_short_name_named_in_full_is_recognized():
    """«Zinc PCA» — ни одного куска длиннее четырёх букв, но компонент назван."""
    assert mentions_composition(
        "Zinc PCA 1.0% рядом с ниацинамидом может подсушивать.", NAMES
    )


def test_position_counts_as_a_tie_to_the_composition():
    assert mentions_composition("Компонент на третьей позиции работает иначе.", NAMES)


def test_template_advice_is_still_caught():
    assert not mentions_composition(
        "Реакция индивидуальна: наносите тонким слоем и следите за кожей.", NAMES
    )


def test_empty_names_do_not_match_everything():
    assert not mentions_composition("Любой текст без привязки.", ["", "  "])
