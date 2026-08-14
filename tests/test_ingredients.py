"""Разбор состава по группам и переход к врачу.

Проверяем три обещания новой навигации:
  • пояснение получает КАЖДЫЙ компонент, даже если модель его пропустила;
  • обрезанный по лимиту токенов ответ разбирается частично, а не теряется;
  • кнопка к врачу появляется только тогда, когда вопросы действительно есть.
"""

import asyncio
import json
import re

import pytest

from agent.agent import (
    _assemble_groups,
    _parse_ingredients_marked_text,
    _parse_step2_marked_text_v2,
    build_ingredients_payload,
)
from agent.agent import AgentResult, Usage
from agent.groups import FALLBACK_GROUP, GROUP_ORDER, group_label, normalize_group
from bot import analytics, bot as botmod

STEP1 = {
    "product_name": "CeraVe Moisturising Cream",
    "risk_level": "medium",
    "source_url": None,
    "ingredients": [
        {"name": "Aqua", "is_hard": False, "is_conditional": False},
        {"name": "Glycerin", "is_hard": False, "is_conditional": False},
        {"name": "Petrolatum", "is_hard": True, "is_conditional": False},
        {"name": "Dimethicone", "is_hard": False, "is_conditional": True},
    ],
}

INGREDIENTS_DATA = {
    "groups": [
        {
            "key": "emol",
            "items": [
                {"name": "Petrolatum", "position": 3, "type": "hard",
                 "note": "Минеральный окклюзив, держит влагу плёнкой."},
            ],
        },
        {
            "key": "sili",
            "items": [
                {"name": "Dimethicone", "position": 4, "type": "conditional",
                 "note": "Силикон, даёт скольжение."},
            ],
        },
    ]
}

STEP2_WITH_QUESTIONS = {
    "summary": "Состав плотный.",
    "comedogens_notes": [],
    "overall_notes": "",
    "recommendations": ["Вазелин на третьей позиции — летом под ним душно."],
    "doctor_questions": [
        "У меня розацеа — насколько для меня критичен вазелин в такой позиции?",
        "Можно ли сочетать это с моим ретинолом?",
        "Стоит ли пользоваться этим круглый год или только зимой?",
    ],
}


class FakeUser:
    id = 777
    username = "tester"
    full_name = "Tester"


class FakeChat:
    id = 555


class Sent:
    def __init__(self, text, reply_markup=None):
        self.text = text
        self.reply_markup = reply_markup


class FakeMessage:
    def __init__(self):
        self.from_user = FakeUser()
        self.chat = FakeChat()
        self.sent = []

    async def answer(self, text, reply_markup=None, **kwargs):
        s = Sent(text, reply_markup)
        self.sent.append(s)
        return s


class FakeCallback:
    def __init__(self, data):
        self.data = data
        self.message = FakeMessage()
        self.from_user = FakeUser()
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


@pytest.fixture(autouse=True)
def clean():
    analytics.init()
    analytics._exec("DELETE FROM events")
    analytics._exec("DELETE FROM users")
    analytics._exec("DELETE FROM cache")
    botmod.STEP2_CACHE.clear()
    botmod.STEP2_INFLIGHT.clear()
    botmod.INGREDIENTS_INFLIGHT.clear()
    yield


def run(coro):
    return asyncio.run(coro)


def buttons(markup):
    return [b.text for row in markup.inline_keyboard for b in row]


# ─────────────────────────────────────────────────────────────
# Разбор ответа модели
# ─────────────────────────────────────────────────────────────

def test_parses_marked_lines():
    parsed = _parse_ingredients_marked_text(
        "- 1 | grp=tex | src=known | note=Вода — основа эмульсии.\n"
        "- 2 | grp=humec | src=web | note=Глицерин удерживает влагу. "
        "| ask=Сколько его нужно твоей коже — зависит от сухости."
    )
    assert parsed[1] == {
        "grp": "tex", "src": "known", "note": "Вода — основа эмульсии.", "ask": "",
    }
    assert parsed[2]["grp"] == "humec"
    assert parsed[2]["ask"].startswith("Сколько его нужно")


