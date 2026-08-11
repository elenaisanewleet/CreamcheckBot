"""Сквозной прогон разбора без обращений к OpenAI и Telegram.

Проверяем главное обещание оптимизации: ответ пользователю не изменился,
а повторный запрос того же продукта отвечает из кэша и не тратит API.
"""

import asyncio
import json

import pytest

from bot import analytics, bot as botmod
from agent.agent import AgentResult, Usage

STEP1_JSON = {
    "product_name": "CeraVe Moisturising Cream",
    "risk_level": "none",
    "summary": "ок",
    "recommendations": [],
    "source_url": None,
    "ingredients": [
        {"name": "Aqua", "is_hard": False, "is_conditional": False},
        {"name": "Glycerin", "is_hard": False, "is_conditional": False},
        {"name": "Dimethicone", "is_hard": False, "is_conditional": True},
        {"name": "Cetearyl Alcohol", "is_hard": False, "is_conditional": False},
    ],
}


class FakeUser:
    id = 777
    username = "tester"
    full_name = "Tester"


class FakeChat:
    id = 555


class Sent:
    """Сообщение, «отправленное» ботом."""

    def __init__(self, text, reply_markup=None):
        self.text = text
        self.reply_markup = reply_markup
        self.deleted = False

    async def delete(self):
        self.deleted = True


class FakeMessage:
    def __init__(self, text=None):
        self.text = text
        self.caption = None
        self.photo = None
        self.media_group_id = None
        self.from_user = FakeUser()
        self.chat = FakeChat()
        self.sent = []

    async def answer(self, text, reply_markup=None, **kwargs):
        s = Sent(text, reply_markup)
        self.sent.append(s)
        return s


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    analytics.init()
    analytics._exec("DELETE FROM events")
    analytics._exec("DELETE FROM users")
    analytics._exec("DELETE FROM cache")
    botmod.STEP2_CACHE.clear()
    botmod.STEP2_INFLIGHT.clear()
    botmod._ACTIVE_USERS.clear()
    yield


def run(coro):
    return asyncio.run(coro)


def _fake_agent(calls):
    async def _run(product_name=None, image_bytes=None, images=None):
        calls.append(product_name)
        return AgentResult(
            raw=json.dumps(STEP1_JSON, ensure_ascii=False),
            usage=Usage(in_tokens=1200, out_tokens=400, tool_calls=2, api_ms=3000),
        )

    return _run


# ── Основной сценарий ──────────────────────────────────────

def test_text_query_answers_and_attaches_buttons(monkeypatch):
    calls = []
    monkeypatch.setattr(botmod, "run_agent_step1_ex", _fake_agent(calls))

    msg = FakeMessage("CeraVe Moisturising Cream")
    run(botmod._run_step1_and_answer(msg, bot=None, product_name=msg.text, source="text"))

    # первое сообщение — статус, второе — результат
    assert msg.sent[0].text == botmod.PROCESSING_TEXT
    assert msg.sent[0].deleted is True

    answer = msg.sent[1]
    assert "CeraVe Moisturising Cream" in answer.text
    # Dimethicone на позиции 3 (≤5) и он один → средний риск
    assert "Средний риск комедогенности" in answer.text
    assert "@DrDubinsky" in answer.text

    buttons = [b.text for row in answer.reply_markup.inline_keyboard for b in row]
    assert buttons == ["🧾 Посмотреть состав", "📘 Подробнее"]
    assert len(calls) == 1


def test_repeat_query_served_from_cache(monkeypatch):
    calls = []
    monkeypatch.setattr(botmod, "run_agent_step1_ex", _fake_agent(calls))

    first = FakeMessage("CeraVe Moisturising Cream")
    run(botmod._run_step1_and_answer(first, None, product_name=first.text, source="text"))

    second = FakeMessage("  cerave   moisturising cream  ")  # другой регистр и пробелы
    run(botmod._run_step1_and_answer(second, None, product_name=second.text, source="text"))

    assert len(calls) == 1, "повторный запрос не должен идти в OpenAI"
    assert second.sent[1].text == first.sent[1].text, "ответ из кэша обязан совпадать"

    stats = run(analytics.period_stats(0))
    assert stats["analyses"] == 2
    assert stats["from_cache"] == 1


def test_cache_disabled_still_works(monkeypatch):
    calls = []
    monkeypatch.setattr(botmod, "run_agent_step1_ex", _fake_agent(calls))
    monkeypatch.setattr(botmod.config, "CACHE_ENABLED", False)

    for _ in range(2):
        m = FakeMessage("Крем")
        run(botmod._run_step1_and_answer(m, None, product_name="Крем", source="text"))

    assert len(calls) == 2


def test_no_inci_answer_has_no_buttons(monkeypatch):
    async def _run(product_name=None, image_bytes=None, images=None):
        return AgentResult(
            raw=json.dumps({"product_name": "Неизвестный крем", "error": "no_inci",
                            "message": "Состав не найден", "recommendations": []}),
            usage=Usage(),
        )

    monkeypatch.setattr(botmod, "run_agent_step1_ex", _run)
    msg = FakeMessage("Неизвестный крем")
    run(botmod._run_step1_and_answer(msg, None, product_name=msg.text, source="text"))

    assert "Состав не найден" in msg.sent[1].text
    assert msg.sent[1].reply_markup is None
    assert run(analytics.period_stats(0))["no_inci"] == 1


