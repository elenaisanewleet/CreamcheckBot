"""
ComedoBot — Telegram bot (aiogram 3) — FINAL BALANCED VERSION

Логика (не менялась):
- Шаг 1: краткий результат (риск + контекст) + 2 кнопки
- Кнопка 1: "Посмотреть состав" → полный список ингредиентов
- Кнопка 2: "Подробнее" → пояснение + рекомендации

Что добавлено в оптимизации:
- аналитика: кто, когда и чем пользуется (SQLite + /stats + веб-дашборд)
- кэш готовых разборов: повтор популярного продукта отвечает мгновенно и бесплатно
- проверка ссылки-источника убрана с «горячего» пути — ответ приходит быстрее
- альбом из нескольких фото уходит в модель ОДНИМ запросом
- один тяжёлый анализ на пользователя одновременно
"""

import asyncio
import hashlib
import html
import json
import logging
import re
import secrets
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    PhotoSize,
)

from . import admin, analytics, config, dashboard
from .config import TELEGRAM_BOT_TOKEN
from agent.agent import (
    build_ingredients_payload,
    build_step2_payload,
    run_agent_ingredients_ex,
    run_agent_step1_ex,
    run_agent_step2_ex,
)
from agent.groups import group_label, is_valid_group


analytics.setup_logging()
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# Визуальные разделители
# ─────────────────────────────────────────────────────────────

DIVIDER_LIGHT = "· · · · · · · · · · · · · · · · · · · · · · ·"
DIVIDER_ACCENT = "🫧 · · · · · · · · · · · · · · · · · · · 🫧"

# ─────────────────────────────────────────────────────────────
# Тексты (UX)
# ─────────────────────────────────────────────────────────────

START_MESSAGE = f"""Привет 🫧

Я помогаю оценить риск комедогенности — то есть вероятность забивания пор. 🤍

Пришли фото средства (упаковку или оборот с составом) или напиши название — бренд и продукт 🩵

В ответ ты получишь:
• уровень риска
• краткое пояснение
• кнопки: «Посмотреть состав» и «Подробнее» ✨"""

HELP_MESSAGE = f"""<b>Как пользоваться</b> 🫧

Отправь фото средства или напиши его название.

Ты получишь:
• уровень риска комедогенности
• краткое пояснение
• кнопку «🧾 Посмотреть состав» — полный список ингредиентов с отметками
• кнопку «📘 Подробнее» — пояснение и рекомендации

{DIVIDER_LIGHT}

<b>Как интерпретировать результат</b> 🤍

🔴 <b>Высокий риск</b>
Есть компоненты, которые чаще провоцируют комедоны у склонной кожи.

🟠 <b>Средний риск</b>
Есть условно-комедогенные компоненты — возможна реакция при регулярном использовании.

🟡 <b>Низкий риск</b>
Есть отдельные условно-комедогенные компоненты — чаще переносится спокойно, но индивидуальная реакция возможна.

⚪️ <b>Риск не выявлен</b>
Комедогенные компоненты не обнаружены.

{DIVIDER_LIGHT}

<b>Важно</b> 🌸

Комедогенность — не абсолютная характеристика: реакция кожи индивидуальна и зависит от множества факторов.

Высокий риск не означает, что средство точно не подойдёт. Низкий риск не гарантирует отсутствие реакции.

Это инструмент для информированного выбора, а не диагностика.

{DIVIDER_LIGHT}

<b>Команды</b> 🫧

/about — о боте и ограничениях
/contacts — контакты"""

ABOUT_MESSAGE = f"""<b>О боте</b> 🤍

{DIVIDER_LIGHT}

<b>Что это</b>

CreamcheckBot помогает ориентироваться в составе косметики и оценивать риск комедогенности.

{DIVIDER_LIGHT}

<b>Что это НЕ</b>

• Не медицинская консультация
• Не диагностика состояния кожи
• Не гарантия реакции или её отсутствия
• Не повод отменять назначенное лечение

{DIVIDER_LIGHT}

<b>Куда за консультацией</b> 🩵

По вопросам кожи, консультаций и услуг: @DrDubinsky

{DIVIDER_LIGHT}

<b>По вопросам работы бота</b> 🤍

По вопросам работы бота, автоматизации и разработки сервисов: @elenaisanewleet"""

CONTACTS_MESSAGE = f"""<b>Контакты</b> 🫧

{DIVIDER_LIGHT}

<b>Консультации по коже и услугам</b> 🩵

Если нужен разбор под твою кожу, консультация дерматолога или запись на услуги — напиши доктору Лизе Дубинской:

@DrDubinsky

{DIVIDER_LIGHT}

<b>Вопросы по работе бота и разработке</b> 🤍

Если бот работает некорректно, не нашёл состав, выдал странный результат или есть идея по улучшению — можно написать по этому контакту.

По вопросам работы бота, автоматизации и разработки сервисов: @elenaisanewleet"""

PROCESSING_PHOTO = "🫧 Анализирую фото…"
PROCESSING_TEXT = "🫧 Ищу состав…"
PROCESSING_STEP2 = "🫧 Готовлю подробное пояснение…"
PROCESSING_INGREDIENTS = "🫧 Разбираю состав по компонентам — это займёт полминуты…"
ERROR_INGREDIENTS = "Не удалось разобрать состав по компонентам. Попробуй ещё раз."
ERROR_GENERAL = "Не удалось обработать запрос. Попробуй ещё раз или отправь другое фото."
ERROR_EMPTY = "Отправь фото средства или напиши его название."
BUSY_MESSAGE = "🫧 Ещё разбираю предыдущий запрос — отвечу через несколько секунд."
TOO_LONG_MESSAGE = "Напиши покороче: бренд и название продукта — этого достаточно 🤍"




# ─────────────────────────────────────────────────────────────
# Вспомогательное
# ─────────────────────────────────────────────────────────────

# Один HTTP-клиент на весь процесс: соединения переиспользуются,
# не тратим время на TLS-хендшейк для каждой картинки.
_http: Optional[httpx.AsyncClient] = None


def _http_client() -> httpx.AsyncClient:
    global _http
    if _http is None or _http.is_closed:
        _http = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=True,
            limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
        )
    return _http


async def _download_photo(bot: Bot, photo: PhotoSize) -> bytes:
    file = await bot.get_file(photo.file_id)
    url = f"https://api.telegram.org/file/bot{bot.token}/{file.file_path}"
    resp = await _http_client().get(url)
    resp.raise_for_status()
    return resp.content


def _maybe_shrink(image: bytes) -> bytes:
    """Опциональное сжатие фото (IMAGE_MAX_SIDE). По умолчанию выключено:
    мелкий шрифт состава лучше распознаётся в оригинальном разрешении."""
    if config.IMAGE_MAX_SIDE <= 0:
        return image
    try:
        from io import BytesIO

        from PIL import Image  # type: ignore

        img = Image.open(BytesIO(image))
        if max(img.size) <= config.IMAGE_MAX_SIDE:
            return image
        img.thumbnail((config.IMAGE_MAX_SIDE, config.IMAGE_MAX_SIDE), Image.LANCZOS)
        out = BytesIO()
        img.convert("RGB").save(out, format="JPEG", quality=config.IMAGE_JPEG_QUALITY, optimize=True)
        shrunk = out.getvalue()
        logger.info("Фото сжато: %d → %d байт", len(image), len(shrunk))
        return shrunk if shrunk else image
    except Exception as exc:  # noqa: BLE001 — Pillow может отсутствовать
        logger.debug("Сжатие фото пропущено: %s", exc)
        return image


RISK_LABELS = {
    "high": "🔴 <b>Высокий риск комедогенности</b>",
    "medium": "🟠 <b>Средний риск комедогенности</b>",
    "low": "🟡 <b>Низкий риск комедогенности</b>",
    "none": "⚪️ <b>Риск комедогенности не выявлен</b>",
}

RISK_SHORT = {
    "high": "🔴 высокий",
    "medium": "🟠 средний",
    "low": "🟡 низкий",
    "none": "⚪️ не выявлен",
}