def test_parses_confidence_and_question():
    parsed = _parse_ingredients_marked_text(
        "- 3 | grp=fragr | src=thin | note=Экстракт, данных мало. "
        "| ask=Если бывали реакции на растительное — это твой случай."
    )
    assert parsed[3]["src"] == "thin"
    assert "реакции" in parsed[3]["ask"]


def test_unknown_confidence_is_dropped():
    """Чужое значение src не должно попадать дальше как признак достоверности."""
    parsed = _parse_ingredients_marked_text("- 1 | grp=tex | src=абсолютно | note=Вода.")
    assert parsed[1]["src"] == ""


def test_pipe_inside_note_does_not_eat_the_tail():
    """Вертикальная черта внутри текста не должна обрезать пояснение."""
    parsed = _parse_ingredients_marked_text(
        "- 1 | grp=tex | src=known | note=Вода | основа всего. | ask=Вопрос?"
    )
    assert parsed[1]["note"] == "Вода | основа всего."
    assert parsed[1]["ask"] == "Вопрос?"


def test_unknown_group_falls_back():
    parsed = _parse_ingredients_marked_text("- 1 | grp=магия | note=Что-то.")
    assert parsed[1]["grp"] == FALLBACK_GROUP


def test_truncated_answer_keeps_parsed_lines():
    """Ответ оборвался на полуслове — уцелевшие строки должны уцелеть."""
    parsed = _parse_ingredients_marked_text(
        "- 1 | grp=tex | note=Вода.\n"
        "- 2 | grp=humec | note=Глицерин.\n"
        "- 3 | grp=em"
    )
    assert set(parsed) == {1, 2, 3}
    assert parsed[3]["note"] == ""


def test_garbage_lines_are_ignored():
    parsed = _parse_ingredients_marked_text("Вот разбор состава:\n\n- 1 | grp=tex | note=Вода.")
    assert list(parsed) == [1]


# ─────────────────────────────────────────────────────────────
# Сборка групп
# ─────────────────────────────────────────────────────────────

def test_payload_marks_comedogens():
    payload = build_ingredients_payload(STEP1)
    marks = {it["name"]: it.get("mark") for it in payload["items"]}
    assert marks["Petrolatum"] == "hard"
    assert marks["Dimethicone"] == "conditional"
    assert marks["Aqua"] is None
    assert [it["n"] for it in payload["items"]] == [1, 2, 3, 4]


def test_skipped_ingredient_is_not_lost():
    """Модель пропустила компонент — он всё равно виден, просто без пояснения."""
    payload = build_ingredients_payload(STEP1)
    parsed = _parse_ingredients_marked_text("- 1 | grp=tex | note=Вода.")

    assembled = _assemble_groups(payload, parsed)
    names = [it["name"] for g in assembled["groups"] for it in g["items"]]

    assert set(names) == {"Aqua", "Glycerin", "Petrolatum", "Dimethicone"}
    other = next(g for g in assembled["groups"] if g["key"] == FALLBACK_GROUP)
    assert {it["name"] for it in other["items"]} == {"Glycerin", "Petrolatum", "Dimethicone"}
    assert all("note" not in it for it in other["items"])


def test_groups_follow_canonical_order():
    payload = build_ingredients_payload(STEP1)
    parsed = {
        1: {"grp": "presv", "note": "n"},
        2: {"grp": "emol", "note": "n"},
        3: {"grp": "emol", "note": "n"},
        4: {"grp": "sili", "note": "n"},
    }
    keys = [g["key"] for g in _assemble_groups(payload, parsed)["groups"]]
    assert keys == sorted(keys, key=GROUP_ORDER.index)


def test_comedogen_type_comes_from_code_not_model():
    """Тип берётся из данных шага 1 — модель на него влиять не должна."""
    payload = build_ingredients_payload(STEP1)
    assembled = _assemble_groups(payload, {3: {"grp": "humec", "note": "n"}})
    petrolatum = next(
        it for g in assembled["groups"] for it in g["items"] if it["name"] == "Petrolatum"
    )
    assert petrolatum["type"] == "hard"


