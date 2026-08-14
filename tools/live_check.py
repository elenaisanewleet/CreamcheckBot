#!/usr/bin/env python3
"""Живая проверка качества ответов на реальных продуктах.

Тесты в `tests/` проверяют код, но не могут проверить главное — насколько
осмысленно пишет модель. Этот скрипт прогоняет полный путь на настоящих
продуктах, показывает все экраны глазами и считает две метрики, ради
которых всё затевалось:

  • привязаны ли рекомендации к конкретному составу или это общие места;
  • получил ли пояснение каждый компонент INCI.

Запуск (нужен рабочий .env с OPENAI_API_KEY):

    python -m tools.live_check "CeraVe Moisturising Cream"
    python -m tools.live_check "Product A" "Product B" --raw

Расходует API. Один продукт — три вызова модели (шаг 1, шаг 2, состав).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TAGS = re.compile(r"</?[^>]+>")


def plain(text: str) -> str:
    """Как сообщение выглядит у человека: без HTML-разметки."""
    return TAGS.sub("", text or "")


def rule(title: str, ch: str = "─") -> None:
    print(f"\n{ch * 70}\n{title}\n{ch * 70}")


def mentions_composition(rec: str, names: list[str]) -> bool:
    """Грубая проверка «не шаблон ли»: назван ли компонент или его позиция.

    Не истина в последней инстанции — рекомендация может быть привязана
    к составу и без прямого называния. Но систематические промахи этой
    проверки означают, что пункты снова уехали в общие места.
    """
    low = rec.lower()
    for name in names:
        parts = [p for p in re.split(r"[\s/&-]+", name.lower()) if len(p) > 4]
        if any(p in low for p in parts):
            return True
    return bool(re.search(r"\b(позици|перв|втор|треть|четвёрт|пят)\w*", low))


async def check(product: str, show_raw: bool) -> dict:
    from agent.agent import run_agent_ingredients_ex, run_agent_step1_ex, run_agent_step2_ex
    from bot import bot as b

    rule(f"ПРОДУКТ: {product}", "━")

    t0 = time.monotonic()
    r1 = await run_agent_step1_ex(product_name=product)
    step1 = b._parse_agent_json(r1.raw)
    if not step1:
        print("✗ шаг 1 не вернул JSON")
        if show_raw:
            print(r1.raw[:2000])
        return {"product": product, "ok": False}

    from agent.agent import apply_comedogenic_flags_strict

    ingredients = step1.get("ingredients") or []
    apply_comedogenic_flags_strict(ingredients)
    step1["risk_level"] = b.calc_risk_level_strict(ingredients)

    names = [i.get("name", "") for i in ingredients]
    print(f"шаг 1: {len(names)} компонентов, риск «{step1['risk_level']}», "
          f"{int((time.monotonic() - t0) * 1000)} мс")

    r2 = await run_agent_step2_ex(step1)
    step2 = b._parse_agent_json(r2.raw) or {}
    r3 = await run_agent_ingredients_ex(step1)
    ing = b._parse_agent_json(r3.raw) or {"groups": []}

    rule("ЭКРАН «ПОДРОБНЕЕ»")
    print(plain(b.build_step2_message(step2, product_name=step1.get("product_name"),
                                      risk_level=step1.get("risk_level"))))

    rule("ЭКРАН «ЧТО ДЕЛАЕТ КАЖДЫЙ КОМПОНЕНТ»")
    print(plain(b.build_groups_message(step1, ing)))
    for g in ing.get("groups") or []:
        rule(f"  группа: {g.get('key')}", "·")
        print(plain(b.build_group_card_message(step1, g)))

    questions = step2.get("doctor_questions") or []
    if questions:
        rule("ЭКРАН «ЧТО СПРОСИТЬ У ВРАЧА»")
        print(plain(b.build_doctor_questions_message(step1, questions)))

    # ── Метрики ───────────────────────────────────────────────
    recs = step2.get("recommendations") or []
    tied = [r for r in recs if mentions_composition(str(r), names)]
    explained = sum(
        1 for g in ing.get("groups") or [] for it in g.get("items") or [] if it.get("note")
    )

    rule("ПРОВЕРКА", "━")
    print(f"рекомендаций: {len(recs)}, привязано к составу: {len(tied)}")
    for r in recs:
        mark = "✓" if mentions_composition(str(r), names) else "✗ ОБЩЕЕ МЕСТО"
        print(f"  {mark}  {str(r)[:110]}")

    print(f"\nвопросов врачу: {len(questions)} (ожидается 3)")
    print(f"компонентов с пояснением: {explained} из {len(names)}")
    if explained < len(names):
        missing = [
            it.get("name")
            for g in ing.get("groups") or []
            for it in g.get("items") or []
            if not it.get("note")
        ]
        print(f"  без пояснения: {', '.join(filter(None, missing))}")

    from bot.analytics import estimate_cost

    print()
    total = 0.0
    for label, u in (("шаг 1", r1.usage), ("шаг 2", r2.usage), ("состав", r3.usage)):
        cost = estimate_cost(u.in_tokens, u.cached_tokens, u.out_tokens, u.tool_calls)
        total += cost
        print(f"{label:8} {u.api_ms:6} мс  вход {u.in_tokens:6} "
              f"(кэш {u.cached_tokens:5})  выход {u.out_tokens:5}  "
              f"поисков {u.tool_calls}  ${cost:.4f}")
    print(f"итого примерно ${total:.4f} за продукт "
          f"(по ценам из PRICE_* в .env)")

    return {
        "product": product,
        "ok": True,
        "recs": len(recs),
        "tied": len(tied),
        "questions": len(questions),
        "explained": explained,
        "total": len(names),
    }


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("products", nargs="+", help="названия средств")
    ap.add_argument("--raw", action="store_true", help="печатать сырой ответ при сбое разбора")
    args = ap.parse_args()

    results = []
    for product in args.products:
        try:
            results.append(await check(product, args.raw))
        except Exception as exc:  # noqa: BLE001
            print(f"\n✗ {product}: {type(exc).__name__}: {exc}")
            results.append({"product": product, "ok": False})

    from agent.agent import aclose

    await aclose()

    rule("ИТОГО", "━")
    for r in results:
        if not r.get("ok"):
            print(f"✗ {r['product']}: не разобрано")
            continue
        print(f"{r['product']}: рекомендаций {r['tied']}/{r['recs']} по составу, "
              f"вопросов {r['questions']}, пояснений {r['explained']}/{r['total']}")


if __name__ == "__main__":
    asyncio.run(main())
