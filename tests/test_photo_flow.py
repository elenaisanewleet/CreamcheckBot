"""Разбор по фото — путь, на котором бот молча умирал в проде.

Тестов на фото не было вовсе, а фейки в остальных тестах объявляли
`answer` через `async def`, то есть возвращали настоящую корутину.
aiogram 3 так себя не ведёт: `msg.answer()` отдаёт объект `SendMessage` —
awaitable, но НЕ корутину. `asyncio.create_task()` на нём падает TypeError,
и обработчик фото умирал на первой же строке, до любой отправки.

Фейки живут в `tools/tg_fakes.py` — одним набором на все тесты и на живой
прогон: пока «как ведёт себя aiogram» описано в каждом файле заново,
описания расходятся и дыра открывается снова. Верните create_task вместо
ensure_future — эти тесты покраснеют.
"""

import asyncio
import json

import pytest

from agent.agent import AgentResult, Usage
from bot import analytics, bot as botmod
from tools.tg_fakes import FakeBot, FakeMessage, FakeUser

USER = FakeUser()

STEP1_JSON = {
    "product_name": "CeraVe Moisturising Cream",
    "risk_level": "none",
    "source_url": None,
    "ingredients": [
        {"name": "Aqua", "is_hard": False, "is_conditional": False},
        {"name": "Glycerin", "is_hard": False, "is_conditional": False},
    ],
}


@pytest.fixture(autouse=True)
def clean():
    analytics.init()
    analytics._exec("DELETE FROM events")
    analytics._exec("DELETE FROM cache")
    botmod.STEP2_CACHE.clear()
    botmod._ACTIVE_USERS.clear()
    botmod._ALBUM_BUFFER.clear()
    yield
    botmod._ACTIVE_USERS.clear()
    botmod._ALBUM_BUFFER.clear()


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def agent_ok(monkeypatch):
    calls = []

    async def _step1(product_name=None, image_bytes=None, images=None):
        calls.append({"product_name": product_name, "images": images})
        return AgentResult(raw=json.dumps(STEP1_JSON), usage=Usage())

    async def _collect(bot, photos):
        return [b"\xff\xd8fake-jpeg"]

    monkeypatch.setattr(botmod, "run_agent_step1_ex", _step1)
    monkeypatch.setattr(botmod, "_collect_images", _collect)
    return calls


# ─────────────────────────────────────────────────────────────
# Одиночное фото
# ─────────────────────────────────────────────────────────────

def test_single_photo_answers(agent_ok):
    """Главный регресс: на фото бот обязан ответить, а не молчать."""
    msg = FakeMessage(photo=True)
    run(botmod.handle_photo(msg, FakeBot()))

    texts = [s.text for s in msg.sent]
    assert texts, "бот не отправил вообще ничего — ровно тот баг, что был в проде"
    assert texts[0] == botmod.PROCESSING_PHOTO
    assert any("CeraVe" in t for t in texts)
    assert len(agent_ok) == 1


def test_status_plate_is_removed_after_answer(agent_ok):
    msg = FakeMessage(photo=True)
    run(botmod.handle_photo(msg, FakeBot()))
    assert msg.sent[0].deleted is True


def test_photo_result_has_buttons(agent_ok):
    msg = FakeMessage(photo=True)
    run(botmod.handle_photo(msg, FakeBot()))
    final = msg.sent[-1]
    labels = [b.text for row in final.reply_markup.inline_keyboard for b in row]
    assert "🧾 Посмотреть состав" in labels
    assert botmod.BTN_DOCTOR_DIRECT in labels


def test_caption_is_passed_to_the_model(agent_ok):
    msg = FakeMessage(photo=True, caption="CeraVe Moisturising Cream")
    run(botmod.handle_photo(msg, FakeBot()))
    assert agent_ok[0]["product_name"] == "CeraVe Moisturising Cream"


def test_user_lock_is_released_after_photo(agent_ok):
    msg = FakeMessage(photo=True)
    run(botmod.handle_photo(msg, FakeBot()))
    assert USER.id not in botmod._ACTIVE_USERS


# ─────────────────────────────────────────────────────────────
# Сбои
# ─────────────────────────────────────────────────────────────

def test_download_failure_tells_the_user(monkeypatch):
    async def _empty(bot, photos):
        return []

    monkeypatch.setattr(botmod, "_collect_images", _empty)
    msg = FakeMessage(photo=True)
    run(botmod.handle_photo(msg, FakeBot()))

    assert botmod.ERROR_GENERAL in [s.text for s in msg.sent]
    assert USER.id not in botmod._ACTIVE_USERS


def test_message_without_photo_is_answered():
    msg = FakeMessage(photo=False)
    run(botmod.handle_photo(msg, FakeBot()))
    assert [s.text for s in msg.sent] == [botmod.ERROR_EMPTY]


def test_second_photo_while_busy_gets_a_reply(agent_ok, monkeypatch):
    """Занятость — тоже ответ, а не тишина."""
    monkeypatch.setattr(botmod.config, "SINGLE_FLIGHT_PER_USER", True)
    botmod._ACTIVE_USERS[USER.id] = 9e18  # держим замок

    msg = FakeMessage(photo=True)
    run(botmod.handle_photo(msg, FakeBot()))
    assert [s.text for s in msg.sent] == [botmod.BUSY_MESSAGE]


# ─────────────────────────────────────────────────────────────
# Альбом
# ─────────────────────────────────────────────────────────────

def test_album_answers_too(agent_ok, monkeypatch):
    monkeypatch.setattr(botmod.config, "ALBUM_WAIT_SEC", 0.01)
    msg = FakeMessage(photo=True, media_group_id="album-1")

    async def _flow():
        await botmod.handle_photo(msg, FakeBot())
        await asyncio.sleep(0.2)  # даём фоновой задаче собрать альбом

    run(_flow())

    texts = [s.text for s in msg.sent]
    assert texts and texts[0] == botmod.PROCESSING_PHOTO
    assert any("CeraVe" in t for t in texts)