def test_model_cannot_invent_ingredients_or_groups():
    """Даже если модель «поедет», состав и группы остаются под контролем кода.

    Имена берутся из данных шага 1, а не из ответа модели: модель отвечает
    только номерами. Поэтому выдуманный ингредиент физически некуда положить,
    а незнакомый ключ группы схлопывается в «Остальное».
    """
    step1 = {"product_name": "X", "ingredients": [
        {"name": "Aqua", "is_hard": False, "is_conditional": False},
        {"name": "Glycerin", "is_hard": False, "is_conditional": False},
    ]}
    rogue = (
        "- 1 | grp=tex | note=Вода.\n"
        "- 2 | grp=humec | note=Глицерин.\n"
        "- 3 | grp=emol | note=ВЫДУМАННОЕ МАСЛО, КОТОРОГО НЕТ В СОСТАВЕ\n"
        "- 99 | grp=магия | note=И ещё одно\n"
    )
    result = _assemble_groups(
        build_ingredients_payload(step1), _parse_ingredients_marked_text(rogue)
    )

    names = [it["name"] for g in result["groups"] for it in g["items"]]
    assert sorted(names) == ["Aqua", "Glycerin"]
    assert all(g["key"] in GROUP_ORDER for g in result["groups"])
    assert "ВЫДУМАННОЕ" not in json.dumps(result, ensure_ascii=False)


def test_empty_composition_yields_no_groups():
    payload = build_ingredients_payload({"product_name": "X", "ingredients": []})
    assert _assemble_groups(payload, {})["groups"] == []


def test_normalize_group_handles_junk():
    assert normalize_group(None) == FALLBACK_GROUP
    assert normalize_group("  EMOL ") == "emol"


# ─────────────────────────────────────────────────────────────
# Тексты сообщений
# ─────────────────────────────────────────────────────────────

def test_groups_message_counts_marked_components():
    text = botmod.build_groups_message(STEP1, INGREDIENTS_DATA)
    assert "CeraVe Moisturising Cream" in text
    assert "2 компонента" in text
    assert "🔴 1" in text and "🟠 1" in text


def test_group_card_shows_marks_positions_and_notes():
    group = INGREDIENTS_DATA["groups"][0]
    text = botmod.build_group_card_message(STEP1, group)
    assert group_label("emol") in text
    assert "🔴 <b>Petrolatum</b> · №3" in text
    assert "Минеральный окклюзив" in text


GROUP_WITH_QUESTIONS = {
    "key": "activ",
    "items": [
        {"name": "Retinol", "position": 6, "type": "conditional",
         "note": "Производное витамина A, ускоряет обновление кожи.",
         "ask": "Совместим ли он с твоим текущим уходом — зависит от состояния барьера."},
        {"name": "Centella Extract", "position": 22,
         "note": "Растительный экстракт, данные о действии расходятся.",
         "thin": True,
         "ask": "Если бывали реакции на растительные компоненты — это твой случай."},
        {"name": "Niacinamide", "position": 8, "note": "Витамин B3."},
    ],
}


def test_thin_data_is_admitted_not_invented():
    """Признание «данных мало» должно доходить до человека, а не прятаться."""
    text = botmod.build_group_card_message(STEP1, GROUP_WITH_QUESTIONS)
    assert "Надёжных данных по нему немного" in text
    # ...и ровно один раз — только у того компонента, что помечен
    assert text.count("Надёжных данных") == 1


def test_open_questions_are_rendered_per_component():
    text = botmod.build_group_card_message(STEP1, GROUP_WITH_QUESTIONS)
    assert "🩺 <i>Совместим ли он с твоим текущим уходом" in text
    assert "зависит от твоей кожи" in text  # пояснение к пометкам внизу
    # У ниацинамида вопроса нет — выдуманный не подставляется
    assert text.count("🩺 <i>") == 2


def test_group_questions_collects_only_real_ones():
    got = botmod.group_questions(GROUP_WITH_QUESTIONS)
    assert [q["name"] for q in got] == ["Retinol", "Centella Extract"]
    assert botmod.group_questions(INGREDIENTS_DATA["groups"][0]) == []


def test_card_without_questions_still_says_it_is_not_a_consultation():
    text = botmod.build_group_card_message(STEP1, INGREDIENTS_DATA["groups"][0])
    assert "не консультация" in text


