# agent/agent.py
"""ComedoBot agent implementation (2 steps) + STRICT comedogen flags by code.

Логика классификации ингредиентов и расчёта флагов НЕ менялась — она детерминированная
и остаётся источником истины. Изменения касаются только транспорта:
  • несколько фото одним запросом (лицевая сторона + состав)
  • телеметрия (токены, вызовы веб-поиска, время) для аналитики
  • устойчивость к смене поддерживаемых параметров API
  • настраиваемые через .env скорость/стоимость (по умолчанию — как было)
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

import httpx
import openai as openai_pkg
from dotenv import load_dotenv
from openai import AsyncOpenAI

from bot import config

from .comedogen_base import hard_comedogens, conditional_comedogens

load_dotenv()

logger = logging.getLogger(__name__)
logger.info("Using openai package version: %s", getattr(openai_pkg, "__version__", "unknown"))

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

PROMPT_STEP1_PATH = os.path.join(BASE_DIR, "prompt_system.txt")
PROMPT_STEP2_PATH = os.path.join(BASE_DIR, "prompt_system_step2.txt")


def _read_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError as exc:
        raise RuntimeError(f"Prompt file not found at {path!r}.") from exc


SYSTEM_PROMPT_STEP1 = _read_text(PROMPT_STEP1_PATH)
SYSTEM_PROMPT_STEP2 = _read_text(PROMPT_STEP2_PATH)

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY is not set")

MODEL = config.OPENAI_MODEL

# Важно: большой общий таймаут клиента (Step2 может быть долгим).
# max_retries=1 — один тихий ретрай на сетевой сбой вместо ошибки пользователю.
client = AsyncOpenAI(
    api_key=OPENAI_API_KEY,
    max_retries=config.OPENAI_MAX_RETRIES,
    timeout=httpx.Timeout(config.OPENAI_TIMEOUT_SEC, connect=config.OPENAI_CONNECT_TIMEOUT_SEC),
)


# ─────────────────────────────────────────────
# Телеметрия одного вызова модели
# ─────────────────────────────────────────────

@dataclass
class Usage:
    """Что стоил и сколько занял вызов модели (для аналитики)."""

    model: str = MODEL
    in_tokens: int = 0
    cached_tokens: int = 0
    out_tokens: int = 0
    tool_calls: int = 0
    api_ms: int = 0
    calls: int = 0

    def merge(self, other: "Usage") -> "Usage":
        self.in_tokens += other.in_tokens
        self.cached_tokens += other.cached_tokens
        self.out_tokens += other.out_tokens
        self.tool_calls += other.tool_calls
        self.api_ms += other.api_ms
        self.calls += other.calls
        return self


@dataclass
class AgentResult:
    """Ответ агента + телеметрия."""

    raw: str
    usage: Usage = field(default_factory=Usage)


def _extract_usage(resp: Any, elapsed_ms: int) -> Usage:
    """Достаёт токены/вызовы инструментов из ответа Responses API (мягко)."""
    usage = Usage(model=MODEL, api_ms=elapsed_ms, calls=1)
    try:
        u = getattr(resp, "usage", None)
        if u is not None:
            usage.in_tokens = int(getattr(u, "input_tokens", 0) or 0)
            usage.out_tokens = int(getattr(u, "output_tokens", 0) or 0)
            details = getattr(u, "input_tokens_details", None)
            if details is not None:
                usage.cached_tokens = int(getattr(details, "cached_tokens", 0) or 0)
            elif isinstance(u, dict):  # на случай другого формата SDK
                usage.cached_tokens = int(
                    (u.get("input_tokens_details") or {}).get("cached_tokens", 0) or 0
                )
    except Exception as exc:  # noqa: BLE001 — телеметрия не должна ломать ответ
        logger.debug("usage parse failed: %s", exc)

    try:
        for item in getattr(resp, "output", None) or []:
            if str(getattr(item, "type", "")).endswith("search_call"):
                usage.tool_calls += 1
    except Exception:  # noqa: BLE001
        pass

    return usage


# ─────────────────────────────────────────────
# Устойчивый вызов Responses API
# ─────────────────────────────────────────────

# Необязательные параметры, которые можно безболезненно выбросить, если модель
# перестанет их принимать (OpenAI периодически меняет поддержку у gpt-5.x).
_DROPPABLE = (
    "temperature",
    "reasoning",
    "prompt_cache_key",
    "max_tool_calls",
    "max_output_tokens",
    "text",
    "tools",
    "store",
)

# Параметры, которые API уже отверг в этом процессе — больше не отправляем.
_DISABLED_PARAMS: set[str] = set()

_PARAM_IN_ERROR = re.compile(r"['\"]([a-z_]+)['\"]", flags=re.I)


def _guess_bad_param(message: str, sent: Dict[str, Any]) -> Optional[str]:
    low = message.lower()
    if not any(w in low for w in ("unsupported", "unknown", "not supported", "unrecognized", "invalid")):
        return None
    for name in _PARAM_IN_ERROR.findall(message):
        base = name.split(".")[0].lower()
        if base in _DROPPABLE and base in sent:
            return base
    for name in _DROPPABLE:
        if name in sent and f"'{name}'" in low:
            return name
    return None


async def _responses_create(**kwargs: Any) -> Any:
    """client.responses.create с автоснятием параметров, которые API не принял.

    Раньше любой такой отказ означал ошибку пользователю. Теперь бот один раз
    учится, что параметр не поддерживается, и продолжает работать.
    """
    payload = {k: v for k, v in kwargs.items() if k not in _DISABLED_PARAMS and v is not None}

    for _ in range(len(_DROPPABLE) + 1):
        started = time.monotonic()
        try:
            resp = await client.responses.create(**payload)
            return resp, _extract_usage(resp, int((time.monotonic() - started) * 1000))
        except TypeError as exc:
            bad = _guess_bad_param(str(exc), payload)
            if not bad:
                raise
            logger.warning("Параметр %r не поддерживается SDK — отключаю его", bad)
            _DISABLED_PARAMS.add(bad)
            payload.pop(bad, None)
        except Exception as exc:  # noqa: BLE001
            status = getattr(exc, "status_code", None)
            if status != 400:
                raise
            bad = _guess_bad_param(str(exc), payload)
            if not bad:
                raise
            logger.warning("Параметр %r отвергнут API — отключаю его: %s", bad, exc)
            _DISABLED_PARAMS.add(bad)
            payload.pop(bad, None)

    raise RuntimeError("responses.create: не удалось подобрать совместимый набор параметров")


def _reasoning(effort: str) -> Optional[Dict[str, Any]]:
    effort = (effort or "").strip().lower()
    if effort in ("minimal", "low", "medium", "high"):
        return {"effort": effort}
    return None


# ─────────────────────────────────────────────
# STRICT matching helpers (by base only)
# ─────────────────────────────────────────────

_non_alnum = re.compile(r"[^a-z0-9\s-]+")
_spaces = re.compile(r"\s+")

# Производные кислот — НЕ считаются hard
_acid_derivative_pattern = re.compile(
    r"\b(palmitate|stearate|laurate|myristate|caprate|caprylate)\b",
    flags=re.I,
)


def _norm(text: str) -> str:
    text = (text or "").lower()
    text = _non_alnum.sub(" ", text)
    text = _spaces.sub(" ", text).strip()
    return text


def _has_word(text: str, word: str) -> bool:
    return re.search(rf"\b{re.escape(word)}\b", text) is not None


def _matches_phrase(term: str, ingredient_norm: str) -> bool:
    """term can be a single word or phrase."""
    t = _norm(term)
    if " " in t:
        return t in ingredient_norm
    return _has_word(ingredient_norm, t)


def classify_ingredient_strict(name: str, position: int) -> Dict[str, Any]:
    """
    STRICT classification (only by your fixed base + special rules from prompt):

    HARD:
      - any exact hard term match
      - wax special rule: if 'wax' is in ingredient NAME => hard
      - acids: ONLY exact "... acid" forms are hard; derivatives are NOT hard

    CONDITIONAL:
      - exact conditional term match
      - special partial matches allowed only for: sil / methicone / dimethicone
    """
    n = _norm(name)

    # ── HARD: wax special rule (in name)
    #   По умолчанию — как было: только отдельное слово «wax»
    #   (Candelilla Wax → да, Beeswax → нет).
    #   COMEDOGEN_MATCH_V2=true включает поиск внутри слова, как описано в промпте.
    if "wax" in n and (config.COMEDOGEN_MATCH_V2 or _has_word(n, "wax")):
        return {"is_hard": True, "is_conditional": False, "early_conditional": False}

    # ── HARD: strict list (with acid derivative exclusion)
    for term in hard_comedogens:
        t = _norm(term)

        # acids: only exact "... acid" entries are allowed as hard
        if t in (
            "palmitic acid",
            "stearic acid",
            "lauric acid",
            "myristic acid",
            "capric acid",
            "caprylic acid",
        ):
            if _matches_phrase(t, n):
                return {"is_hard": True, "is_conditional": False, "early_conditional": False}
            continue

        if _matches_phrase(t, n):
            # защита от ложных срабатываний на производные кислот (на всякий случай)
            if _acid_derivative_pattern.search(n) and ("acid" in t):
                continue
            return {"is_hard": True, "is_conditional": False, "early_conditional": False}

    # ── CONDITIONAL: strict list + partial rules
    for term, cutoff in conditional_comedogens.items():
        t = _norm(term)

        # "sil" — match as whole word only (so it doesn't catch "silica").
        # COMEDOGEN_MATCH_V2 дополнительно ловит «silicone» внутри слова
        # (Polysilicone-11 — пример из промпта), не задевая Silica.
        if t == "sil":
            if _has_word(n, "sil") or (config.COMEDOGEN_MATCH_V2 and "silicone" in n):
                return {"is_hard": False, "is_conditional": True, "early_conditional": position <= int(cutoff)}
            continue

        # "methicone"/"dimethicone" — partial match allowed
        if t in ("methicone", "dimethicone"):
            if t in n:
                return {"is_hard": False, "is_conditional": True, "early_conditional": position <= int(cutoff)}
            continue

        # others — exact/phrase match
        if _matches_phrase(t, n):
            return {"is_hard": False, "is_conditional": True, "early_conditional": position <= int(cutoff)}

    return {"is_hard": False, "is_conditional": False, "early_conditional": False}


def apply_comedogenic_flags_strict(ingredients: List[Dict[str, Any]]) -> None:
    """Mutates ingredients: sets is_hard/is_conditional strictly by the base."""
    for idx, ing in enumerate(ingredients, start=1):
        name = ing.get("name") or ""
        flags = classify_ingredient_strict(name, idx)
        ing["is_hard"] = bool(flags["is_hard"])
        ing["is_conditional"] = bool(flags["is_conditional"])
        ing["_early_conditional"] = bool(flags["early_conditional"])  # внутренний флаг для отладки


# ─────────────────────────────────────────────
# URL sanitation for source_url (Step 1 output)
# ─────────────────────────────────────────────

_URL_HTTP_RE = re.compile(r"^https?://", flags=re.I)
_URL_PROTOLESS_RE = re.compile(r"^(www\.)", flags=re.I)
_URL_SCHEMELESS_RE = re.compile(r"^//")


def _normalize_source_url(value: Any) -> Optional[str]:
    """
    Делает source_url безопасным:
    - принимает только реальные URL
    - нормализует //example.com -> https://example.com
    - нормализует www.example.com -> https://www.example.com
    - отсеивает "название продукта" и прочий текст
    """
    if not isinstance(value, str):
        return None

    url = value.strip()
    if not url:
        return None

    # если есть пробелы/переносы — почти наверняка это не URL (как в твоём кейсе)
    if any(ch in url for ch in (" ", "\n", "\t")):
        return None

    url = url.strip(' "\'()[]{}<>')
    if not url:
        return None

    if _URL_SCHEMELESS_RE.match(url):
        url = "https:" + url

    if _URL_PROTOLESS_RE.match(url):
        url = "https://" + url

    if not _URL_HTTP_RE.match(url):
        return None

    # минимальная защита от мусора типа "https://"
    if len(url) < 12:
        return None

    return url


# ─────────────────────────────────────────────
# Common helpers
# ─────────────────────────────────────────────

def _encode_image_to_base64(image_bytes: bytes) -> str:
    return base64.b64encode(image_bytes).decode("utf-8")


def _build_user_content(
    product_name: Optional[str],
    images: Optional[Sequence[bytes]] = None,
) -> List[Dict[str, Any]]:
    user_content: List[Dict[str, Any]] = []
    if product_name:
        user_content.append({"type": "input_text", "text": f"Название продукта от пользователя: {product_name}"})

    images = [img for img in (images or []) if img]
    if len(images) > 1:
        user_content.append(
            {
                "type": "input_text",
                "text": (
                    f"Прислано {len(images)} фото одного и того же продукта "
                    "(например, лицевая сторона упаковки и оборот с составом). "
                    "Используй их вместе как один материал."
                ),
            }
        )
    for img in images:
        b64 = _encode_image_to_base64(img)
        user_content.append({"type": "input_image", "image_url": f"data:image/jpeg;base64,{b64}"})

    if not user_content:
        user_content.append({"type": "input_text", "text": "Данных о продукте нет. Верни JSON с error."})
    return user_content


def _safe_json_loads(s: str) -> Optional[Dict[str, Any]]:
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def _strip_bullets(line: str) -> str:
    line = line.strip()
    line = re.sub(r"^[-•]\s*", "", line)
    line = re.sub(r"^\d+[.)]\s*", "", line)
    return line.strip()


def _parse_step2_marked_text_v2(text: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "summary": "",
        "comedogens_notes": [],
        "overall_notes": "",
        "recommendations": [],
    }
    if not text:
        return out

    t = text.strip()

    def _block(name: str) -> str:
        pattern = rf"{name}:\s*(.+?)(?:\n\s*\n(?:SUMMARY|COMEDOGENS|OVERALL|RECOMMENDATIONS):|\Z)"
        m = re.search(pattern, t, flags=re.S | re.I)
        return (m.group(1).strip() if m else "").strip()

    out["summary"] = _block("SUMMARY")
    out["overall_notes"] = _block("OVERALL")

    rec_block = _block("RECOMMENDATIONS")
    recs: List[str] = []
    if rec_block:
        for line in rec_block.splitlines():
            s = _strip_bullets(line)
            if s:
                recs.append(s)
    out["recommendations"] = recs[:10]

    com_block = _block("COMEDOGENS")
    notes: List[Dict[str, Any]] = []
    if com_block:
        for line in com_block.splitlines():
            s = _strip_bullets(line)
            if not s:
                continue

            parts = [p.strip() for p in s.split("|")]
            name = parts[0] if parts else ""
            pos: Optional[int] = None
            typ: Optional[str] = None
            note = ""

            for p in parts[1:]:
                pl = p.lower()
                if pl.startswith("pos="):
                    try:
                        pos = int(re.sub(r"[^0-9]", "", p))
                    except Exception:
                        pos = None
                elif pl.startswith("type="):
                    typ = p.split("=", 1)[1].strip().lower()
                elif pl.startswith("note="):
                    note = p.split("=", 1)[1].strip()

            if name:
                item: Dict[str, Any] = {"name": name}
                if pos is not None:
                    item["position"] = pos
                if typ in ("hard", "conditional"):
                    item["type"] = typ
                if note:
                    item["note"] = note
                notes.append(item)

    out["comedogens_notes"] = notes[:30]
    return out


# ─────────────────────────────────────────────
# Step 1
# ─────────────────────────────────────────────

async def run_agent_step1_ex(
    product_name: Optional[str] = None,
    image_bytes: Optional[bytes] = None,
    images: Optional[Sequence[bytes]] = None,
) -> AgentResult:
    """Шаг 1 + телеметрия. `images` — несколько фото одного продукта (альбом)."""
    all_images: List[bytes] = []
    if image_bytes:
        all_images.append(image_bytes)
    for img in images or []:
        if img and img not in all_images:
            all_images.append(img)

    user_content = _build_user_content(product_name, all_images)

    resp, usage = await _responses_create(
        model=MODEL,
        instructions=SYSTEM_PROMPT_STEP1,
        tools=[{"type": "web_search"}],
        max_tool_calls=config.STEP1_MAX_TOOL_CALLS,
        input=[{"role": "user", "content": user_content}],
        max_output_tokens=config.STEP1_MAX_OUTPUT_TOKENS,
        temperature=0,
        reasoning=_reasoning(config.STEP1_REASONING_EFFORT),
        prompt_cache_key=f"{config.PROMPT_CACHE_KEY}-step1" if config.PROMPT_CACHE_KEY else None,
    )

    raw = (resp.output_text or "").strip()

    # Делаем флаги ДЕТЕРМИНИРОВАННЫМИ (строго по базе)
    obj = _safe_json_loads(raw)
    if not obj:
        return AgentResult(raw=raw, usage=usage)

    ingredients = obj.get("ingredients")
    if isinstance(ingredients, list) and ingredients:
        apply_comedogenic_flags_strict(ingredients)
        obj["ingredients"] = ingredients

    # ✅ ВАЖНО: source_url либо валидный URL, либо null
    obj["source_url"] = _normalize_source_url(obj.get("source_url"))

    # risk_level НЕ считаем тут — это делает bot.py (строго по правилам)
    return AgentResult(raw=json.dumps(obj, ensure_ascii=False), usage=usage)


async def run_agent_step1(
    product_name: Optional[str] = None,
    image_bytes: Optional[bytes] = None,
) -> str:
    """Обратная совместимость: возвращает только JSON-строку."""
    result = await run_agent_step1_ex(product_name=product_name, image_bytes=image_bytes)
    return result.raw


# ─────────────────────────────────────────────
# Step 2
# ─────────────────────────────────────────────

def build_step2_payload(step1_payload: Dict[str, Any]) -> Dict[str, Any]:
    """Готовит вход для шага 2 (также используется как ключ кэша)."""
    ingredients = step1_payload.get("ingredients") or []

    comedogens: List[Dict[str, Any]] = []
    inci_list: List[str] = []

    for idx, ing in enumerate(ingredients, start=1):
        name = ing.get("name")
        if not name:
            continue
        inci_list.append(name)
        if ing.get("is_hard") or ing.get("is_conditional"):
            comedogens.append(
                {
                    "name": name,
                    "position": idx,
                    "type": "hard" if ing.get("is_hard") else "conditional",
                }
            )

    return {
        "product_name": step1_payload.get("product_name"),
        "risk_level": step1_payload.get("risk_level"),
        "comedogens": comedogens,
        "inci": inci_list,
    }


async def run_agent_step2_ex(step1_payload: Dict[str, Any]) -> AgentResult:
    payload = build_step2_payload(step1_payload)
    usage = Usage(model=MODEL)

    prompt_text_web = (
        "Данные шага 1 (источник истины). Не выдумывай ингредиенты.\n"
        "Сделай:\n"
        "1) SUMMARY — короткое пояснение результата.\n"
        "2) COMEDOGENS — по каждому комедогену: почему он может быть проблемным для пор (1–2 предложения).\n"
        "3) OVERALL — общий вывод по продукту.\n"
        "4) RECOMMENDATIONS — 3–7 практичных рекомендаций.\n\n"
        "Верни СТРОГО с маркерами:\n"
        "SUMMARY:\n"
        "<абзац>\n\n"
        "COMEDOGENS:\n"
        "- <Name> | pos=<N> | type=<hard|conditional> | note=<...>\n\n"
        "OVERALL:\n"
        "<абзац>\n\n"
        "RECOMMENDATIONS:\n"
        "- ...\n\n"
        "Данные:\n"
        + json.dumps(payload, ensure_ascii=False)
    )

    try:
        resp, u1 = await _responses_create(
            model=MODEL,
            instructions=SYSTEM_PROMPT_STEP2,
            tools=[{"type": "web_search"}] if config.STEP2_WEB_SEARCH else [],
            max_tool_calls=config.STEP2_MAX_TOOL_CALLS if config.STEP2_WEB_SEARCH else None,
            input=[{"role": "user", "content": [{"type": "input_text", "text": prompt_text_web}]}],
            max_output_tokens=config.STEP2_MAX_OUTPUT_TOKENS,
            temperature=0,
            reasoning=_reasoning(config.STEP2_REASONING_EFFORT),
            prompt_cache_key=f"{config.PROMPT_CACHE_KEY}-step2" if config.PROMPT_CACHE_KEY else None,
        )
        usage.merge(u1)

        parsed = _parse_step2_marked_text_v2((resp.output_text or "").strip())
        if parsed.get("summary") and parsed.get("recommendations"):
            return AgentResult(raw=json.dumps(parsed, ensure_ascii=False), usage=usage)

        logger.warning("STEP2 returned bad format; fallback to JSON-mode without web_search")

    except Exception as e:
        logger.warning("STEP2 first pass failed; fallback without web_search: %s", e)

    prompt_text_json = (
        "json\n"
        "Данные шага 1 (источник истины). Не выдумывай ингредиенты.\n"
        "Верни СТРОГО JSON-объект:\n"
        "{"
        "\"summary\":\"...\","
        "\"comedogens_notes\":[{\"name\":\"...\",\"position\":1,\"type\":\"hard\",\"note\":\"...\"}],"
        "\"overall_notes\":\"...\","
        "\"recommendations\":[\"...\",\"...\"]"
        "}\n\n"
        "Требования:\n"
        "- summary: 1 абзац, спокойный тон, на 'ты'\n"
        "- comedogens_notes: по каждому комедогену из данных\n"
        "- overall_notes: 1 абзац про продукт в целом\n"
        "- recommendations: 3–7 пунктов, практично, без лечения\n\n"
        "Данные:\n"
        + json.dumps(payload, ensure_ascii=False)
    )

    resp2, u2 = await _responses_create(
        model=MODEL,
        instructions=SYSTEM_PROMPT_STEP2,
        tools=[],
        input=[{"role": "user", "content": [{"type": "input_text", "text": prompt_text_json}]}],
        text={"format": {"type": "json_object"}},
        max_output_tokens=900,
        temperature=0,
        reasoning=_reasoning(config.STEP2_REASONING_EFFORT),
        prompt_cache_key=f"{config.PROMPT_CACHE_KEY}-step2" if config.PROMPT_CACHE_KEY else None,
    )
    usage.merge(u2)
    return AgentResult(raw=(resp2.output_text or "").strip(), usage=usage)


async def run_agent_step2(step1_payload: Dict[str, Any]) -> str:
    """Обратная совместимость: возвращает только JSON-строку."""
    result = await run_agent_step2_ex(step1_payload)
    return result.raw


async def run_agent(product_name: Optional[str] = None, image_bytes: Optional[bytes] = None) -> str:
    return await run_agent_step1(product_name=product_name, image_bytes=image_bytes)


async def aclose() -> None:
    """Аккуратно закрыть HTTP-соединения при остановке бота."""
    try:
        await client.close()
    except Exception:  # noqa: BLE001
        pass
