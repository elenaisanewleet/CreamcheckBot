"""Configuration for CreamcheckBot.

Все новые настройки читаются из окружения и имеют безопасные значения по умолчанию:
если ничего не задавать, бот работает ровно так же, как до оптимизации.
"""

from __future__ import annotations

import os
from typing import Optional, Set

from dotenv import load_dotenv

# Загружаем переменные из .env, если файл есть рядом с проектом
load_dotenv()


# ─────────────────────────────────────────────────────────────
# Хелперы чтения env
# ─────────────────────────────────────────────────────────────

_TRUE = {"1", "true", "yes", "y", "on", "да"}
_FALSE = {"0", "false", "no", "n", "off", "нет"}


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    raw = raw.strip().lower()
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    return default


def env_int(name: str, default: int) -> int:
    try:
        return int(str(os.environ.get(name, default)).strip())
    except (TypeError, ValueError):
        return default


def env_float(name: str, default: float) -> float:
    try:
        return float(str(os.environ.get(name, default)).strip().replace(",", "."))
    except (TypeError, ValueError):
        return default


def env_str(name: str, default: str = "") -> str:
    raw = os.environ.get(name)
    return raw.strip() if isinstance(raw, str) and raw.strip() else default


def env_ids(name: str, default: Set[int]) -> Set[int]:
    raw = os.environ.get(name)
    if not raw:
        return set(default)
    out: Set[int] = set()
    for chunk in raw.replace(";", ",").replace(" ", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            out.add(int(chunk))
        except ValueError:
            continue
    return out or set(default)


# ─────────────────────────────────────────────────────────────
# Обязательное
# ─────────────────────────────────────────────────────────────

TELEGRAM_BOT_TOKEN: Optional[str] = os.environ.get("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY: Optional[str] = os.environ.get("OPENAI_API_KEY")


# ─────────────────────────────────────────────────────────────
# Админы (скрытые команды + доступ к дашборду)
#   41082373 — Лена (@elenaisanewleet), 228174255 — Лиза (@DrDubinsky)
# ─────────────────────────────────────────────────────────────

DEFAULT_ADMIN_IDS = {41082373, 228174255}
ADMIN_IDS: Set[int] = env_ids("ADMIN_IDS", DEFAULT_ADMIN_IDS)


# ─────────────────────────────────────────────────────────────
# Аналитика / логи
# ─────────────────────────────────────────────────────────────

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

ANALYTICS_ENABLED: bool = env_bool("ANALYTICS_ENABLED", True)
ANALYTICS_DB: str = env_str("ANALYTICS_DB", os.path.join(BASE_DIR, "data", "analytics.db"))
# Сколько дней хранить сырые события (0 = хранить всё)
ANALYTICS_RETENTION_DAYS: int = env_int("ANALYTICS_RETENTION_DAYS", 180)

LOG_LEVEL: str = env_str("LOG_LEVEL", "INFO").upper()
LOG_FILE: str = env_str("LOG_FILE", os.path.join(BASE_DIR, "data", "bot.log"))
LOG_FILE_MAX_BYTES: int = env_int("LOG_FILE_MAX_BYTES", 5 * 1024 * 1024)
LOG_FILE_BACKUPS: int = env_int("LOG_FILE_BACKUPS", 3)
# Сколько последних строк лога держать в памяти для /logs и дашборда
LOG_RING_SIZE: int = env_int("LOG_RING_SIZE", 800)


# ─────────────────────────────────────────────────────────────
# Веб-дашборд (тот же порт, что и health-сервер)
# ─────────────────────────────────────────────────────────────

RENDER_PORT: int = env_int("PORT", 10000)
DASHBOARD_ENABLED: bool = env_bool("DASHBOARD_ENABLED", True)
# Если не задан — генерируется случайный при старте и пишется в лог.
DASHBOARD_TOKEN: str = env_str("DASHBOARD_TOKEN", "")
# Публичный адрес, чтобы бот мог прислать рабочую ссылку в /dash
PUBLIC_BASE_URL: str = env_str("PUBLIC_BASE_URL", "").rstrip("/")


# ─────────────────────────────────────────────────────────────
# Кэш результатов (ускорение + экономия на API)
# ─────────────────────────────────────────────────────────────

CACHE_ENABLED: bool = env_bool("CACHE_ENABLED", True)
# Сколько живёт готовый разбор состава (сутки * N)
CACHE_TTL_DAYS: int = env_int("CACHE_TTL_DAYS", 30)
# Кэш проверки source_url — короче, ссылки протухают быстрее
URL_CACHE_TTL_HOURS: int = env_int("URL_CACHE_TTL_HOURS", 72)


# ─────────────────────────────────────────────────────────────
# Скорость / поведение анализа
# ─────────────────────────────────────────────────────────────

OPENAI_MODEL: str = env_str("OPENAI_MODEL", "gpt-5.2")
# Ретраи SDK: 0 — как было раньше; 1 — молча переживаем разовый сбой сети.
OPENAI_MAX_RETRIES: int = env_int("OPENAI_MAX_RETRIES", 1)
OPENAI_TIMEOUT_SEC: float = env_float("OPENAI_TIMEOUT_SEC", 300.0)
OPENAI_CONNECT_TIMEOUT_SEC: float = env_float("OPENAI_CONNECT_TIMEOUT_SEC", 20.0)

STEP1_MAX_TOOL_CALLS: int = env_int("STEP1_MAX_TOOL_CALLS", 10)
STEP1_MAX_OUTPUT_TOKENS: int = env_int("STEP1_MAX_OUTPUT_TOKENS", 2500)
# "", "minimal", "low", "medium", "high". Пусто = дефолт модели (как было).
STEP1_REASONING_EFFORT: str = env_str("STEP1_REASONING_EFFORT", "").lower()

STEP2_WEB_SEARCH: bool = env_bool("STEP2_WEB_SEARCH", True)
STEP2_MAX_TOOL_CALLS: int = env_int("STEP2_MAX_TOOL_CALLS", 3)
STEP2_MAX_OUTPUT_TOKENS: int = env_int("STEP2_MAX_OUTPUT_TOKENS", 1200)
STEP2_REASONING_EFFORT: str = env_str("STEP2_REASONING_EFFORT", "").lower()

# Ключ prompt-кэша OpenAI: одинаковый префикс промпта переиспользуется дешевле.
PROMPT_CACHE_KEY: str = env_str("PROMPT_CACHE_KEY", "creamcheck")

# Сжатие фото перед отправкой в модель. 0 = выключено (по умолчанию),
# чтобы не терять качество OCR мелкого шрифта состава.
IMAGE_MAX_SIDE: int = env_int("IMAGE_MAX_SIDE", 0)
IMAGE_JPEG_QUALITY: int = env_int("IMAGE_JPEG_QUALITY", 88)

# Собирать альбом (несколько фото одним сообщением) в один запрос к модели.
ALBUM_ENABLED: bool = env_bool("ALBUM_ENABLED", True)
ALBUM_WAIT_SEC: float = env_float("ALBUM_WAIT_SEC", 1.5)
ALBUM_MAX_PHOTOS: int = env_int("ALBUM_MAX_PHOTOS", 4)

# Один тяжёлый анализ на пользователя одновременно.
SINGLE_FLIGHT_PER_USER: bool = env_bool("SINGLE_FLIGHT_PER_USER", True)
# Мягкий лимит: сколько анализов в час на одного пользователя (0 = без лимита)
RATE_LIMIT_PER_HOUR: int = env_int("RATE_LIMIT_PER_HOUR", 0)

# Максимальная длина текстового запроса (защита от «простыней» в промпте)
MAX_QUERY_LEN: int = env_int("MAX_QUERY_LEN", 300)


# ─────────────────────────────────────────────────────────────
# Совпадения по базе комедогенов.
#
# ВНИМАНИЕ: этот флаг МЕНЯЕТ РЕЗУЛЬТАТ для пользователей. По умолчанию false —
# бот считает риск ровно так же, как считал раньше.
#
# При true исправляются два расхождения между промптом и кодом:
#   1) «wax» ищется внутри слова → Beeswax / Synthetic Beeswax становятся
#      жёсткими комедогенами (промпт прямо называет beeswax примером,
#      но текущий код ловит только отдельное слово: «Candelilla Wax» — да,
#      «Beeswax» — нет);
#   2) «silicone» ищется как часть слова → Polysilicone-11 становится
#      условным комедогеном (тоже пример из промпта). Silica при этом
#      по-прежнему НЕ отмечается.
#
# Включать только после проверки на реальных составах вместе с Лизой:
# оба изменения повышают уровень риска у части продуктов.
# ─────────────────────────────────────────────────────────────

COMEDOGEN_MATCH_V2: bool = env_bool("COMEDOGEN_MATCH_V2", False)


# ─────────────────────────────────────────────────────────────
# Цены для оценки расходов (USD за 1M токенов).
# Значения на август 2026 для gpt-5.2; переопредели через .env при смене модели.
# ─────────────────────────────────────────────────────────────

PRICE_IN_PER_1M: float = env_float("PRICE_IN_PER_1M", 1.75)
PRICE_CACHED_IN_PER_1M: float = env_float("PRICE_CACHED_IN_PER_1M", 0.175)
PRICE_OUT_PER_1M: float = env_float("PRICE_OUT_PER_1M", 14.0)
# Веб-поиск тарифицируется отдельно, за вызов инструмента (USD за 1000 вызовов).
PRICE_WEB_SEARCH_PER_1K: float = env_float("PRICE_WEB_SEARCH_PER_1K", 10.0)
