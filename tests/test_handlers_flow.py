"""Сквозной путь по обработчикам: каждый экран обязан ответить.

Тесты в `tests/` проверяли функции разбора, но не телеграмные обработчики.
Из-за этого прод молчал на каждом фото: обработчик падал на первой строке,
разбор при этом работал безупречно, и ни один тест не покраснел.

Здесь путь проходится целиком — текст, фото и все шесть кнопок, — а объекты
Telegram берутся из `tools/tg_fakes.py`, где они повторяют aiogram 3:
`answer()` и `delete()` возвращают awaitable-объект, а не корутину.

Правило, по которому написан каждый тест: тишина — это брак. Человек нажал
кнопку, значит он обязан получить сообщение либо всплывашку с причиной.
Обработчик, который «просто ничего не сделал», — тот же баг, что был в проде.
"""

import asyncio
import json

import pytest

from agent.agent import AgentResult, Usage
from bot import analytics, bot as botmod
from tools.tg_fakes import (
    FakeBot,
    FakeCallbackQuery,
    FakeMessage,
    buttons,
    callback_data,
    texts,
)

STEP1_JSON = {
    "product_name": "CeraVe Moisturising Cream",
    "risk_level": "high",
    "source_url": None,
    "ingredients": [
        {"name": "Aqua", "is_hard": False, "is_conditional": False},
        {"name": "Glycerin", "is_hard": False, "is_conditional": False},
        {"name": "Petrolatum", "is_hard": True, "is_conditional": False},
        {"name": "Dimethicone", "is_hard": False, "is_conditional": True},
    ],
}

STEP2_JSON = {
    "summary": "Состав плотный, окклюзив на третьей позиции.",
    "comedogens_notes": [{"name": "Petrolatum", "note": "Запечатывает влагу плёнкой."}],
    "recommendations": ["Petrolatum на третьей позиции — следи за Т-зоной."],
    "overall_notes": "",
    "doctor_questions": [
        "Подойдёт ли мне такая окклюзия круглый год?",
        "Совместим ли состав с моим ретиноидом?",
        "Как часто наносить при моём типе кожи?",
    ],
}

INGREDIENTS_JSON = {
    "groups": [
        {
            "key": "emol",
            "items": [
                {
                    "name": "Petrolatum",
                    "position": 3,
                    "type": "hard",
                    "note": "Минеральный окклюзив, держит влагу плёнкой.",
                    "ask": "Насколько критична для тебя такая плёнка — зависит от склонности к комедонам.",
                }
            ],
        },
        {
            "key": "sili",
            "items": [
                {
                    "name": "Dimethicone",
                    "position": 4,
                    "type": "conditional",
                    "note": "Силикон, даёт скольжение.",
                }
            ],
        },
        {
            "key": "tex",
            "items": [{"name": "Aqua", "position": 1, "note": "Основа эмульсии."}],
        },
    ]
}


@pytest.fixture(autouse=True)
def clean():
    analytics.init()
    analytics._exec("DELETE FROM events")
    analytics._exec("DELETE FROM cache")
    botmod.STEP2_CACHE.clear()
    botmod.STEP2_INFLIGHT.clear()
    botmod.INGREDIENTS_INFLIGHT.clear()
    botmod._ACTIVE_USERS.clear()
    botmod._ALBUM_BUFFER.clear()
    yield
    botmod._ACTIVE_USERS.clear()
    botmod._ALBUM_BUFFER.clear()


@pytest.fixture
def agent_ok(monkeypatch):
    """Модель отвечает нормально — проверяем сам путь, а не её качество."""

    async def _step1(product_name=None, image_bytes=None, images=None):
        return AgentResult(raw=json.dumps(STEP1_JSON), usage=Usage())

    async def _step2(step1_payload):
        return AgentResult(raw=json.dumps(STEP2_JSON), usage=Usage())

    async def _ingredients(step1_payload):
        return AgentResult(raw=json.dumps(INGREDIENTS_JSON), usage=Usage())

    async def _collect(bot, photos):
        return [b"\xff\xd8fake-jpeg"]

    monkeypatch.setattr(botmod, "run_agent_step1_ex", _step1)
    monkeypatch.setattr(botmod, "run_agent_step2_ex", _step2)
    monkeypatch.setattr(botmod, "run_agent_ingredients_ex", _ingredients)
    monkeypatch.setattr(botmod, "_collect_images", _collect)