def test_group_questions_screen_leads_to_the_doctor():
    text = botmod.build_group_questions_message(STEP1, GROUP_WITH_QUESTIONS)
    assert "Retinol" in text and "Centella Extract" in text
    assert "Niacinamide" not in text
    assert "Лиза Дубинская" in text
    assert_telegram_html_safe(text)


def test_group_card_button_counts_its_questions():
    labels = buttons(botmod._build_group_card_keyboard(
        "tok", GROUP_WITH_QUESTIONS, has_questions=True))
    assert "🩺 2 вопроса по этой группе" in labels
    # У группы без вопросов такой кнопки нет
    plain_labels = buttons(botmod._build_group_card_keyboard(
        "tok", INGREDIENTS_DATA["groups"][0], has_questions=True))
    assert not any("по этой группе" in x for x in plain_labels)


def test_group_questions_callback(monkeypatch):
    data = {"groups": [GROUP_WITH_QUESTIONS]}

    async def _run(step1_payload):
        return AgentResult(raw=json.dumps(data), usage=Usage())

    monkeypatch.setattr(botmod, "run_agent_ingredients_ex", _run)
    token = botmod._cache_put(STEP1)
    run(botmod.handle_groups_callback(FakeCallback(f"groups:{token}")))

    cb = FakeCallback(f"gq:{token}:activ")
    run(botmod.handle_group_questions_callback(cb))
    assert "Что спросить у врача" in cb.message.sent[-1].text
    assert any(b.url for row in cb.message.sent[-1].reply_markup.inline_keyboard for b in row)


def test_group_questions_callback_refuses_empty_group(monkeypatch):
    async def _run(step1_payload):
        return AgentResult(raw=json.dumps(INGREDIENTS_DATA), usage=Usage())

    monkeypatch.setattr(botmod, "run_agent_ingredients_ex", _run)
    token = botmod._cache_put(STEP1)
    run(botmod.handle_groups_callback(FakeCallback(f"groups:{token}")))

    cb = FakeCallback(f"gq:{token}:emol")
    run(botmod.handle_group_questions_callback(cb))
    assert cb.answers[-1][1] is True
    assert cb.message.sent == []


def test_ask_and_thin_survive_assembly():
    payload = build_ingredients_payload(STEP1)
    parsed = _parse_ingredients_marked_text(
        "- 3 | grp=emol | src=thin | note=Мало данных. | ask=Твой ли это случай?"
    )
    item = next(
        it for g in _assemble_groups(payload, parsed)["groups"]
        for it in g["items"] if it["name"] == "Petrolatum"
    )
    assert item["thin"] is True
    assert item["ask"] == "Твой ли это случай?"


def test_confident_components_carry_no_thin_flag():
    payload = build_ingredients_payload(STEP1)
    parsed = _parse_ingredients_marked_text("- 1 | grp=tex | src=known | note=Вода.")
    item = next(
        it for g in _assemble_groups(payload, parsed)["groups"]
        for it in g["items"] if it["name"] == "Aqua"
    )
    assert "thin" not in item


def test_group_card_survives_missing_note():
    group = {"key": "tex", "items": [{"name": "Aqua", "position": 1}]}
    text = botmod.build_group_card_message(STEP1, group)
    assert "Aqua" in text


def test_doctor_message_leads_to_the_doctor():
    text = botmod.build_doctor_questions_message(
        STEP1, STEP2_WITH_QUESTIONS["doctor_questions"]
    )
    assert "розацеа" in text
    assert "1️⃣" in text and "3️⃣" in text
    assert "Лиза Дубинская" in text
    # Обещание «это не консультация» должно остаться на экране.
    assert "именно твоей коже" in text


def test_step2_hooks_to_doctor_when_questions_exist():
    with_q = botmod.build_step2_message(STEP2_WITH_QUESTIONS, product_name="X")
    assert "3 вопроса" in with_q

    without_q = botmod.build_step2_message(
        {**STEP2_WITH_QUESTIONS, "doctor_questions": []}, product_name="X"
    )
    assert "3 вопроса" not in without_q
    assert "@DrDubinsky" in without_q  # старый футер остаётся запасным вариантом


@pytest.mark.parametrize(
    "n,expected",
    [(1, "1 компонент"), (2, "2 компонента"), (5, "5 компонентов"),
     (11, "11 компонентов"), (21, "21 компонент"), (34, "34 компонента")],
)
def test_plural_components(n, expected):
    assert botmod._plural_components(n) == expected


