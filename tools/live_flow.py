#!/usr/bin/env python3
"""Сквозной прогон через НАСТОЯЩИЕ обработчики бота на живой модели.

Зачем отдельно от `live_check.py`. Тот зовёт функции разбора напрямую и
поэтому не мог увидеть бага, на котором бот сегодня молча умирал в проде:
падал сам обработчик фото, до любого разбора. Здесь проходится вся цепочка
целиком — handle_text, handle_photo и все шесть кнопок, — а подставные только
объекты Telegram (`tools/tg_fakes.py`, они повторяют поведение aiogram 3).

Проверяется ровно одно, и оно же было сломано: на каждом шаге человек
получает сообщение. Тишина — брак, даже если в логах «всё хорошо».

Запуск (нужен рабочий OPENAI_API_KEY):

    python -m tools.live_flow "CeraVe Moisturising Cream"
    python -m tools.live_flow "Product A" "Product B" --skip-photo

Расходует API примерно как live_check: три вызова модели на продукт
(шаг 1, шаг 2, состав) плюс ещё один шаг 1, если гоняется путь фото.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.tg_fakes import (  # noqa: E402
    FakeBot,
    FakeCallbackQuery,
    FakeMessage,
    buttons,
    callback_data,
)

TAGS = re.compile(r"</?[^>]+>")

# Валидный, но пустой JPEG: путь фото проверяет обработчик, а не зрение модели —
# название продукта уходит подписью, как и делает человек в чате.
BLANK_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRof"
    "Hh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAARCAABAAEDASIAAhEBAxEB"
    "/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9"
    "AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3"
    "ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKj"
    "pKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6"
    "/9oADAMBAAIRAxEAPwD3+iiigD//2Q=="
)


def plain(text: str) -> str:
    return TAGS.sub("", text or "")


def rule(title: str, ch: str = "─") -> None:
    print(f"\n{ch * 70}\n{title}\n{ch * 70}")


class Report:
    """Копит вердикты по шагам, чтобы в конце было видно одну таблицу."""

    def __init__(self) -> None:
        self.rows: List[Dict[str, Any]] = []

    def step(self, name: str, sent: List[Any], *, expect: Optional[str] = None) -> bool:
        texts = [plain(m.text) for m in sent]
        ok = bool(texts)
        detail = ""
        if not ok:
            detail = "ТИШИНА — обработчик ничего не отправил"
        elif expect and not any(expect.lower() in t.lower() for t in texts):
            ok = False
            detail = f"в ответе нет ожидаемого «{expect}»"
        else:
            detail = f"{len(texts)} сообщ., {sum(len(t) for t in texts)} симв."

        self.rows.append({"name": name, "ok": ok, "detail": detail})
        print(f"\n  {'✓' if ok else '✗ БРАК'}  {name}: {detail}")
        for t in texts:
            head = t.strip().splitlines()
            preview = next((ln for ln in head if ln.strip()), "")
            print(f"      → {preview[:96]}")
        return ok

    def failures(self) -> List[Dict[str, Any]]:
        return [r for r in self.rows if not r["ok"]]


async def _settle(pending_before: set, timeout: float = 180.0) -> None:
    """Дожидается фоновых задач, которые породили обработчики."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        extra = {t for t in asyncio.all_tasks() if t is not asyncio.current_task()} - pending_before
        alive = [t for t in extra if not t.done()]
        if not alive:
            return
        await asyncio.wait(alive, timeout=max(0.0, deadline - time.monotonic()))


def _token_from(markup: Any) -> Optional[str]:
    data = callback_data(markup, "состав") or callback_data(markup, "подробнее")
    return data.split(":", 1)[1] if data else None