async def settle():
    """Дожидается фоновых задач, которые породил обработчик."""
    for _ in range(200):
        alive = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
        if not alive:
            return
        await asyncio.wait(alive, timeout=5)


def run(coro):
    async def _wrap():
        result = await coro
        await settle()
        return result

    return asyncio.run(_wrap())


def token_of(markup):
    data = callback_data(markup, "состав")
    return data.split(":", 1)[1]


def analyse(text="CeraVe Moisturising Cream"):
    """Проходит шаг 1 и отдаёт (сообщение, токен разбора)."""
    msg = FakeMessage(text=text)
    run(botmod.handle_text(msg, FakeBot()))
    final = [m for m in msg.sent if m.reply_markup is not None][-1]
    return msg, token_of(final.reply_markup)


# ─────────────────────────────────────────────────────────────
# Вход: текст и фото
# ─────────────────────────────────────────────────────────────

def test_text_gets_verdict_and_buttons(agent_ok):
    msg, token = analyse()
    assert texts(msg)[0] == botmod.PROCESSING_TEXT
    assert any("CeraVe" in t for t in texts(msg))

    labels = [label for label, _ in buttons(msg.sent[-1].reply_markup)]
    assert "🧾 Посмотреть состав" in labels
    assert "📘 Подробнее" in labels
    assert botmod.BTN_DOCTOR_DIRECT in labels
    assert token


def test_text_removes_the_status_plate(agent_ok):
    msg, _ = analyse()
    assert msg.sent[0].deleted is True


def test_photo_gets_verdict_and_buttons(agent_ok):
    msg = FakeMessage(photo=True, caption="CeraVe Moisturising Cream")
    run(botmod.handle_photo(msg, FakeBot()))
    assert texts(msg)[0] == botmod.PROCESSING_PHOTO
    assert any("CeraVe" in t for t in texts(msg))
    assert msg.sent[-1].reply_markup is not None


def test_empty_text_is_answered(agent_ok):
    msg = FakeMessage(text="   ")
    run(botmod.handle_text(msg, FakeBot()))
    assert texts(msg) == [botmod.ERROR_EMPTY]


def test_too_long_text_is_answered(agent_ok):
    msg = FakeMessage(text="я" * (botmod.config.MAX_QUERY_LEN + 1))
    run(botmod.handle_text(msg, FakeBot()))
    assert texts(msg) == [botmod.TOO_LONG_MESSAGE]


def test_unknown_command_is_answered(agent_ok):
    """Неизвестная команда — тоже разговор, а не игнор.

    Раньше handle_text молча выходил на любом тексте с «/», и человек,
    ошибившийся в команде, получал ровно ту же тишину, что и на фото.
    """
    msg = FakeMessage(text="/statss")
    run(botmod.handle_text(msg, FakeBot()))
    assert texts(msg), "на неизвестную команду бот промолчал"


# ─────────────────────────────────────────────────────────────
# Кнопки: каждая обязана нарисовать свой экран
# ─────────────────────────────────────────────────────────────

def test_composition_button_shows_composition(agent_ok):
    _, token = analyse()
    cb = FakeCallbackQuery(f"composition:{token}")
    run(botmod.handle_composition_callback(cb))

    body = "\n".join(texts(cb.message))
    assert "Состав" in body
    assert "Petrolatum" in body and "Dimethicone" in body


def test_step2_button_shows_details(agent_ok):
    _, token = analyse()
    bot = FakeBot()
    cb = FakeCallbackQuery(f"step2:{token}")
    run(botmod.handle_step2_callback(cb, bot))

    assert botmod.PROCESSING_STEP2 in texts(cb.message)
    body = "\n".join(texts(bot))
    assert "Подробнее" in body
    assert "Petrolatum" in body
    assert token not in botmod.STEP2_INFLIGHT