@pytest.mark.parametrize(
    "n,expected", [(1, "1 вопрос"), (2, "2 вопроса"), (3, "3 вопроса"), (5, "5 вопросов")]
)
def test_plural_questions(n, expected):
    assert botmod._plural_questions(n) == expected


def plain(text):
    """Текст без HTML — как его прочитает человек в Telegram."""
    return re.sub(r"</?[^>]+>", "", text)


def test_step2_hook_reads_correctly_with_one_question():
    """Согласование не должно ломаться на единственном вопросе."""
    text = plain(botmod.build_step2_message(
        {**STEP2_WITH_QUESTIONS, "doctor_questions": ["Единственный вопрос?"]},
        product_name="X",
    ))
    assert "— 1 вопрос." in text
    assert "1 вопроса" not in text


def test_step2_hook_reads_correctly_with_three_questions():
    text = plain(botmod.build_step2_message(STEP2_WITH_QUESTIONS, product_name="X"))
    assert "— 3 вопроса." in text


def test_long_message_is_split_on_paragraphs():
    text = "\n\n".join(["абзац " * 50] * 20)
    parts = botmod._split_message(text, limit=1000)
    assert len(parts) > 1
    assert all(len(p) <= 1000 for p in parts)
    assert "".join(p.replace("\n", "") for p in parts) == text.replace("\n", "")


def test_oversized_paragraph_is_hard_split():
    parts = botmod._split_message("x" * 2500, limit=1000)
    assert all(len(p) <= 1000 for p in parts)
    assert "".join(parts) == "x" * 2500


# ─────────────────────────────────────────────────────────────
# Шаг 2: вопросы врачу в ответе модели
# ─────────────────────────────────────────────────────────────

def test_step2_parses_doctor_block():
    parsed = _parse_step2_marked_text_v2(
        "SUMMARY:\nКоротко.\n\n"
        "COMEDOGENS:\n- Petrolatum | pos=3 | type=hard | note=Окклюзив.\n\n"
        "OVERALL:\nВывод.\n\n"
        "RECOMMENDATIONS:\n- Совет про вазелин.\n\n"
        "DOCTOR:\n- Первый вопрос?\n- Второй вопрос?\n"
    )
    assert parsed["doctor_questions"] == ["Первый вопрос?", "Второй вопрос?"]
    assert parsed["recommendations"] == ["Совет про вазелин."]
    assert parsed["comedogens_notes"][0]["name"] == "Petrolatum"


def test_step2_without_doctor_block_is_still_valid():
    parsed = _parse_step2_marked_text_v2("SUMMARY:\nКоротко.\n\nRECOMMENDATIONS:\n- Совет.\n")
    assert parsed["doctor_questions"] == []
    assert parsed["summary"] == "Коротко."


# ─────────────────────────────────────────────────────────────
# Кнопки
# ─────────────────────────────────────────────────────────────

def test_step2_keyboard_hides_doctor_without_questions():
    assert botmod.BTN_DOCTOR in buttons(botmod._build_step2_keyboard("t", has_questions=True))
    assert botmod.BTN_DOCTOR not in buttons(botmod._build_step2_keyboard("t", has_questions=False))


def test_every_screen_offers_the_doctor(monkeypatch):
    """К врачу должно вести с любого экрана, даже когда вопросов не набралось."""
    monkeypatch.setattr(botmod.config, "INGREDIENTS_ENABLED", False)

    bare = botmod._build_step2_keyboard("t", has_questions=False)
    assert botmod.BTN_DOCTOR_DIRECT in buttons(bare)

    groups = botmod._build_groups_keyboard("t", INGREDIENTS_DATA["groups"], has_questions=False)
    assert botmod.BTN_DOCTOR_DIRECT in buttons(groups)

    card = botmod._build_group_card_keyboard(
        "t", INGREDIENTS_DATA["groups"][0], has_questions=False
    )
    assert botmod.BTN_DOCTOR_DIRECT in buttons(card)

    assert botmod.BTN_DOCTOR_DIRECT in buttons(botmod._build_step1_keyboard("t"))