RISK_CONTEXT = {
    "high": "В составе есть компоненты, которые чаще провоцируют комедоны у склонной кожи. Это не означает, что средство точно не подойдёт — реакция индивидуальна. Попробуй тест на небольшом участке и понаблюдай 3–7 дней. 🤍",
    "medium": "В составе есть условно-комедогенные компоненты. Если кожа склонна к комедонам, вводи средство постепенно и отслеживай реакцию. 🫧",
    "low": "Есть отдельные условно-комедогенные компоненты. Риск обычно невысокий, но индивидуальная реакция возможна. 🌸",
    "none": "Комедогенные компоненты не обнаружены. С точки зрения комедогенности состав выглядит спокойным. 🤍",
}

EARLY_CUTOFF = 5  # используется в строгой логике, но не объясняется пользователю

# ─────────────────────────────────────────────────────────────
# Валидация source_url (чтобы не показывать "битые" ссылки)
# ─────────────────────────────────────────────────────────────

_URL_RE = re.compile(r"^https?://", flags=re.I)


def _normalize_source_url(value: Any) -> Optional[str]:
    """Возвращает URL только если это похоже на реальную ссылку."""
    if not isinstance(value, str):
        return None
    url = value.strip()
    if not url:
        return None
    if not _URL_RE.match(url):
        return None
    # минимальная чистка от кавычек/пробелов
    url = url.strip(' "\'')
    if not _URL_RE.match(url):
        return None
    return url


def _norm_text_for_match(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"\s+", " ", s)
    return s.strip()


async def _validate_source_url(url: str, ingredients: List[Dict[str, Any]]) -> bool:
    """
    Быстрая проверка:
    - страница открывается (200)
    - на странице встречаются несколько ингредиентов из списка
    """
    if not url:
        return False

    # берем несколько первых "наиболее вероятных" ингредиентов (обычно они точно есть в INCI-блоке)
    sample_names: List[str] = []
    for ing in ingredients[:12]:
        name = (ing.get("name") or "").strip()
        if name and len(name) >= 4:
            sample_names.append(name)

    if not sample_names:
        return False

    # если ингредиентов мало — не блокируем ссылку излишне строго
    needed = min(3, len(sample_names))

    try:
        timeout = httpx.Timeout(6.0, connect=5.0)
        resp = await _http_client().get(
            url, headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout
        )
        if resp.status_code != 200:
            return False
        text = _norm_text_for_match(resp.text)

        # считаем попадания по подстроке (не идеально, но достаточно, чтобы отсеять "не туда")
        hits = 0
        for n in sample_names:
            nn = _norm_text_for_match(n)
            if nn and nn in text:
                hits += 1
            if hits >= needed:
                return True

        # если совсем не нашли совпадений — вероятно это не страница состава
        return False

    except Exception:
        return False


async def _source_url_ok(url: str, ingredients: List[Dict[str, Any]]) -> bool:
    """Та же проверка, но с кэшем: одну и ту же страницу не дёргаем повторно."""
    cached = await analytics.url_check_get(url)
    if cached is not None:
        return cached
    ok = await _validate_source_url(url, ingredients)
    await analytics.url_check_put(url, ok)
    return ok


def _warm_source_url(url: Optional[str], ingredients: List[Dict[str, Any]]) -> None:
    """Прогреваем проверку ссылки в фоне — чтобы к нажатию «Состав» она была готова.

    Раньше эта проверка выполнялась ДО ответа пользователю и добавляла к каждому
    разбору до 6 секунд ожидания. Теперь ответ уходит сразу.
    """
    if not url or not ingredients:
        return

    async def _job() -> None:
        try:
            await _source_url_ok(url, ingredients)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Прогрев проверки ссылки не удался: %s", exc)

    asyncio.create_task(_job())


def calc_risk_level_strict(ingredients: List[Dict[str, Any]]) -> str:
    hard_positions: List[int] = []
    conditional_positions: List[int] = []
    early_conditionals: List[int] = []

    for idx, ing in enumerate(ingredients, start=1):
        if ing.get("is_hard"):
            hard_positions.append(idx)
        if ing.get("is_conditional"):
            conditional_positions.append(idx)
            if idx <= EARLY_CUTOFF:
                early_conditionals.append(idx)

    if hard_positions or len(early_conditionals) >= 2:
        return "high"
    if len(early_conditionals) == 1 and not hard_positions:
        return "medium"
    if conditional_positions and not early_conditionals and not hard_positions:
        return "low"
    if not hard_positions and not conditional_positions:
        return "none"
    return "none"


# Состояние одного разбора в памяти: данные шага 1 плюс всё, что человек
# успел раскрыть кнопками. Живёт, пока по кнопкам ходят, — каждое обращение
# продлевает срок. Час без действий — и токен убирает уборщик.
STEP2_CACHE: Dict[str, Dict[str, Any]] = {}
STEP2_CACHE_TTL_SEC = 60 * 60
STEP2_INFLIGHT: Dict[str, float] = {}
INGREDIENTS_INFLIGHT: Dict[str, float] = {}


def _cache_put(step1_data: Dict[str, Any]) -> str:
    token = secrets.token_urlsafe(8)
    STEP2_CACHE[token] = {"ts": time.time(), "data": step1_data}
    return token


def _cache_get(token: str) -> Optional[Dict[str, Any]]:
    item = STEP2_CACHE.get(token)
    if not item:
        return None
    if time.time() - float(item.get("ts", 0)) > STEP2_CACHE_TTL_SEC:
        STEP2_CACHE.pop(token, None)
        STEP2_INFLIGHT.pop(token, None)
        INGREDIENTS_INFLIGHT.pop(token, None)
        return None
    # Человек листает карточки — значит разбор ещё нужен.
    item["ts"] = time.time()
    return item.get("data")


def _slot_get(token: str, slot: str) -> Optional[Dict[str, Any]]:
    """Готовый шаг 2 / разбор состава для этого токена, если уже считали."""
    item = STEP2_CACHE.get(token)
    return (item or {}).get(slot)


def _slot_put(token: str, slot: str, value: Dict[str, Any]) -> None:
    item = STEP2_CACHE.get(token)
    if item is not None:
        item[slot] = value
        item["ts"] = time.time()


def _cache_del(token: str) -> None:
    STEP2_CACHE.pop(token, None)
    STEP2_INFLIGHT.pop(token, None)
    INGREDIENTS_INFLIGHT.pop(token, None)


def _sweep_memory_caches() -> None:
    """Чистит протухшие токены — иначе словарь растёт бесконечно."""
    now = time.time()
    stale = [t for t, item in STEP2_CACHE.items()
             if now - float(item.get("ts", 0)) > STEP2_CACHE_TTL_SEC]
    for t in stale:
        STEP2_CACHE.pop(t, None)
        STEP2_INFLIGHT.pop(t, None)
        INGREDIENTS_INFLIGHT.pop(t, None)
    for t in [t for t, ts in STEP2_INFLIGHT.items() if now - ts > 600]:
        STEP2_INFLIGHT.pop(t, None)
    for t in [t for t, ts in INGREDIENTS_INFLIGHT.items() if now - ts > 600]:
        INGREDIENTS_INFLIGHT.pop(t, None)
    if stale:
        logger.debug("Очищено %d просроченных токенов", len(stale))


def _parse_agent_json(raw: str) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(raw)
    except Exception as e:
        logging.error("JSON parse error: %s", e)
        return None


DOCTOR_URL = f"https://t.me/{config.DOCTOR_USERNAME}"

BTN_DOCTOR = "❓ Что спросить у врача"
BTN_GROUPS = "🧩 Что делает каждый компонент"


BTN_DOCTOR_DIRECT = "🩺 Спросить Лизу Дубинскую"


def _doctor_link_button() -> InlineKeyboardButton:
    return InlineKeyboardButton(text=BTN_DOCTOR_DIRECT, url=DOCTOR_URL)


