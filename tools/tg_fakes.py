"""Подставные объекты Telegram, повторяющие поведение aiogram 3.

Зачем отдельный модуль. Сегодня бот молча умирал на каждом фото: обработчик
падал `TypeError` на первой же строке, а тесты этого не видели — их фейки
объявляли `answer` через `async def`, то есть возвращали настоящую корутину.
aiogram так себя не ведёт:

    Message.answer()      → объект SendMessage
    Message.delete()      → объект DeleteMessage
    Message.edit_text()   → объект EditMessageText
    CallbackQuery.answer() → объект AnswerCallbackQuery

Все они awaitable, но НЕ корутины: `asyncio.create_task()` на таком объекте
падает, `inspect.iscoroutine()` возвращает False. Единственный настоящий
`async def` из ходовых — `Bot.send_message()`.

Пока «как ведёт себя aiogram» описано в каждом тесте заново, описания
расходятся, и дыра открывается снова. Поэтому определение здесь одно —
им пользуются и офлайновые тесты (`tests/test_handlers_flow.py`), и живой
сквозной прогон через настоящую модель (`tools/live_flow.py`).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple


class _Request:
    """Объект запроса aiogram: awaitable, но не корутина.

    Ровно то, что ломало `asyncio.create_task()` в проде.
    """

    def _perform(self) -> Any:  # pragma: no cover - переопределяют наследники
        raise NotImplementedError

    def __await__(self):
        async def _run():
            return self._perform()

        return _run().__await__()


class DeleteMessage(_Request):
    def __init__(self, message: "SendMessage") -> None:
        self._message = message

    def _perform(self) -> bool:
        self._message.deleted = True
        return True


class EditMessageText(_Request):
    def __init__(self, message: "SendMessage", text: str, reply_markup: Any = None) -> None:
        self._message = message
        self._text = text
        self._reply_markup = reply_markup

    def _perform(self) -> "SendMessage":
        self._message.text = self._text
        self._message.reply_markup = self._reply_markup
        self._message.edited = True
        return self._message


class SendMessage(_Request):
    """Результат `msg.answer()`: и запрос, и получившееся сообщение.

    Пока его не дождались, ничего не отправлено — как в aiogram.
    """

    def __init__(
        self,
        sink: List["SendMessage"],
        text: str,
        reply_markup: Any = None,
        *,
        chat: Any = None,
        error: Optional[BaseException] = None,
        **kwargs: Any,
    ) -> None:
        self._sink = sink
        self._error = error
        self.text = text
        self.reply_markup = reply_markup
        self.kwargs = kwargs
        self.chat = chat
        self.deleted = False
        self.edited = False
        self.sent = False

    def _perform(self) -> "SendMessage":
        if self._error is not None:
            raise self._error
        self.sent = True
        self._sink.append(self)
        return self

    # Ответ на отправленное сообщение уходит в тот же чат и тот же список.
    def answer(self, text: str, reply_markup: Any = None, **kwargs: Any) -> "SendMessage":
        return SendMessage(self._sink, text, reply_markup, chat=self.chat, **kwargs)

    def edit_text(self, text: str, reply_markup: Any = None, **kwargs: Any) -> EditMessageText:
        return EditMessageText(self, text, reply_markup)

    def delete(self) -> DeleteMessage:
        return DeleteMessage(self)


class AnswerCallbackQuery(_Request):
    """`cb.answer()` — гасит «часики» на кнопке, может показать всплывашку."""

    def __init__(
        self,
        sink: List[Dict[str, Any]],
        text: Optional[str],
        show_alert: bool,
        error: Optional[BaseException] = None,
    ) -> None:
        self._sink = sink
        self._error = error
        self.text = text
        self.show_alert = show_alert

    def _perform(self) -> bool:
        if self._error is not None:
            raise self._error
        self._sink.append({"text": self.text, "show_alert": self.show_alert})
        return True


class FakeUser:
    def __init__(self, user_id: int = 41082373, username: str = "elenaisanewleet") -> None:
        self.id = user_id
        self.username = username
        self.full_name = "Тест"


class FakeChat:
    def __init__(self, chat_id: int = 555) -> None:
        self.id = chat_id
        self.type = "private"


class FakePhoto:
    def __init__(self, file_id: str = "photo-1") -> None:
        self.file_id = file_id
        self.file_unique_id = file_id
        self.width = 1280
        self.height = 960


class FakeMessage:
    """Входящее сообщение. `answer()` — обычный метод, как в aiogram."""

    def __init__(
        self,
        text: Optional[str] = None,
        *,
        photo: bool = False,
        caption: Optional[str] = None,
        media_group_id: Optional[str] = None,
        user: Optional[FakeUser] = None,
        chat: Optional[FakeChat] = None,
        answer_error: Optional[BaseException] = None,
    ) -> None:
        self.text = text
        self.caption = caption
        self.photo = [FakePhoto()] if photo else None
        self.media_group_id = media_group_id
        self.from_user = user or FakeUser()
        self.chat = chat or FakeChat()
        self.sent: List[SendMessage] = []
        # Телеграм умеет отказать в отправке: чат закрыт, флуд-контроль, таймаут.
        self.answer_error = answer_error

    def answer(self, text: str, reply_markup: Any = None, **kwargs: Any) -> SendMessage:
        return SendMessage(
            self.sent, text, reply_markup, chat=self.chat, error=self.answer_error, **kwargs
        )

    def delete(self) -> DeleteMessage:
        return DeleteMessage(SendMessage(self.sent, self.text or ""))


class FakeCallbackQuery:
    """Нажатие на кнопку. `answer()` тоже возвращает объект, а не корутину."""

    def __init__(
        self,
        data: str,
        message: Optional[FakeMessage] = None,
        *,
        user: Optional[FakeUser] = None,
        answer_error: Optional[BaseException] = None,
    ) -> None:
        self.id = "cbq-1"
        self.data = data
        self.message = message if message is not None else FakeMessage()
        self.from_user = user or FakeUser()
        self.answers: List[Dict[str, Any]] = []
        # «query is too old and response timeout expired» — штатная ошибка Telegram.
        self.answer_error = answer_error

    def answer(
        self, text: Optional[str] = None, show_alert: bool = False, **kwargs: Any
    ) -> AnswerCallbackQuery:
        return AnswerCallbackQuery(self.answers, text, show_alert, error=self.answer_error)

    @property
    def sent(self) -> List[SendMessage]:
        return self.message.sent if self.message is not None else []


class FakeBot:
    """`Bot.send_message()` — единственный из ходовых, кто настоящая корутина."""

    def __init__(self, chat_sink: Optional[List[SendMessage]] = None) -> None:
        self.sent: List[SendMessage] = chat_sink if chat_sink is not None else []

    async def send_message(
        self, chat_id: int, text: str, reply_markup: Any = None, **kwargs: Any
    ) -> SendMessage:
        msg = SendMessage(self.sent, text, reply_markup, chat=FakeChat(chat_id))
        return await msg


# ─────────────────────────────────────────────────────────────
# Мелкие помощники для проверок
# ─────────────────────────────────────────────────────────────

def texts(target: Any) -> List[str]:
    """Тексты всего, что реально ушло человеку."""
    sink = getattr(target, "sent", target)
    return [m.text for m in sink]


def buttons(markup: Any) -> List[Tuple[str, Optional[str]]]:
    """[(подпись, callback_data)] — url-кнопки идут с None."""
    if markup is None:
        return []
    out: List[Tuple[str, Optional[str]]] = []
    for row in markup.inline_keyboard:
        for btn in row:
            out.append((btn.text, getattr(btn, "callback_data", None)))
    return out


def callback_data(markup: Any, needle: str) -> Optional[str]:
    """callback_data первой кнопки, в подписи которой встретилось `needle`."""
    for label, data in buttons(markup):
        if needle.lower() in label.lower() and data:
            return data
    return None


def last_with_buttons(sink: Sequence[SendMessage]) -> Optional[SendMessage]:
    for msg in reversed(list(sink)):
        if msg.reply_markup is not None:
            return msg
    return None