def test_groups_button_shows_groups(agent_ok):
    _, token = analyse()
    cb = FakeCallbackQuery(f"groups:{token}")
    run(botmod.handle_groups_callback(cb))

    assert texts(cb.message), "экран групп промолчал"
    labels = [label for label, data in buttons(cb.message.sent[-1].reply_markup)
              if data and data.startswith("grp:")]
    assert labels, "не появилось ни одной кнопки группы"


def test_group_card_button_shows_the_card(agent_ok):
    _, token = analyse()
    run(botmod.handle_groups_callback(FakeCallbackQuery(f"groups:{token}")))

    cb = FakeCallbackQuery(f"grp:{token}:emol")
    run(botmod.handle_group_card_callback(cb))
    body = "\n".join(texts(cb.message))
    assert "Petrolatum" in body
    assert "окклюзив" in body.lower()


def test_group_questions_button_shows_questions(agent_ok):
    _, token = analyse()
    run(botmod.handle_groups_callback(FakeCallbackQuery(f"groups:{token}")))

    cb = FakeCallbackQuery(f"gq:{token}:emol")
    run(botmod.handle_group_questions_callback(cb))
    assert texts(cb.message), "вопросы по группе не отправились"
    assert "комедон" in "\n".join(texts(cb.message)).lower()


def test_doctor_button_shows_questions(agent_ok):
    _, token = analyse()
    run(botmod.handle_step2_callback(FakeCallbackQuery(f"step2:{token}"), FakeBot()))

    cb = FakeCallbackQuery(f"doc:{token}")
    run(botmod.handle_doctor_callback(cb))
    body = "\n".join(texts(cb.message))
    assert "ретиноид" in body.lower()


def test_whole_chain_answers_on_every_screen(agent_ok):
    """Один проход по всем экранам подряд — как ходит человек."""
    _, token = analyse()
    bot = FakeBot()

    screens = []
    for data, handler, args in (
        (f"composition:{token}", botmod.handle_composition_callback, ()),
        (f"step2:{token}", botmod.handle_step2_callback, (bot,)),
        (f"groups:{token}", botmod.handle_groups_callback, ()),
        (f"grp:{token}:emol", botmod.handle_group_card_callback, ()),
        (f"gq:{token}:emol", botmod.handle_group_questions_callback, ()),
        (f"doc:{token}", botmod.handle_doctor_callback, ()),
    ):
        cb = FakeCallbackQuery(data)
        run(handler(cb, *args))
        got = texts(cb.message) + (texts(bot) if args else [])
        screens.append((data.split(":")[0], got))

    silent = [name for name, got in screens if not got]
    assert not silent, f"эти экраны промолчали: {silent}"


# ─────────────────────────────────────────────────────────────
# Протухшие кнопки: всплывашка вместо тишины
# ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "data, handler",
    [
        ("composition:нет-такого", "handle_composition_callback"),
        ("step2:нет-такого", "handle_step2_callback"),
        ("groups:нет-такого", "handle_groups_callback"),
        ("grp:нет-такого:emol", "handle_group_card_callback"),
        ("gq:нет-такого:emol", "handle_group_questions_callback"),
        ("doc:нет-такого", "handle_doctor_callback"),
    ],
)
def test_stale_button_explains_itself(data, handler):
    cb = FakeCallbackQuery(data)
    fn = getattr(botmod, handler)
    run(fn(cb, FakeBot()) if handler == "handle_step2_callback" else fn(cb))

    assert cb.answers, "кнопка не ответила вообще ничего"
    assert cb.answers[-1]["show_alert"] is True
    assert cb.answers[-1]["text"]


# ─────────────────────────────────────────────────────────────
# Сбои Telegram и модели: замок не должен запирать экран навсегда
# ─────────────────────────────────────────────────────────────