def _build_step1_keyboard(token: str) -> InlineKeyboardMarkup:
    # Кнопка к врачу есть уже здесь: разбор состава не отвечает на вопрос
    # «подойдёт ли это мне», и предложить живого специалиста уместно сразу.
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🧾 Посмотреть состав", callback_data=f"composition:{token}")],
            [InlineKeyboardButton(text="📘 Подробнее", callback_data=f"step2:{token}")],
            [_doctor_link_button()],
        ]
    )


def _build_step2_keyboard(token: str, *, has_questions: bool) -> Optional[InlineKeyboardMarkup]:
    """None вместо пустой клавиатуры: Telegram не принимает разметку без кнопок."""
    rows = []
    if config.INGREDIENTS_ENABLED:
        rows.append([InlineKeyboardButton(text=BTN_GROUPS, callback_data=f"groups:{token}")])
    if has_questions:
        rows.append([InlineKeyboardButton(text=BTN_DOCTOR, callback_data=f"doc:{token}")])
    else:
        rows.append([_doctor_link_button()])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def _build_groups_keyboard(
    token: str, groups: Sequence[Dict[str, Any]], *, has_questions: bool
) -> InlineKeyboardMarkup:
    """Кнопка на каждую непустую группу; счётчик показывает, сколько внутри."""
    rows = []
    for grp in groups:
        key = grp.get("key")
        items = grp.get("items") or []
        if not key or not items or not is_valid_group(key):
            continue
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{group_label(key)} · {len(items)}",
                    callback_data=f"grp:{token}:{key}",
                )
            ]
        )
    if has_questions:
        rows.append([InlineKeyboardButton(text=BTN_DOCTOR, callback_data=f"doc:{token}")])
    else:
        rows.append([_doctor_link_button()])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _build_group_card_keyboard(
    token: str, group: Dict[str, Any], *, has_questions: bool
) -> InlineKeyboardMarkup:
    rows = []

    # Вопросы по этой конкретной группе — ближе к тому, что человек только что
    # прочитал, чем общий список по всему средству.
    own = group_questions(group)
    if own:
        rows.append([
            InlineKeyboardButton(
                text=f"🩺 {_plural_questions(len(own))} по этой группе",
                callback_data=f"gq:{token}:{group.get('key')}",
            )
        ])

    rows.append([InlineKeyboardButton(text="← Все группы", callback_data=f"groups:{token}")])
    if has_questions:
        rows.append([InlineKeyboardButton(text=BTN_DOCTOR, callback_data=f"doc:{token}")])
    else:
        rows.append([_doctor_link_button()])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _build_doctor_keyboard(token: str) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=f"💬 Написать @{config.DOCTOR_USERNAME}", url=DOCTOR_URL
            )
        ]
    ]
    if config.INGREDIENTS_ENABLED:
        rows.append([InlineKeyboardButton(text="← К составу", callback_data=f"groups:{token}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _mark_for_component(is_hard: bool, is_cond: bool) -> str:
    if is_hard:
        return "🔴"
    if is_cond:
        return "🟠"
    return "⚪️"


def _esc(t: Any) -> str:
    """Экранирует текст перед вставкой в HTML-разметку сообщения.

    Сюда попадают названия продуктов и ингредиентов, а также пояснения от
    модели. В INCI регулярно встречаются смеси вида «Tocopherol & Ascorbyl
    Palmitate», а в тексте — сравнения вроде «<1%». Без экранирования Telegram
    отвечает `can't parse entities`, и человек не получает сообщение вообще.
    """
    return html.escape(str(t or ""), quote=False)


def _clean_text(t: str) -> str:
    t = (t or "").strip()
    t = re.sub(r"\n{3,}", "\n\n", t)
    t = re.sub(r"\bпо вашим данным\b[:,]?\s*", "", t, flags=re.IGNORECASE)
    return t


_SENT_SPLIT = re.compile(r"(?<=[.!?…])\s+")


def _short_text(t: str, *, max_sentences: int = 2, max_chars: int = 500) -> str:
    t = _clean_text(t or "")
    if not t:
        return ""
    parts = _SENT_SPLIT.split(t)
    out = " ".join(parts[:max_sentences]).strip()
    if len(out) > max_chars:
        out = out[:max_chars].rstrip(" ,.;:—-") + "…"
    return out


def _plural_components(n: int) -> str:
    """«1 компонент», «3 компонента», «14 компонентов»."""
    tail = n % 100
    if 11 <= tail <= 14:
        word = "компонентов"
    else:
        last = n % 10
        if last == 1:
            word = "компонент"
        elif 2 <= last <= 4:
            word = "компонента"
        else:
            word = "компонентов"
    return f"{n} {word}"


def _plural_questions(n: int) -> str:
    """«1 вопрос», «3 вопроса», «5 вопросов»."""
    tail = n % 100
    if 11 <= tail <= 14:
        word = "вопросов"
    else:
        last = n % 10
        if last == 1:
            word = "вопрос"
        elif 2 <= last <= 4:
            word = "вопроса"
        else:
            word = "вопросов"
    return f"{n} {word}"


# У Telegram жёсткий предел 4096 символов на сообщение. Карточка большой
# группы с пояснениями по каждому компоненту может в него не влезть.
TELEGRAM_LIMIT = 4000


def _split_message(text: str, limit: int = TELEGRAM_LIMIT) -> List[str]:
    """Режет длинный текст по границам абзацев, не разрывая строки."""
    if len(text) <= limit:
        return [text]

    chunks: List[str] = []
    current = ""
    for block in text.split("\n\n"):
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        # Отдельный абзац длиннее лимита — режем жёстко, иначе Telegram откажет.
        while len(block) > limit:
            chunks.append(block[:limit])
            block = block[limit:]
        current = block
    if current:
        chunks.append(current)
    return chunks


async def _answer_long(message: Message, text: str, **kwargs: Any) -> None:
    """Клавиатуру вешаем на последний кусок, чтобы кнопки были внизу."""
    parts = _split_message(text)
    for part in parts[:-1]:
        await message.answer(part)
    await message.answer(parts[-1], **kwargs)


def build_doctor_cta_footer() -> str:
    lines = [
        DIVIDER_LIGHT,
        "",
        "💭 <i>Информация для ориентира и не является медицинской консультацией.</i>",
        "💬 За консультацией по коже и услугам: @DrDubinsky",
        "",
        DIVIDER_ACCENT,
    ]
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────
# Формат сообщений
# ─────────────────────────────────────────────────────────────

def build_step1_brief_message(data: Dict[str, Any]) -> str:
    if data.get("error") == "no_inci":
        product_name = data.get("product_name") or "Продукт"
        lines = [
            DIVIDER_ACCENT,
            "",
            "<b>Состав не найден</b> 🤍",
            "",
            f"🧴 <b>{_esc(product_name)}</b>",
            "",
            DIVIDER_LIGHT,
            "",
            "Не удалось найти достоверный состав в открытых источниках.",
            "",
            "<b>Что можно сделать:</b>",
            "",
            "• сделай фото оборотной стороны упаковки при хорошем освещении",
            "• проверь, чтобы текст состава был чётким и не размытым",
            "• отправь точное название (бренд + линейка + продукт)",
            "",
        ]
        lines.append(build_doctor_cta_footer())
        return "\n".join(lines)

    product_name = data.get("product_name") or "Продукт"
    risk_level = data.get("risk_level") or "none"

    lines = [
        DIVIDER_ACCENT,
        "",
        RISK_LABELS.get(risk_level, RISK_LABELS["none"]),
        "",
        f"🧴 <b>{_esc(product_name)}</b>",
        "",
        DIVIDER_LIGHT,
        "",
        RISK_CONTEXT.get(risk_level, ""),
        "",
    ]
    lines.append(build_doctor_cta_footer())
    return "\n".join(lines)


def build_composition_message(data: Dict[str, Any]) -> str:
    product_name = data.get("product_name") or "Продукт"
    ingredients = data.get("ingredients") or []
    source_url = data.get("source_url")

    lines = [
        DIVIDER_ACCENT,
        "",
        "<b>Состав</b> 🫧",
        "",
        f"🧴 <b>{_esc(product_name)}</b>",
        "",
        DIVIDER_LIGHT,
        "",
    ]

    has_comedogens = any(bool(ing.get("is_hard") or ing.get("is_conditional")) for ing in ingredients)

    if has_comedogens:
        lines.append("<b>Отмеченные компоненты:</b>")
        lines.append("")
        for idx, ing in enumerate(ingredients, start=1):
            name = ing.get("name")
            if not name:
                continue
            is_hard = bool(ing.get("is_hard"))
            is_cond = bool(ing.get("is_conditional"))
            if is_hard or is_cond:
                mark = _mark_for_component(is_hard, is_cond)
                lines.append(f"{mark} {idx}. {_esc(name)}")
        lines.append("")
        lines.append(DIVIDER_LIGHT)
        lines.append("")

    lines.append("<b>Список ингредиентов:</b>")
    lines.append("")
    for idx, ing in enumerate(ingredients, start=1):
        name = ing.get("name")
        if not name:
            continue
        mark = _mark_for_component(bool(ing.get("is_hard")), bool(ing.get("is_conditional")))
        lines.append(f"{mark} {idx}. {_esc(name)}")

    lines.append("")
    lines.append(DIVIDER_LIGHT)
    lines.append("")

    lines.append("💭 <b>Обозначения:</b>")
    lines.append("")
    lines.append("🔴 — жёсткие комедогенные компоненты")
    lines.append("🟠 — условно-комедогенные компоненты")
    lines.append("⚪️ — не отмечены как комедогенные")

    # ВАЖНО: источник показываем только если это валидный URL
    if source_url:
        lines.append("")
        lines.append(DIVIDER_LIGHT)
        lines.append("")
        lines.append("🔗 <b>Источник состава:</b>")
        lines.append(f'<a href="{source_url}">Открыть страницу</a>')

    lines.append("")
    lines.append(build_doctor_cta_footer())

    return "\n".join(lines)


def build_step2_message(step2_data: Dict[str, Any], product_name: Optional[str] = None, risk_level: Optional[str] = None) -> str:
    summary = _short_text(step2_data.get("summary") or "", max_sentences=3, max_chars=650)
    overall = _short_text(step2_data.get("overall_notes") or "", max_sentences=2, max_chars=420)
    notes = step2_data.get("comedogens_notes") or []
    recs = step2_data.get("recommendations") or []

    lines = [
        DIVIDER_ACCENT,
        "",
        "<b>Подробнее</b> 🩵",
        "",
    ]

    if product_name:
        lines.append(f"🧴 <b>{_esc(product_name)}</b>")
    if risk_level:
        lines.append(f"🏷️ Уровень риска: <b>{RISK_SHORT.get(risk_level, '⚪️ не выявлен')}</b>")

    lines.append("")
    lines.append(DIVIDER_LIGHT)
    lines.append("")

    if summary:
        lines.append("<b>Коротко о результате</b> 🤍")
        lines.append("")
        lines.append(_esc(summary))
        lines.append("")
        lines.append(DIVIDER_LIGHT)
        lines.append("")

    if notes:
        lines.append("<b>Что в составе может быть чувствительным для склонной кожи</b> 🫧")
        lines.append("")
        for item in notes[:5]:
            name = (item.get("name") or "").strip()
            typ = (item.get("type") or "").strip().lower()
            pos = item.get("position")
            note = _short_text(item.get("note") or "", max_sentences=1, max_chars=260)

            if not name:
                continue

            is_hard = (typ == "hard")
            is_cond = (typ == "conditional")
            mark = _mark_for_component(is_hard, is_cond)

            pos_txt = f" (№{pos})" if isinstance(pos, int) else ""
            lines.append(f"{mark} <b>{_esc(name)}{pos_txt}</b>")
            if note:
                lines.append(_esc(note))
            lines.append("")

        while lines and lines[-1] == "":
            lines.pop()
        lines.append("")
        lines.append(DIVIDER_LIGHT)
        lines.append("")

    if overall and not summary:
        lines.append("<b>Общая оценка</b> 🌸")
        lines.append("")
        lines.append(_esc(overall))
        lines.append("")
        lines.append(DIVIDER_LIGHT)
        lines.append("")

    if recs:
        lines.append("<b>Что это значит на практике</b> ✨")
        lines.append("")
        for r in recs[:5]:
            rr = _short_text(str(r), max_sentences=3, max_chars=400)
            if rr:
                lines.append(f"• {_esc(rr)}")
                lines.append("")

        while lines and lines[-1] == "":
            lines.pop()
        lines.append("")
        lines.append(DIVIDER_LIGHT)
        lines.append("")

    # Если вопросы врачу есть — ведём к ним живым переходом, а не сухим
    # дисклеймером: он примелькался и его перестают замечать.
    questions = step2_data.get("doctor_questions") or []
    if questions:
        lines.append("💭 <i>Это разбор состава, а не консультация: как отреагирует "
                     "именно твоя кожа, зависит от того, чего бот не знает.</i>")
        lines.append("")
        lines.append("👇 Собрала, что стоит спросить у Лизы Дубинской именно "
                     f"по этому составу — <b>{_plural_questions(len(questions[:3]))}</b>.")
        lines.append("")
        lines.append(DIVIDER_ACCENT)
    else:
        lines.append(build_doctor_cta_footer())

    return "\n".join(lines).strip() or "Не удалось сформировать пояснение."


def build_groups_message(data: Dict[str, Any], ingredients_data: Dict[str, Any]) -> str:
    """Экран выбора группы: сколько всего компонентов и что отмечено."""
    product_name = data.get("product_name") or "Продукт"
    ingredients = data.get("ingredients") or []
    groups = ingredients_data.get("groups") or []

    total = sum(len(g.get("items") or []) for g in groups)
    hard = sum(1 for ing in ingredients if ing.get("is_hard"))
    cond = sum(1 for ing in ingredients if ing.get("is_conditional"))

    lines = [
        DIVIDER_ACCENT,
        "",
        "<b>Что делает каждый компонент</b> 🧩",
        "",
        f"🧴 <b>{_esc(product_name)}</b>",
        f"🧾 {_plural_components(total)} — разложены по назначению",
        "",
        DIVIDER_LIGHT,
        "",
        "Выбери группу — внутри пояснение по каждому компоненту: что это, "
        "зачем он в формуле и о чём говорит его позиция в списке.",
        "",
    ]

    if hard or cond:
        marks = []
        if hard:
            marks.append(f"🔴 {hard} — выраженная комедогенность")
        if cond:
            marks.append(f"🟠 {cond} — условная")
        lines.append("Отмечено: " + ", ".join(marks) + ".")
        lines.append("")

    lines.append(DIVIDER_LIGHT)
    lines.append("")
    lines.append("💭 <i>Состав — это про средство. Про твою кожу ответит "
                 "Лиза Дубинская, дерматолог: @DrDubinsky</i>")

    return "\n".join(lines).strip()


def build_group_card_message(
    data: Dict[str, Any], group: Dict[str, Any]
) -> str:
    """Карточка одной группы: построчное пояснение по её компонентам."""
    product_name = data.get("product_name") or "Продукт"
    key = group.get("key") or ""
    items = group.get("items") or []

    lines = [
        DIVIDER_ACCENT,
        "",
        f"<b>{group_label(key)}</b>",
        "",
        f"🧴 <b>{_esc(product_name)}</b>",
        f"🧾 {_plural_components(len(items))} в этой группе",
        "",
        DIVIDER_LIGHT,
        "",
    ]

    for item in items:
        name = (item.get("name") or "").strip()
        if not name:
            continue
        typ = (item.get("type") or "").strip().lower()
        mark = _mark_for_component(typ == "hard", typ == "conditional")
        pos = item.get("position")
        pos_txt = f" · №{pos}" if isinstance(pos, int) else ""

        lines.append(f"{mark} <b>{_esc(name)}</b>{pos_txt}")

        note = _clean_text(item.get("note") or "")
        if note:
            lines.append(_esc(note))

        # Модель призналась, что надёжных данных мало. Показываем это честно:
        # признание полезнее правдоподобной выдумки, и это сам по себе повод
        # спросить у врача.
        if item.get("thin"):
            lines.append("🔸 <i>Надёжных данных по нему немного — додумывать не буду.</i>")

        ask = _clean_text(item.get("ask") or "")
        if ask:
            lines.append(f"🩺 <i>{_esc(ask)}</i>")

        lines.append("")

    while lines and lines[-1] == "":
        lines.pop()

    lines.append("")
    lines.append(DIVIDER_LIGHT)
    lines.append("")

    if group_questions(group):
        lines.append("🩺 <b>Отмеченное — то, что зависит от твоей кожи</b>, "
                     "а не от состава. Здесь я могу только назвать вопрос — "
                     "ответить на него может Лиза Дубинская на осмотре.")
    else:
        lines.append("💭 <i>Это состав, а не консультация: как отреагирует "
                     "именно твоя кожа, состав не показывает. Это к Лизе "
                     "Дубинской — она дерматолог.</i>")

    return "\n".join(lines).strip()


def group_questions(group: Dict[str, Any]) -> List[Dict[str, str]]:
    """Вопросы врачу, выросшие из компонентов одной группы."""
    out: List[Dict[str, str]] = []
    for item in group.get("items") or []:
        ask = _clean_text(item.get("ask") or "")
        name = (item.get("name") or "").strip()
        if ask and name:
            out.append({"name": name, "ask": ask})
    return out


def build_group_questions_message(
    data: Dict[str, Any], group: Dict[str, Any]
) -> str:
    """Экран вопросов врачу по одной группе компонентов."""
    product_name = data.get("product_name") or "Продукт"
    questions = group_questions(group)

    lines = [
        DIVIDER_ACCENT,
        "",
        "<b>Что спросить у врача</b> ❓",
        f"по группе «{group_label(group.get('key') or '')}»",
        "",
        f"🧴 <b>{_esc(product_name)}</b>",
        "",
        DIVIDER_LIGHT,
        "",
        "Про сами компоненты я рассказала всё, что известно. "
        "А дальше начинается то, что зависит от тебя — и вот об этом "
        "стоит спросить на приёме 👇",
        "",
    ]

    for item in questions:
        lines.append(f"🩺 <b>{_esc(item['name'])}</b>")
        lines.append(_esc(item["ask"]))
        lines.append("")

    while lines and lines[-1] == "":
        lines.pop()

    lines.extend([
        "",
        DIVIDER_LIGHT,
        "",
        "🩵 <b>Лиза Дубинская</b> — дерматолог, @DrDubinsky",
        "Можно написать напрямую и показать этот разбор.",
        "",
        DIVIDER_ACCENT,
    ])

    return "\n".join(lines).strip()


def build_doctor_questions_message(
    data: Dict[str, Any], questions: Sequence[str]
) -> str:
    """Экран с вопросами врачу — то, ради чего затевалась вся навигация."""
    product_name = data.get("product_name") or "Продукт"

    lines = [
        DIVIDER_ACCENT,
        "",
        "<b>Что спросить у врача</b> ❓",
        "",
        f"🧴 <b>{_esc(product_name)}</b>",
        "",
        DIVIDER_LIGHT,
        "",
        "Состав я разобрала. Но подойдёт ли средство <b>именно твоей коже</b> — "
        "зависит от того, чего я знать не могу: типа кожи и состояния барьера, "
        "остального ухода, назначенных препаратов, сезона и планов.",
        "",
        "Об этом стоит спросить живого врача. Лиза Дубинская — дерматолог, принимает и консультирует. Вот вопросы, собранные "
        "по этому конкретному составу 👇",
        "",
    ]

    numerals = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]
    for idx, q in enumerate(questions[:5]):
        qq = _clean_text(str(q))
        if not qq:
            continue
        lines.append(f"{numerals[idx]} {_esc(qq)}")
        lines.append("")

    while lines and lines[-1] == "":
        lines.pop()

    lines.extend([
        "",
        DIVIDER_LIGHT,
        "",
        "🩵 <b>Лиза Дубинская</b> — дерматолог, @DrDubinsky",
        "Можно написать напрямую и показать этот разбор: с ним на приёме "
        "будет предметнее, чем с фотографией баночки.",
        "",
        DIVIDER_ACCENT,
    ])

    return "\n".join(lines).strip()


# ─────────────────────────────────────────────────────────────
# Кэш готовых разборов (SQLite): скорость + экономия на API
# ─────────────────────────────────────────────────────────────

_PUNCT = re.compile(r"[^\w\s\-\+/&.]", flags=re.UNICODE)


def normalize_product_key(name: str) -> str:
    """Нормализуем название для ключа кэша: регистр, пробелы, пунктуация."""
    s = (name or "").strip().lower()
    s = _PUNCT.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def step1_cache_key_text(name: str) -> str:
    return "s1:name:" + normalize_product_key(name)


def step1_cache_key_images(images: Sequence[bytes], caption: Optional[str] = None) -> str:
    h = hashlib.sha256()
    for img in images:
        h.update(hashlib.sha256(img).digest())
    if caption:
        h.update(normalize_product_key(caption).encode("utf-8"))
    return "s1:img:" + h.hexdigest()[:32]


def step2_cache_key(step1_data: Dict[str, Any]) -> str:
    payload = build_step2_payload(step1_data)
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return "s2:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


# ─────────────────────────────────────────────────────────────
# Ограничение параллельных разборов
# ─────────────────────────────────────────────────────────────

_ACTIVE_USERS: Dict[int, float] = {}
_USER_HISTORY: Dict[int, List[float]] = {}


def _try_acquire(user_id: Optional[int]) -> Tuple[bool, str]:
    """Возвращает (можно ли начать, причина отказа)."""
    if not user_id:
        return True, ""

    now = time.time()

    if config.SINGLE_FLIGHT_PER_USER:
        started = _ACTIVE_USERS.get(user_id)
        # 5 минут — предохранитель на случай зависшей задачи
        if started and now - started < 300:
            return False, "busy"

    if config.RATE_LIMIT_PER_HOUR > 0:
        hist = [t for t in _USER_HISTORY.get(user_id, []) if now - t < 3600]
        if len(hist) >= config.RATE_LIMIT_PER_HOUR:
            _USER_HISTORY[user_id] = hist
            return False, "rate_limit"
        hist.append(now)
        _USER_HISTORY[user_id] = hist

    _ACTIVE_USERS[user_id] = now
    return True, ""


def _release(user_id: Optional[int]) -> None:
    if user_id:
        _ACTIVE_USERS.pop(user_id, None)


# ─────────────────────────────────────────────────────────────
# Handlers
# ─────────────────────────────────────────────────────────────

async def handle_start(msg: Message):
    await msg.answer(f"{START_MESSAGE}\n\n{build_doctor_cta_footer()}")
    await analytics.track(kind="start", user=msg.from_user, chat_id=msg.chat.id)


async def handle_help(msg: Message):
    await msg.answer(f"{HELP_MESSAGE}\n\n{build_doctor_cta_footer()}")
    await analytics.track(kind="help", user=msg.from_user, chat_id=msg.chat.id)


async def handle_about(msg: Message):
    await msg.answer(f"{ABOUT_MESSAGE}\n\n{build_doctor_cta_footer()}")
    await analytics.track(kind="about", user=msg.from_user, chat_id=msg.chat.id)


async def handle_contacts(msg: Message):
    await msg.answer(f"{CONTACTS_MESSAGE}\n\n{build_doctor_cta_footer()}")
    await analytics.track(kind="contacts", user=msg.from_user, chat_id=msg.chat.id)


async def _answer_from_step1(
    msg: Message,
    data: Dict[str, Any],
    *,
    status: Optional[Message] = None,
) -> None:
    """Отправляет краткий результат шага 1 и вешает кнопки."""
    answer = build_step1_brief_message(data)
    ingredients = data.get("ingredients") or []

    reply_markup: Optional[InlineKeyboardMarkup] = None
    if data.get("error") != "no_inci" and ingredients:
        token = _cache_put(
            {
                "product_name": data.get("product_name"),
                "risk_level": data.get("risk_level"),
                "source_url": data.get("source_url"),
                "ingredients": ingredients,
            }
        )
        reply_markup = _build_step1_keyboard(token)

    if status is not None:
        try:
            await status.delete()
        except Exception:  # noqa: BLE001
            pass

    await msg.answer(answer, reply_markup=reply_markup)


async def _run_step1_and_answer(
    msg: Message,
    bot: Bot,
    product_name: Optional[str],
    images: Optional[Sequence[bytes]] = None,
    *,
    status: Optional[Message] = None,
    source: str = "text",
    started_at: Optional[float] = None,
):
    images = list(images or [])
    user = msg.from_user
    user_id = getattr(user, "id", None)
    t0 = started_at or time.monotonic()

    if status is None:
        status = await msg.answer(PROCESSING_PHOTO if images else PROCESSING_TEXT)

    async def _drop_status() -> None:
        if status is not None:
            try:
                await status.delete()
            except Exception:  # noqa: BLE001
                pass

    try:
        # ── 1. Кэш: тот же продукт уже разбирали недавно?
        cache_key = (
            step1_cache_key_images(images, product_name)
            if images
            else step1_cache_key_text(product_name or "")
        )
        cached = await analytics.cache_get(cache_key)
        if cached:
            logger.info("Кэш-попадание: %s", cache_key)
            await _answer_from_step1(msg, cached, status=status)
            _warm_source_url(cached.get("source_url"), cached.get("ingredients") or [])
            await analytics.track(
                kind=source,
                user=user,
                chat_id=msg.chat.id,
                status="ok",
                source=source,
                product=cached.get("product_name"),
                risk=cached.get("risk_level"),
                latency_ms=int((time.monotonic() - t0) * 1000),
                cached=True,
            )
            return

        result = await run_agent_step1_ex(product_name=product_name, images=images)
        data = _parse_agent_json(result.raw)
        usage = result.usage

        if not data:
            await _drop_status()
            await msg.answer(ERROR_GENERAL)
            await analytics.track(
                kind=source, user=user, chat_id=msg.chat.id, status="error", source=source,
                product=product_name, latency_ms=int((time.monotonic() - t0) * 1000),
                model=usage.model, in_tokens=usage.in_tokens, cached_tokens=usage.cached_tokens,
                out_tokens=usage.out_tokens, tool_calls=usage.tool_calls,
                detail="модель вернула не-JSON",
            )
            return

        ingredients = data.get("ingredients") or []
        if ingredients and data.get("error") != "no_inci":
            data["risk_level"] = calc_risk_level_strict(ingredients)

        # ── Нормализуем source_url. Проверку доступности НЕ ждём:
        #    она уходит в фон и понадобится только на экране «Состав».
        if data.get("error") != "no_inci":
            data["source_url"] = _normalize_source_url(data.get("source_url")) if ingredients else None
        _warm_source_url(data.get("source_url"), ingredients)

        await _answer_from_step1(msg, data, status=status)

        # ── Кладём в кэш только удачные разборы
        if data.get("error") != "no_inci" and ingredients:
            payload = {
                "product_name": data.get("product_name"),
                "risk_level": data.get("risk_level"),
                "source_url": data.get("source_url"),
                "ingredients": ingredients,
            }
            await analytics.cache_put(cache_key, "step1", payload)
            # Фото → запоминаем и под распознанным названием,
            # чтобы текстовый запрос того же продукта тоже попал в кэш.
            recognized = data.get("product_name")
            if images and recognized:
                await analytics.cache_put(step1_cache_key_text(recognized), "step1", payload)

        await analytics.track(
            kind=source,
            user=user,
            chat_id=msg.chat.id,
            status="no_inci" if data.get("error") == "no_inci" else "ok",
            source=source,
            product=data.get("product_name") or product_name,
            risk=data.get("risk_level"),
            latency_ms=int((time.monotonic() - t0) * 1000),
            model=usage.model,
            in_tokens=usage.in_tokens,
            cached_tokens=usage.cached_tokens,
            out_tokens=usage.out_tokens,
            tool_calls=usage.tool_calls,
        )

    except Exception as e:
        logger.error("STEP1 ERROR: %s", e, exc_info=True)
        await _drop_status()
        await msg.answer(ERROR_GENERAL)
        await analytics.track(
            kind=source, user=user, chat_id=msg.chat.id, status="error", source=source,
            product=product_name, latency_ms=int((time.monotonic() - t0) * 1000),
            detail=f"{type(e).__name__}: {e}",
        )
    finally:
        _release(user_id)


# ─────────────────────────────────────────────────────────────
# Фото: одиночное и альбомом
# ─────────────────────────────────────────────────────────────

# media_group_id → накопленные фото одного альбома
_ALBUM_BUFFER: Dict[str, List[PhotoSize]] = {}


async def _collect_images(bot: Bot, photos: Sequence[PhotoSize]) -> List[bytes]:
    """Скачиваем фото параллельно — быстрее, чем по очереди."""
    tasks = [_download_photo(bot, p) for p in photos[: config.ALBUM_MAX_PHOTOS]]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    images: List[bytes] = []
    for r in results:
        if isinstance(r, Exception):
            logger.error("PHOTO DOWNLOAD ERROR: %s", r)
            continue
        images.append(_maybe_shrink(r))
    return images


async def _process_album(bot: Bot, msg: Message, group_id: str, caption: Optional[str]) -> None:
    """Ждём остальные фото альбома и отправляем их ОДНИМ запросом в модель."""
    t0 = time.monotonic()
    status = await msg.answer(PROCESSING_PHOTO)
    await asyncio.sleep(config.ALBUM_WAIT_SEC)

    photos = _ALBUM_BUFFER.pop(group_id, [])
    if not photos:
        try:
            await status.delete()
        except Exception:  # noqa: BLE001
            pass
        _release(getattr(msg.from_user, "id", None))
        return

    images = await _collect_images(bot, photos)
    if not images:
        try:
            await status.delete()
        except Exception:  # noqa: BLE001
            pass
        await msg.answer(ERROR_GENERAL)
        _release(getattr(msg.from_user, "id", None))
        return

    await _run_step1_and_answer(
        msg, bot,
        product_name=caption,
        images=images,
        status=status,
        source="album" if len(images) > 1 else "photo",
        started_at=t0,
    )


async def handle_photo(msg: Message, bot: Bot):
    if not msg.photo:
        return await msg.answer(ERROR_EMPTY)

    user_id = getattr(msg.from_user, "id", None)
    caption = (msg.caption or "").strip() or None
    group_id = msg.media_group_id if config.ALBUM_ENABLED else None

    # ── Альбом: фото приходят отдельными апдейтами и обрабатываются параллельно,
    #    поэтому «занимаем» группу синхронно, без единого await между проверкой и записью.
    if group_id:
        if group_id in _ALBUM_BUFFER:
            _ALBUM_BUFFER[group_id].append(msg.photo[-1])
            return
        _ALBUM_BUFFER[group_id] = [msg.photo[-1]]

        allowed, reason = _try_acquire(user_id)
        if not allowed:
            _ALBUM_BUFFER.pop(group_id, None)
            await msg.answer(BUSY_MESSAGE)
            await analytics.track(
                kind="blocked", user=msg.from_user, chat_id=msg.chat.id,
                status=reason, source="album",
            )
            return

        asyncio.create_task(_process_album(bot, msg, group_id, caption))
        return

    allowed, reason = _try_acquire(user_id)
    if not allowed:
        await msg.answer(BUSY_MESSAGE)
        await analytics.track(
            kind="blocked", user=msg.from_user, chat_id=msg.chat.id,
            status=reason, source="photo",
        )
        return

    t0 = time.monotonic()
    # Плашку «Анализирую фото» показываем СРАЗУ, параллельно со скачиванием —
    # человек видит реакцию бота на ~секунду раньше.
    status_task = asyncio.create_task(msg.answer(PROCESSING_PHOTO))
    try:
        images = await _collect_images(bot, [msg.photo[-1]])
    except Exception as e:  # noqa: BLE001
        logger.error("PHOTO DOWNLOAD ERROR: %s", e)
        images = []

    try:
        status = await status_task
    except Exception:  # noqa: BLE001
        status = None

    if not images:
        if status is not None:
            try:
                await status.delete()
            except Exception:  # noqa: BLE001
                pass
        _release(user_id)
        await analytics.track(
            kind="photo", user=msg.from_user, chat_id=msg.chat.id,
            status="error", source="photo", detail="не удалось скачать фото",
        )
        return await msg.answer(ERROR_GENERAL)

    await _run_step1_and_answer(
        msg, bot, product_name=caption, images=images,
        status=status, source="photo", started_at=t0,
    )


async def handle_text(msg: Message, bot: Bot):
    text = (msg.text or "").strip()
    if not text:
        return await msg.answer(ERROR_EMPTY)

    if text.startswith("/"):
        return

    if len(text) > config.MAX_QUERY_LEN:
        await msg.answer(TOO_LONG_MESSAGE)
        await analytics.track(
            kind="blocked", user=msg.from_user, chat_id=msg.chat.id,
            status="too_long", source="text", detail=f"{len(text)} символов",
        )
        return

    user_id = getattr(msg.from_user, "id", None)
    allowed, reason = _try_acquire(user_id)
    if not allowed:
        await msg.answer(BUSY_MESSAGE)
        await analytics.track(
            kind="blocked", user=msg.from_user, chat_id=msg.chat.id,
            status=reason, source="text",
        )
        return

    await _run_step1_and_answer(msg, bot, product_name=text, images=None, source="text")


# ─────────────────────────────────────────────────────────────
# Кнопки
# ─────────────────────────────────────────────────────────────

async def handle_composition_callback(cb: CallbackQuery):
    payload = cb.data or ""
    if not payload.startswith("composition:"):
        return

    token = payload.split(":", 1)[1]
    step1_data = _cache_get(token)
    if not step1_data or cb.message is None:
        await cb.answer("Эта кнопка уже неактуальна. Отправь запрос заново.", show_alert=True)
        return

    await cb.answer()

    t0 = time.monotonic()
    # Ссылку-источник проверяем здесь (обычно уже прогрета в фоне и это мгновенно)
    data = dict(step1_data)
    url = data.get("source_url")
    if url:
        ok = await _source_url_ok(url, data.get("ingredients") or [])
        data["source_url"] = url if ok else None

    await cb.message.answer(build_composition_message(data), reply_markup=_build_step1_keyboard(token))
    await analytics.track(
        kind="composition",
        user=cb.from_user,
        chat_id=cb.message.chat.id,
        product=data.get("product_name"),
        risk=data.get("risk_level"),
        latency_ms=int((time.monotonic() - t0) * 1000),
    )


async def _run_step2_background(
    bot: Bot,
    chat_id: int,
    step1_data: Dict[str, Any],
    token: str,
    user: Any = None,
) -> None:
    t0 = time.monotonic()
    key = step2_cache_key(step1_data)

    async def _deliver(step2_data: Dict[str, Any]) -> None:
        """Отправляет шаг 2 и оставляет разбор доступным для кнопок."""
        _slot_put(token, "step2", step2_data)
        questions = step2_data.get("doctor_questions") or []
        await bot.send_message(
            chat_id,
            build_step2_message(
                step2_data,
                product_name=step1_data.get("product_name"),
                risk_level=step1_data.get("risk_level"),
            ),
            reply_markup=_build_step2_keyboard(token, has_questions=bool(questions)),
        )

    try:
        cached = await analytics.cache_get(key)
        if cached:
            await _deliver(cached)
            await analytics.track(
                kind="step2", user=user, chat_id=chat_id, cached=True,
                product=step1_data.get("product_name"), risk=step1_data.get("risk_level"),
                latency_ms=int((time.monotonic() - t0) * 1000),
            )
            return

        result = await run_agent_step2_ex(step1_data)
        step2_json = _parse_agent_json(result.raw)
        usage = result.usage

        if not step2_json:
            await bot.send_message(chat_id, "Не удалось сформировать пояснение. Попробуй ещё раз.")
            await analytics.track(
                kind="step2", user=user, chat_id=chat_id, status="error",
                product=step1_data.get("product_name"),
                latency_ms=int((time.monotonic() - t0) * 1000),
                model=usage.model, in_tokens=usage.in_tokens, cached_tokens=usage.cached_tokens,
                out_tokens=usage.out_tokens, tool_calls=usage.tool_calls,
                detail="шаг 2 вернул не-JSON",
            )
            return

        await _deliver(step2_json)
        await analytics.cache_put(key, "step2", step2_json)
        await analytics.track(
            kind="step2", user=user, chat_id=chat_id,
            product=step1_data.get("product_name"), risk=step1_data.get("risk_level"),
            latency_ms=int((time.monotonic() - t0) * 1000),
            model=usage.model, in_tokens=usage.in_tokens, cached_tokens=usage.cached_tokens,
            out_tokens=usage.out_tokens, tool_calls=usage.tool_calls,
        )
    except Exception as e:
        logger.error("STEP2 BACKGROUND ERROR: %s", e, exc_info=True)
        try:
            await bot.send_message(chat_id, "Не удалось сформировать пояснение. Попробуй ещё раз.")
        except Exception:
            pass
        await analytics.track(
            kind="step2", user=user, chat_id=chat_id, status="error",
            product=step1_data.get("product_name"),
            latency_ms=int((time.monotonic() - t0) * 1000),
            detail=f"{type(e).__name__}: {e}",
        )
    finally:
        # Раньше здесь удалялся весь токен: шаг 2 был последним экраном.
        # Теперь с него уходят дальше — в разбор состава и к вопросам врачу,
        # поэтому снимаем только замок «уже считаю», а разбор оставляем жить.
        STEP2_INFLIGHT.pop(token, None)


async def handle_step2_callback(cb: CallbackQuery, bot: Bot):
    payload = cb.data or ""
    if not payload.startswith("step2:"):
        return

    token = payload.split(":", 1)[1]
    step1_data = _cache_get(token)
    if not step1_data or cb.message is None:
        await cb.answer("Эта кнопка уже неактуальна. Отправь запрос заново.", show_alert=True)
        return

    if token in STEP2_INFLIGHT:
        await cb.answer("Уже формирую ответ.", show_alert=False)
        return

    STEP2_INFLIGHT[token] = time.time()

    await cb.answer()
    await cb.message.answer(PROCESSING_STEP2)

    chat_id = cb.message.chat.id
    asyncio.create_task(_run_step2_background(bot, chat_id, step1_data, token, cb.from_user))


# ─────────────────────────────────────────────────────────────
# Разбор состава по группам
# ─────────────────────────────────────────────────────────────

STALE_BUTTON = "Эта кнопка уже неактуальна. Отправь запрос заново."


def _doctor_questions(token: str) -> List[str]:
    step2 = _slot_get(token, "step2") or {}
    return [q for q in (step2.get("doctor_questions") or []) if str(q).strip()]


def ingredients_cache_key(step1_data: Dict[str, Any]) -> str:
    payload = build_ingredients_payload(step1_data)
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return "ing:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


async def _load_ingredients(
    step1_data: Dict[str, Any], token: str, user: Any = None, chat_id: Optional[int] = None
) -> Optional[Dict[str, Any]]:
    """Разбор состава: сначала память, потом SQLite, и только затем модель."""
    ready = _slot_get(token, "ingredients")
    if ready:
        return ready

    t0 = time.monotonic()
    key = ingredients_cache_key(step1_data)

    cached = await analytics.cache_get(key)
    if cached:
        _slot_put(token, "ingredients", cached)
        await analytics.track(
            kind="ingredients", user=user, chat_id=chat_id, cached=True,
            product=step1_data.get("product_name"), risk=step1_data.get("risk_level"),
            latency_ms=int((time.monotonic() - t0) * 1000),
        )
        return cached

    result = await run_agent_ingredients_ex(step1_data)
    data = _parse_agent_json(result.raw)
    usage = result.usage

    if not data or not data.get("groups"):
        await analytics.track(
            kind="ingredients", user=user, chat_id=chat_id, status="error",
            product=step1_data.get("product_name"),
            latency_ms=int((time.monotonic() - t0) * 1000),
            model=usage.model, in_tokens=usage.in_tokens, cached_tokens=usage.cached_tokens,
            out_tokens=usage.out_tokens, tool_calls=usage.tool_calls,
            detail="разбор состава вернул пусто",
        )
        return None

    _slot_put(token, "ingredients", data)
    await analytics.cache_put(key, "ingredients", data)
    await analytics.track(
        kind="ingredients", user=user, chat_id=chat_id,
        product=step1_data.get("product_name"), risk=step1_data.get("risk_level"),
        latency_ms=int((time.monotonic() - t0) * 1000),
        model=usage.model, in_tokens=usage.in_tokens, cached_tokens=usage.cached_tokens,
        out_tokens=usage.out_tokens, tool_calls=usage.tool_calls,
    )
    return data


async def handle_groups_callback(cb: CallbackQuery):
    payload = cb.data or ""
    if not payload.startswith("groups:"):
        return

    token = payload.split(":", 1)[1]
    step1_data = _cache_get(token)
    if not step1_data or cb.message is None:
        await cb.answer(STALE_BUTTON, show_alert=True)
        return

    if not config.INGREDIENTS_ENABLED:
        await cb.answer("Разбор состава сейчас недоступен.", show_alert=True)
        return

    ready = _slot_get(token, "ingredients")
    if not ready:
        if token in INGREDIENTS_INFLIGHT:
            await cb.answer("Уже разбираю состав.", show_alert=False)
            return
        INGREDIENTS_INFLIGHT[token] = time.time()
        await cb.answer()
        await cb.message.answer(PROCESSING_INGREDIENTS)
    else:
        await cb.answer()

    try:
        data = await _load_ingredients(
            step1_data, token, user=cb.from_user, chat_id=cb.message.chat.id
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("INGREDIENTS ERROR: %s", exc, exc_info=True)
        data = None
    finally:
        INGREDIENTS_INFLIGHT.pop(token, None)

    if not data:
        await cb.message.answer(ERROR_INGREDIENTS)
        return

    await cb.message.answer(
        build_groups_message(step1_data, data),
        reply_markup=_build_groups_keyboard(
            token, data.get("groups") or [], has_questions=bool(_doctor_questions(token))
        ),
    )


async def handle_group_card_callback(cb: CallbackQuery):
    payload = cb.data or ""
    if not payload.startswith("grp:"):
        return

    parts = payload.split(":", 2)
    if len(parts) < 3:
        await cb.answer(STALE_BUTTON, show_alert=True)
        return

    _, token, group_key = parts
    step1_data = _cache_get(token)
    data = _slot_get(token, "ingredients")
    if not step1_data or not data or cb.message is None:
        await cb.answer(STALE_BUTTON, show_alert=True)
        return

    group = next(
        (g for g in (data.get("groups") or []) if g.get("key") == group_key), None
    )
    if not group:
        await cb.answer("Такой группы в этом составе нет.", show_alert=True)
        return

    await cb.answer()
    await _answer_long(
        cb.message,
        build_group_card_message(step1_data, group),
        reply_markup=_build_group_card_keyboard(
            token, group, has_questions=bool(_doctor_questions(token))
        ),
    )
    await analytics.track(
        kind="group", user=cb.from_user, chat_id=cb.message.chat.id,
        product=step1_data.get("product_name"), detail=group_key,
    )


async def handle_group_questions_callback(cb: CallbackQuery):
    """Вопросы врачу по одной группе — из пометок ask у её компонентов."""
    payload = cb.data or ""
    if not payload.startswith("gq:"):
        return

    parts = payload.split(":", 2)
    if len(parts) < 3:
        await cb.answer(STALE_BUTTON, show_alert=True)
        return

    _, token, group_key = parts
    step1_data = _cache_get(token)
    data = _slot_get(token, "ingredients")
    if not step1_data or not data or cb.message is None:
        await cb.answer(STALE_BUTTON, show_alert=True)
        return

    group = next(
        (g for g in (data.get("groups") or []) if g.get("key") == group_key), None
    )
    if not group or not group_questions(group):
        await cb.answer("По этой группе вопросов не набралось.", show_alert=True)
        return

    await cb.answer()
    await _answer_long(
        cb.message,
        build_group_questions_message(step1_data, group),
        reply_markup=_build_doctor_keyboard(token),
    )
    await analytics.track(
        kind="doctor", user=cb.from_user, chat_id=cb.message.chat.id,
        product=step1_data.get("product_name"), detail=f"group:{group_key}",
    )


async def handle_doctor_callback(cb: CallbackQuery):
    payload = cb.data or ""
    if not payload.startswith("doc:"):
        return

    token = payload.split(":", 1)[1]
    step1_data = _cache_get(token)
    questions = _doctor_questions(token)
    if not step1_data or not questions or cb.message is None:
        await cb.answer(STALE_BUTTON, show_alert=True)
        return

    await cb.answer()
    await _answer_long(
        cb.message,
        build_doctor_questions_message(step1_data, questions),
        reply_markup=_build_doctor_keyboard(token),
    )
    await analytics.track(
        kind="doctor", user=cb.from_user, chat_id=cb.message.chat.id,
        product=step1_data.get("product_name"), risk=step1_data.get("risk_level"),
    )


# ─────────────────────────────────────────────────────────────
# Фоновая уборка
# ─────────────────────────────────────────────────────────────

async def _housekeeping() -> None:
    while True:
        try:
            await asyncio.sleep(3600)
            _sweep_memory_caches()
            await analytics.maintenance()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("Уборка не удалась: %s", exc)


# ─────────────────────────────────────────────────────────────
# Run
# ─────────────────────────────────────────────────────────────

async def _main_async():
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

    analytics.init()

    bot = Bot(
        token=TELEGRAM_BOT_TOKEN,
        default=DefaultBotProperties(parse_mode="HTML"),
    )

    dp = Dispatcher()

    dp.message.register(handle_start, CommandStart())
    dp.message.register(handle_help, Command("help"))
    dp.message.register(handle_about, Command("about"))
    dp.message.register(handle_contacts, Command("contacts"))

    # Скрытые админ-команды — ДО общего текстового хендлера
    admin.register(dp)

    dp.message.register(handle_photo, F.photo)
    dp.message.register(handle_text, F.text)

    dp.callback_query.register(handle_composition_callback, F.data.startswith("composition:"))
    dp.callback_query.register(handle_step2_callback, F.data.startswith("step2:"))
    dp.callback_query.register(handle_groups_callback, F.data.startswith("groups:"))
    dp.callback_query.register(handle_group_card_callback, F.data.startswith("grp:"))
    dp.callback_query.register(handle_group_questions_callback, F.data.startswith("gq:"))
    dp.callback_query.register(handle_doctor_callback, F.data.startswith("doc:"))

    logger.info("CreamcheckBot started (FINAL BALANCED UX)")

    await dashboard.start()
    housekeeper = asyncio.create_task(_housekeeping())

    try:
        await dp.start_polling(bot)
    finally:
        housekeeper.cancel()
        await dashboard.stop()
        if _http is not None and not _http.is_closed:
            await _http.aclose()
        try:
            from agent.agent import aclose as agent_close

            await agent_close()
        except Exception:  # noqa: BLE001
            pass
        await bot.session.close()
        analytics.close()


def main():
    try:
        asyncio.run(_main_async())
    except (KeyboardInterrupt, SystemExit):
        logger.info("CreamcheckBot остановлен")


if __name__ == "__main__":
    main()