# ─────────────────────────────────────────────────────────────
# HTML-экранирование
# ─────────────────────────────────────────────────────────────

HTML_TAGS = re.compile(r"</?(b|i|u|s|a|code|pre|tg-spoiler|blockquote)[ >]")
BARE_AMP = re.compile(r"&(?!amp;|lt;|gt;|quot;|#)")


def assert_telegram_html_safe(text):
    """Ни одного чужого тега и ни одного голого амперсанда."""
    stray = [m.group(0) for m in re.finditer(r"<[^>]*>", text) if not HTML_TAGS.match(m.group(0))]
    assert stray == [], f"Telegram не разберёт: {stray}"
    assert not BARE_AMP.search(text), "неэкранированный амперсанд"


def test_group_card_escapes_inci_blends_and_comparisons():
    """«Tocopherol & Ascorbyl Palmitate» и «<1%» — реальные строки из INCI и пояснений."""
    group = {
        "key": "activ",
        "items": [{
            "name": "Tocopherol & Ascorbyl Palmitate",
            "position": 9,
            "type": "conditional",
            "note": "Антиоксидантная связка. Доля <1% и она не определяет характер формулы.",
        }],
    }
    text = botmod.build_group_card_message({"product_name": "Крем & Ко <Люкс>"}, group)
    assert_telegram_html_safe(text)
    assert "Tocopherol &amp; Ascorbyl Palmitate" in text


def test_doctor_and_step2_messages_escape_model_text():
    questions = ["Мне подойдёт средство с долей масел <5%?", "А если у меня AHA & BHA в уходе?"]
    assert_telegram_html_safe(botmod.build_doctor_questions_message({"product_name": "A & B"}, questions))

    step2 = {
        "summary": "Формула плотная: масел >30%.",
        "comedogens_notes": [{"name": "Butyrospermum Parkii & Wax", "position": 2,
                              "type": "hard", "note": "Доля <10%, но позиция высокая."}],
        "overall_notes": "",
        "recommendations": ["Связка воск & масло ши плотная — летом это <многовато>."],
        "doctor_questions": questions,
    }
    assert_telegram_html_safe(botmod.build_step2_message(step2, product_name="Q & A"))


def test_composition_and_brief_escape_ingredient_names():
    data = {
        "product_name": "Крем & Ко",
        "risk_level": "none",
        "ingredients": [{"name": "Tocopherol & Ascorbyl Palmitate", "is_hard": False,
                         "is_conditional": False}],
    }
    assert_telegram_html_safe(botmod.build_composition_message(data))
    assert_telegram_html_safe(botmod.build_step1_brief_message(data))


def test_groups_message_escapes_product_name():
    assert_telegram_html_safe(botmod.build_groups_message({"product_name": "A & B <C>",
                                                           "ingredients": []}, INGREDIENTS_DATA))


def test_groups_keyboard_shows_counts_and_skips_empty():
    data = {"groups": INGREDIENTS_DATA["groups"] + [{"key": "surf", "items": []}]}
    labels = buttons(botmod._build_groups_keyboard("t", data["groups"], has_questions=False))
    assert f"{group_label('emol')} · 1" in labels
    assert not any("Очищающие" in x for x in labels)


def test_group_callback_data_fits_telegram_limit():
    token = botmod._cache_put(STEP1)
    markup = botmod._build_groups_keyboard("t" and token, INGREDIENTS_DATA["groups"], has_questions=True)
    for row in markup.inline_keyboard:
        for b in row:
            if b.callback_data:
                assert len(b.callback_data.encode()) <= 64


def test_doctor_keyboard_links_to_telegram():
    markup = botmod._build_doctor_keyboard("tok")
    url_buttons = [b for row in markup.inline_keyboard for b in row if b.url]
    assert url_buttons and url_buttons[0].url.endswith("/DrDubinsky")


# ─────────────────────────────────────────────────────────────
# Обработчики
# ─────────────────────────────────────────────────────────────

def _patch_agent(monkeypatch, calls):
    async def _run(step1_payload):
        calls.append(step1_payload)
        return AgentResult(raw=json.dumps(INGREDIENTS_DATA), usage=Usage())

    monkeypatch.setattr(botmod, "run_agent_ingredients_ex", _run)