async def run_product(product: str, *, skip_photo: bool) -> Report:
    from bot import bot as b

    rep = Report()
    rule(f"ПРОДУКТ: {product}", "━")

    baseline = {t for t in asyncio.all_tasks() if t is not asyncio.current_task()}
    fake_bot = FakeBot()

    # ── 1. Текст ──────────────────────────────────────────────
    msg = FakeMessage(text=product)
    t0 = time.monotonic()
    await b.handle_text(msg, fake_bot)
    await _settle(baseline)
    rep.step(f"handle_text ({int((time.monotonic() - t0) * 1000)} мс)", msg.sent)

    final = next((m for m in reversed(msg.sent) if m.reply_markup is not None), None)
    if final is None:
        print("\n  ✗ шаг 1 не дал кнопок — дальше идти не с чем")
        return rep
    print(f"      кнопки: {[label for label, _ in buttons(final.reply_markup)]}")

    token = _token_from(final.reply_markup)
    if not token:
        print("\n  ✗ в кнопках нет токена разбора")
        return rep

    # ── 2. Фото ───────────────────────────────────────────────
    if not skip_photo:
        async def _collect(bot, photos):
            return [BLANK_JPEG]

        original = b._collect_images
        b._collect_images = _collect
        try:
            photo_msg = FakeMessage(photo=True, caption=product)
            t0 = time.monotonic()
            await b.handle_photo(photo_msg, fake_bot)
            await _settle(baseline)
            rep.step(
                f"handle_photo ({int((time.monotonic() - t0) * 1000)} мс)",
                photo_msg.sent,
                expect=b.PROCESSING_PHOTO,
            )
            # Плашка «Анализирую фото» обязана исчезнуть после ответа.
            plate = photo_msg.sent[0] if photo_msg.sent else None
            if plate is not None and plate.text == b.PROCESSING_PHOTO and not plate.deleted:
                print("      ⚠️  плашка осталась висеть — её не удалили")
        finally:
            b._collect_images = original

    # ── 3. Кнопки ─────────────────────────────────────────────
    async def press(data: str, name: str, *, with_bot: bool = False, expect=None) -> List[Any]:
        cb = FakeCallbackQuery(data)
        handler = {
            "composition": b.handle_composition_callback,
            "step2": b.handle_step2_callback,
            "groups": b.handle_groups_callback,
            "grp": b.handle_group_card_callback,
            "gq": b.handle_group_questions_callback,
            "doc": b.handle_doctor_callback,
        }[data.split(":", 1)[0]]

        before = {t for t in asyncio.all_tasks() if t is not asyncio.current_task()}
        started = time.monotonic()
        if with_bot:
            await handler(cb, fake_bot)
        else:
            await handler(cb)
        await _settle(before)

        # Шаг 2 уходит через bot.send_message — забираем и оттуда.
        sent = list(cb.message.sent)
        if with_bot:
            sent += [m for m in fake_bot.sent if m not in sent]
        rep.step(f"{name} ({int((time.monotonic() - started) * 1000)} мс)", sent, expect=expect)
        if cb.answers and cb.answers[-1].get("show_alert"):
            print(f"      ⚠️  всплывашка вместо экрана: {cb.answers[-1]['text']}")
        return sent

    await press(f"composition:{token}", "handle_composition_callback", expect="Состав")
    fake_bot.sent.clear()
    await press(f"step2:{token}", "handle_step2_callback", with_bot=True, expect="Подробнее")
    groups_sent = await press(f"groups:{token}", "handle_groups_callback")

    markup = next((m.reply_markup for m in reversed(groups_sent) if m.reply_markup), None)
    group_keys = [
        data.split(":", 2)[2]
        for _, data in buttons(markup)
        if data and data.startswith("grp:")
    ]
    if not group_keys:
        print("\n  ✗ экран групп не дал ни одной кнопки группы")
    for key in group_keys:
        card = await press(f"grp:{token}:{key}", f"handle_group_card_callback [{key}]")
        card_markup = next((m.reply_markup for m in reversed(card) if m.reply_markup), None)
        if any(d and d.startswith("gq:") for _, d in buttons(card_markup)):
            await press(f"gq:{token}:{key}", f"handle_group_questions_callback [{key}]")

    if b._doctor_questions(token):
        await press(f"doc:{token}", "handle_doctor_callback", expect="врач")
    else:
        print("\n  ⚠️  вопросов врачу не набралось — кнопка «doc:» не проверена")

    return rep


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("products", nargs="+", help="названия средств")
    ap.add_argument("--skip-photo", action="store_true", help="не гонять путь фото (дешевле)")
    args = ap.parse_args()

    from bot import analytics

    analytics.init()

    reports: List[tuple] = []
    for product in args.products:
        try:
            reports.append((product, await run_product(product, skip_photo=args.skip_photo)))
        except Exception as exc:  # noqa: BLE001
            import traceback

            traceback.print_exc()
            print(f"\n✗ {product}: {type(exc).__name__}: {exc}")
            reports.append((product, None))

    from agent.agent import aclose

    await aclose()

    rule("ИТОГО", "━")
    bad = 0
    for product, rep in reports:
        if rep is None:
            print(f"✗ {product}: прогон упал")
            bad += 1
            continue
        fails = rep.failures()
        bad += len(fails)
        mark = "✓" if not fails else "✗"
        print(f"{mark} {product}: шагов {len(rep.rows)}, брака {len(fails)}")
        for f in fails:
            print(f"    ✗ {f['name']}: {f['detail']}")

    print(f"\n{'Все экраны ответили.' if not bad else f'Мест с тишиной/браком: {bad}'}")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    asyncio.run(main())