def test_no_inci_is_not_cached(monkeypatch):
    calls = []

    async def _run(product_name=None, image_bytes=None, images=None):
        calls.append(product_name)
        return AgentResult(raw=json.dumps({"product_name": "X", "error": "no_inci"}), usage=Usage())

    monkeypatch.setattr(botmod, "run_agent_step1_ex", _run)
    for _ in range(2):
        m = FakeMessage("X")
        run(botmod._run_step1_and_answer(m, None, product_name="X", source="text"))

    assert len(calls) == 2, "ненайденный состав кэшировать нельзя — вдруг найдётся позже"


def test_api_failure_shows_friendly_error(monkeypatch):
    async def _boom(product_name=None, image_bytes=None, images=None):
        raise RuntimeError("OpenAI недоступен")

    monkeypatch.setattr(botmod, "run_agent_step1_ex", _boom)
    msg = FakeMessage("Крем")
    run(botmod._run_step1_and_answer(msg, None, product_name="Крем", source="text"))

    assert msg.sent[-1].text == botmod.ERROR_GENERAL
    errors = run(analytics.recent_errors())
    assert "OpenAI недоступен" in errors[0]["detail"]


def test_user_lock_released_after_cache_hit(monkeypatch):
    """Блокировка «один разбор на человека» не должна залипать на кэш-ответах."""
    monkeypatch.setattr(botmod, "run_agent_step1_ex", _fake_agent([]))

    for _ in range(3):
        m = FakeMessage("Крем")
        assert botmod._try_acquire(FakeUser.id)[0] is True
        run(botmod._run_step1_and_answer(m, None, product_name="Крем", source="text"))
        assert FakeUser.id not in botmod._ACTIVE_USERS


def test_usage_is_recorded(monkeypatch):
    monkeypatch.setattr(botmod, "run_agent_step1_ex", _fake_agent([]))
    msg = FakeMessage("Крем")
    run(botmod._run_step1_and_answer(msg, None, product_name="Крем", source="text"))

    ev = run(analytics.recent_events(1))[0]
    assert ev["in_tokens"] == 1200
    assert ev["out_tokens"] == 400
    assert ev["tool_calls"] == 2
    assert ev["cost_usd"] > 0
    assert ev["risk"] == "medium"


def test_long_text_is_rejected_before_api(monkeypatch):
    calls = []
    monkeypatch.setattr(botmod, "run_agent_step1_ex", _fake_agent(calls))

    msg = FakeMessage("а" * (botmod.config.MAX_QUERY_LEN + 1))
    run(botmod.handle_text(msg, bot=None))

    assert msg.sent[0].text == botmod.TOO_LONG_MESSAGE
    assert not calls


def test_second_request_while_busy_is_refused():
    assert botmod._try_acquire(FakeUser.id)[0] is True
    allowed, reason = botmod._try_acquire(FakeUser.id)
    assert allowed is False and reason == "busy"
    botmod._release(FakeUser.id)
    assert botmod._try_acquire(FakeUser.id)[0] is True


# ── Шаг 2 ──────────────────────────────────────────────────

STEP2_JSON = {
    "summary": "Состав спокойный.",
    "comedogens_notes": [{"name": "Dimethicone", "position": 3, "type": "conditional",
                          "note": "Может создавать окклюзию."}],
    "overall_notes": "Реакция индивидуальна.",
    "recommendations": ["Вводи постепенно.", "Наблюдай 7–14 дней."],
}


class FakeBot:
    def __init__(self):
        self.messages = []

    async def send_message(self, chat_id, text, **kwargs):
        self.messages.append(text)


def test_step2_renders_and_caches(monkeypatch):
    calls = []

    async def _run2(step1_payload):
        calls.append(step1_payload)
        return AgentResult(raw=json.dumps(STEP2_JSON, ensure_ascii=False),
                           usage=Usage(in_tokens=800, out_tokens=300))

    monkeypatch.setattr(botmod, "run_agent_step2_ex", _run2)

    step1 = {
        "product_name": "CeraVe Moisturising Cream",
        "risk_level": "medium",
        "source_url": None,
        "ingredients": STEP1_JSON["ingredients"],
    }

    fb = FakeBot()
    run(botmod._run_step2_background(fb, 555, step1, "tok1", FakeUser()))
    assert "Подробнее" in fb.messages[0]
    assert "Dimethicone" in fb.messages[0]
    assert "Вводи постепенно." in fb.messages[0]

    # второй раз — из кэша, без обращения к модели
    fb2 = FakeBot()
    run(botmod._run_step2_background(fb2, 555, step1, "tok2", FakeUser()))
    assert len(calls) == 1
    assert fb2.messages[0] == fb.messages[0]


def test_step2_token_is_released(monkeypatch):
    async def _run2(step1_payload):
        return AgentResult(raw=json.dumps(STEP2_JSON), usage=Usage())

    monkeypatch.setattr(botmod, "run_agent_step2_ex", _run2)
    token = botmod._cache_put({"product_name": "X", "ingredients": []})
    botmod.STEP2_INFLIGHT[token] = 1.0
    run(botmod._run_step2_background(FakeBot(), 1, {"product_name": "X", "ingredients": []}, token))
    assert token not in botmod.STEP2_CACHE
    assert token not in botmod.STEP2_INFLIGHT


def test_expired_tokens_are_swept():
    token = botmod._cache_put({"product_name": "X"})
    botmod.STEP2_CACHE[token]["ts"] = 0  # как будто прошло много времени
    botmod._sweep_memory_caches()
    assert token not in botmod.STEP2_CACHE