def test_groups_callback_renders_and_caches(monkeypatch):
    calls = []
    _patch_agent(monkeypatch, calls)

    token = botmod._cache_put(STEP1)
    cb = FakeCallback(f"groups:{token}")
    run(botmod.handle_groups_callback(cb))

    assert len(calls) == 1
    assert botmod.PROCESSING_INGREDIENTS in cb.message.sent[0].text
    assert "Что делает каждый компонент" in cb.message.sent[-1].text
    assert f"{group_label('sili')} · 1" in buttons(cb.message.sent[-1].reply_markup)

    # Второй заход — из памяти, без плашки ожидания и без вызова модели.
    cb2 = FakeCallback(f"groups:{token}")
    run(botmod.handle_groups_callback(cb2))
    assert len(calls) == 1
    assert botmod.PROCESSING_INGREDIENTS not in cb2.message.sent[0].text


def test_groups_callback_uses_sqlite_cache_for_new_token(monkeypatch):
    """Тот же состав в новом разборе не должен снова стоить вызова модели."""
    calls = []
    _patch_agent(monkeypatch, calls)

    run(botmod.handle_groups_callback(FakeCallback(f"groups:{botmod._cache_put(STEP1)}")))
    run(botmod.handle_groups_callback(FakeCallback(f"groups:{botmod._cache_put(STEP1)}")))

    assert len(calls) == 1


def test_group_card_callback_renders_requested_group(monkeypatch):
    _patch_agent(monkeypatch, [])
    token = botmod._cache_put(STEP1)
    run(botmod.handle_groups_callback(FakeCallback(f"groups:{token}")))

    cb = FakeCallback(f"grp:{token}:sili")
    run(botmod.handle_group_card_callback(cb))
    assert "Dimethicone" in cb.message.sent[-1].text
    assert "← Все группы" in buttons(cb.message.sent[-1].reply_markup)


def test_group_card_rejects_unknown_group(monkeypatch):
    _patch_agent(monkeypatch, [])
    token = botmod._cache_put(STEP1)
    run(botmod.handle_groups_callback(FakeCallback(f"groups:{token}")))

    cb = FakeCallback(f"grp:{token}:presv")
    run(botmod.handle_group_card_callback(cb))
    assert cb.answers[-1][1] is True  # show_alert
    assert cb.message.sent == []


def test_doctor_callback_needs_step2_questions():
    token = botmod._cache_put(STEP1)

    cb = FakeCallback(f"doc:{token}")
    run(botmod.handle_doctor_callback(cb))
    assert cb.message.sent == []  # шага 2 ещё не было — вопросов нет

    botmod._slot_put(token, "step2", STEP2_WITH_QUESTIONS)
    cb2 = FakeCallback(f"doc:{token}")
    run(botmod.handle_doctor_callback(cb2))
    assert "Что спросить у врача" in cb2.message.sent[-1].text
    assert any(b.url for row in cb2.message.sent[-1].reply_markup.inline_keyboard for b in row)


def test_stale_token_is_reported_not_crashed():
    for cb in (FakeCallback("groups:nope"), FakeCallback("grp:nope:emol"), FakeCallback("doc:nope")):
        run(
            botmod.handle_groups_callback(cb)
            if cb.data.startswith("groups:")
            else botmod.handle_group_card_callback(cb)
            if cb.data.startswith("grp:")
            else botmod.handle_doctor_callback(cb)
        )
        assert cb.answers[-1][1] is True
        assert cb.message.sent == []


def test_malformed_group_callback_is_rejected():
    cb = FakeCallback("grp:onlytoken")
    run(botmod.handle_group_card_callback(cb))
    assert cb.answers[-1][1] is True


def test_agent_failure_tells_the_user(monkeypatch):
    async def _boom(step1_payload):
        raise RuntimeError("модель недоступна")

    monkeypatch.setattr(botmod, "run_agent_ingredients_ex", _boom)
    token = botmod._cache_put(STEP1)
    cb = FakeCallback(f"groups:{token}")
    run(botmod.handle_groups_callback(cb))

    assert botmod.ERROR_INGREDIENTS in cb.message.sent[-1].text
    assert token not in botmod.INGREDIENTS_INFLIGHT  # замок снят, можно повторить
