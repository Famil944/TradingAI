"""Bounded second research algorithm; REST only, no second stream or polling bot."""

import argparse
import asyncio
import json
import logging
import time

from aiogram import Bot

from config.settings import ROOT, settings
from core.logging import SecretFormatter
from core.single_instance import SingleInstance
from database.db import Database
from services.otc_conditional import Setup, make_plan, message
from services.otc_data import OTChartsProvider
from services.otc_market import eligible
from services.otc_ml.features import FEATURE_NAMES, indicators
from services.otc_session import db_call

STRATEGY = "observer-bb-rsi-adx-v1"
STOP_FILE = ROOT / "data" / "observer.stop"
STATE_FILE = ROOT / "data" / "observer-status.json"


def observer_setup(minutes, closed_at):
    bars = [b for b in minutes if b.time + 60 <= closed_at][-120:]
    if not bars or bars[-1].time + 60 != closed_at:
        return None
    values = indicators(bars)
    if values is None:
        return None
    v = dict(zip(FEATURE_NAMES, values))
    if v["adx14"] > 0.25 or v["atr14"] <= 0 or v["range_atr"] > 2.5:
        return None
    strength = v["atr14"] * bars[-1].close
    if v["bb_position"] <= 0.15 and v["rsi14"] <= 0.35:
        return Setup(
            "UP",
            "green",
            min(b.low for b in bars[-5:]) - 0.25 * strength,
            "Bollinger lower band, RSI≤35, ADX≤25; green reversal condition",
        )
    if v["bb_position"] >= 0.85 and v["rsi14"] >= 0.65:
        return Setup(
            "DOWN",
            "red",
            max(b.high for b in bars[-5:]) + 0.25 * strength,
            "Bollinger upper band, RSI≥65, ADX≤25; red reversal condition",
        )
    return None


async def notify(bot, text):
    try:
        async with asyncio.timeout(10):
            await bot.send_message(settings.admin_chat_id, text)
    except Exception as exc:
        logging.getLogger(__name__).warning(
            "Observer notification: %s", type(exc).__name__
        )


async def publish(bot, db, plan, clock=time.time):
    if STOP_FILE.exists() or clock() > plan.delivery_deadline:
        return False
    identifier = await db_call(db.prepare_plan, plan, clock(), strategy=STRATEGY)
    if identifier is None:
        return False
    text = "👁 НАБЛЮДАТЕЛЬ · ТЕСТ\n" + message(plan, identifier, None)
    try:
        async with asyncio.timeout(min(10, max(0.1, plan.delivery_deadline - clock()))):
            await bot.send_message(settings.admin_chat_id, text)
    except Exception:
        await db_call(db.plan_delivered, identifier, clock(), False)
        return False
    at = clock()
    await db_call(db.plan_delivered, identifier, at, True)
    return at <= plan.delivery_deadline


def state(value):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = STATE_FILE.with_suffix(".tmp")
    temp.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temp.replace(STATE_FILE)


async def run(minutes=30, budget=16):
    if not 1 <= minutes <= 120 or not 2 <= budget <= 61:
        raise ValueError("Invalid observer limits")
    STOP_FILE.unlink(missing_ok=True)
    db = Database(ROOT / "data" / "observer.db")
    await db_call(db.init_db)
    await db_call(db.close_plans, time.time(), cancel=True)
    provider = OTChartsProvider(settings.otcharts_api_key)
    bot = Bot(settings.telegram_bot_token)
    end = time.time() + minutes * 60
    used = sent = index = 0
    summary = dict(
        status="starting", strategy=STRATEGY, ends_at=end, requests=0, plans=0
    )
    try:
        plan = await provider.usage()
        if plan.remaining < budget + 10:
            raise RuntimeError("Insufficient quota reserve for bounded observer")
        used += 1
        assets = eligible(
            await provider.symbols(), time.time(), research=settings.test_mode
        )[:5]
        if not assets:
            raise RuntimeError("No eligible OTC assets")
        await notify(
            bot, f"👁 Наблюдатель включён: {minutes} мин. Отдельные тестовые планы."
        )
        while time.time() < end and used < budget and not STOP_FILE.exists():
            asset = assets[index % len(assets)]
            index += 1
            current = await provider.usage()
            if current.remaining <= 10:
                break
            used += 1
            try:
                bars = await provider.candles(asset.symbol, time.time())
                if bars:
                    at = bars[-1].time + 60
                    context = observer_setup(bars, at)
                    if context is not None:
                        advance = make_plan(asset.symbol, at, context, time.time(), end)
                        if advance is not None and await publish(bot, db, advance):
                            sent += 1
                summary.update(
                    status="watching",
                    requests=used,
                    plans=sent,
                    last_checked=asset.symbol,
                    last_check_at=time.time(),
                )
            except Exception as exc:
                summary.update(
                    status="data_error",
                    error_type=type(exc).__name__,
                    requests=used,
                    plans=sent,
                )
            state(summary)
            wake = (int(time.time()) // 120 + 1) * 120 + 2
            while time.time() < min(wake, end) and not STOP_FILE.exists():
                await asyncio.sleep(min(3, max(0.1, min(wake, end) - time.time())))
        summary.update(status="stopped", requests=used, plans=sent)
    except Exception as exc:
        summary.update(
            status="failed", error_type=type(exc).__name__, requests=used, plans=sent
        )
        logging.getLogger(__name__).warning("Observer stopped: %s", type(exc).__name__)
    finally:
        await db_call(db.close_plans, time.time(), cancel=True)
        state(summary)
        await notify(
            bot, f"👁 Наблюдатель завершён. Планов: {sent}. Его прежние планы отменены."
        )
        await provider.close()
        await bot.session.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--minutes", type=int, default=30)
    parser.add_argument("--budget", type=int, default=16)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    for handler in logging.getLogger().handlers:
        handler.setFormatter(
            SecretFormatter((settings.telegram_bot_token, settings.otcharts_api_key))
        )
    logging.getLogger("aiogram").setLevel(logging.CRITICAL)
    logging.getLogger("aiohttp").setLevel(logging.CRITICAL)
    instance = SingleInstance(port=47654)
    if not instance.acquire():
        raise SystemExit("Observer already running")
    try:
        asyncio.run(run(args.minutes, args.budget))
    finally:
        instance.close()