def test_step2_survives_expired_callback_query(agent_ok):
    """«query is too old» гасит часики, но не должен отменять ответ.

    Раньше замок STEP2_INFLIGHT ставился до этого await: исключение уносило
    обработчик, задача не создавалась, и кнопка «Подробнее» навсегда
    отвечала «Уже формирую ответ», не присылая ничего.
    """
    _, token = analyse()
    bot = FakeBot()
    cb = FakeCallbackQuery(f"step2:{token}", answer_error=RuntimeError("query is too old"))
    run(botmod.handle_step2_callback(cb, bot))

    assert texts(bot), "шаг 2 не пришёл из-за устаревшего callback-запроса"
    assert token not in botmod.STEP2_INFLIGHT, "замок остался — кнопка мертва навсегда"


def test_groups_survives_expired_callback_query(agent_ok):
    _, token = analyse()
    cb = FakeCallbackQuery(f"groups:{token}", answer_error=RuntimeError("query is too old"))
    run(botmod.handle_groups_callback(cb))

    assert texts(cb.message), "разбор состава не пришёл"
    assert token not in botmod.INGREDIENTS_INFLIGHT, "замок остался — кнопка мертва навсегда"


def test_groups_lock_is_released_when_the_plate_fails(agent_ok):
    """Плашка «разбираю состав» — украшение; её сбой не отменяет разбор."""
    _, token = analyse()
    cb = FakeCallbackQuery(f"groups:{token}")
    cb.message.answer_error = RuntimeError("Telegram: bad request")
    run(botmod.handle_groups_callback(cb))

    assert token not in botmod.INGREDIENTS_INFLIGHT


def test_second_press_while_busy_gets_an_answer(agent_ok, monkeypatch):
    _, token = analyse()
    botmod.STEP2_INFLIGHT[token] = 9e18
    cb = FakeCallbackQuery(f"step2:{token}")
    run(botmod.handle_step2_callback(cb, FakeBot()))
    assert cb.answers, "занятость — тоже ответ, а не тишина"


def test_broken_model_data_does_not_silence_the_screen(agent_ok, monkeypatch):
    """Модель может прислать не то, что ждёт разметка.

    Такой ответ ломал построение сообщения, исключение уходило в лог,
    а человек оставался с погасшей кнопкой и без единого слова.
    """
    _, token = analyse()

    def _boom(*a, **kw):
        raise TypeError("'str' object has no attribute 'get'")

    monkeypatch.setattr(botmod, "build_composition_message", _boom)
    cb = FakeCallbackQuery(f"composition:{token}")
    run(botmod.handle_composition_callback(cb))

    assert texts(cb.message), "экран состава промолчал на кривых данных"


def test_broken_group_card_does_not_silence_the_screen(agent_ok, monkeypatch):
    _, token = analyse()
    run(botmod.handle_groups_callback(FakeCallbackQuery(f"groups:{token}")))

    def _boom(*a, **kw):
        raise ValueError("сломалась разметка")

    monkeypatch.setattr(botmod, "build_group_card_message", _boom)
    cb = FakeCallbackQuery(f"grp:{token}:emol")
    run(botmod.handle_group_card_callback(cb))

    assert texts(cb.message), "карточка группы промолчала"


def test_ingredients_failure_is_reported(agent_ok, monkeypatch):
    async def _boom(step1_payload):
        raise RuntimeError("модель недоступна")

    _, token = analyse()
    monkeypatch.setattr(botmod, "run_agent_ingredients_ex", _boom)
    cb = FakeCallbackQuery(f"groups:{token}")
    run(botmod.handle_groups_callback(cb))

    assert botmod.ERROR_INGREDIENTS in texts(cb.message)
    assert token not in botmod.INGREDIENTS_INFLIGHT


def test_step2_failure_is_reported(agent_ok, monkeypatch):
    async def _boom(step1_payload):
        raise RuntimeError("модель недоступна")

    _, token = analyse()
    monkeypatch.setattr(botmod, "run_agent_step2_ex", _boom)
    bot = FakeBot()
    run(botmod.handle_step2_callback(FakeCallbackQuery(f"step2:{token}"), bot))

    assert texts(bot), "о сбое шага 2 человеку не сказали"
    assert token not in botmod.STEP2_INFLIGHT
