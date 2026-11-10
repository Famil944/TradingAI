"""Experimental advance plans. Future candle colour is a condition, never a feature."""

from dataclasses import asdict, dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from services.otc_strategy import aggregate, atr, ema, rsi

MOSCOW = ZoneInfo("Europe/Moscow")
STRATEGY = "conditional-trend-pullback-v1"
CONTEXT_STRATEGY = "conditional-context-v2"


@dataclass(frozen=True)
class Setup:
    direction: str
    colour: str
    invalidation: float
    reason: str
    rule: str = STRATEGY
    evidence: dict | None = None


@dataclass(frozen=True)
class Plan:
    symbol: str
    setup_at: int
    entry_at: int
    expires_at: int
    condition_from: int
    delivery_deadline: int
    setup: Setup


def setup(minutes, closed_at):
    """A trend context for a possible opposite-colour pullback three minutes later."""
    bars = [b for b in minutes if b.time + 60 <= closed_at]
    if len(bars) < 250 or bars[-1].time + 60 != closed_at:
        return None
    if any(b.time - a.time != 60 for a, b in zip(bars[-250:], bars[-249:])):
        return None
    higher = [b for b in aggregate(bars, 300) if b.time + 300 <= closed_at]
    if len(higher) < 50 or any(
        b.time - a.time != 300 for a, b in zip(higher[-50:], higher[-49:])
    ):
        return None
    closes = [b.close for b in bars]
    fast, slow = ema(closes, 20), ema(closes, 50)
    prior = ema(closes[:-3], 20)
    strength = atr(bars)
    oscillator = rsi(closes)
    higher_closes = [b.close for b in higher]
    upper_fast, upper_slow = ema(higher_closes, 20), ema(higher_closes, 50)
    if (
        strength is None
        or strength <= 0
        or abs(fast - slow) < 0.10 * strength
        or bars[-1].high - bars[-1].low > 2.5 * strength
    ):
        return None
    up = (
        fast > slow
        and fast - prior >= 0.05 * strength
        and closes[-1] > slow
        and 42 <= oscillator <= 70
        and upper_fast > upper_slow
    )
    down = (
        fast < slow
        and prior - fast >= 0.05 * strength
        and closes[-1] < slow
        and 30 <= oscillator <= 58
        and upper_fast < upper_slow
    )
    if up:
        return Setup(
            "UP",
            "red",
            min(slow, min(b.low for b in bars[-5:])) - 0.25 * strength,
            f"Восходящий контекст EMA20/50 M1 и M5, RSI {oscillator:.1f}; ожидается красный откат",
        )
    if down:
        return Setup(
            "DOWN",
            "green",
            max(slow, max(b.high for b in bars[-5:])) + 0.25 * strength,
            f"Нисходящий контекст EMA20/50 M1 и M5, RSI {oscillator:.1f}; ожидается зелёный откат",
        )
    return None


def context_setup(minutes, closed_at):
    """Choose a hypothesis from observed context, never guess the future colour."""
    from services.otc_ml.features import FEATURE_NAMES, indicators

    base = setup(minutes, closed_at)
    if base is None:
        return None
    bars = [b for b in minutes if b.time + 60 <= closed_at]
    feature_values = indicators(bars[-250:])
    if feature_values is None:
        return None
    f = dict(zip(FEATURE_NAMES, feature_values))
    last, previous = bars[-1], bars[-2]
    span = last.high - last.low
    strength = f["atr14"] * last.close
    if span <= 0 or strength <= 0:
        return None
    body = abs(last.close - last.open) / span
    impulse = (
        f["adx14"] >= 0.20
        and body >= 0.5
        and (
            (
                base.direction == "UP"
                and last.close > last.open
                and last.close > previous.high
                and f["rsi14"] >= 0.55
                and f["macd_hist"] > 0
            )
            or (
                base.direction == "DOWN"
                and last.close < last.open
                and last.close < previous.low
                and f["rsi14"] <= 0.45
                and f["macd_hist"] < 0
            )
        )
    )
    near_fast = abs(f["ema20_gap"] * last.close) <= strength
    pullback = near_fast and (
        (
            base.direction == "UP"
            and last.close < last.open
            and 0.42 <= f["rsi14"] <= 0.60
        )
        or (
            base.direction == "DOWN"
            and last.close > last.open
            and 0.40 <= f["rsi14"] <= 0.58
        )
    )
    if impulse:
        colour = "green" if base.direction == "UP" else "red"
        kind = "momentum"
        description = "Импульс: пробой предыдущей свечи, MACD, ADX и тренд M1/M5"
    elif pullback:
        colour = "red" if base.direction == "UP" else "green"
        kind = "pullback"
        description = "Откат к EMA20 в согласованном тренде M1/M5"
    else:
        return None
    return Setup(
        base.direction,
        colour,
        base.invalidation,
        f"{description}; RSI {100 * f['rsi14']:.1f}, ADX {100 * f['adx14']:.1f}. Гипотеза, преимущество не доказано.",
        f"{CONTEXT_STRATEGY}:{kind}:{base.direction}:{colour}",
        {
            "known_asof": closed_at,
            "last_closed_candle": asdict(last),
            "previous_closed_candle": asdict(previous),
            "features": f,
            "context": kind,
            "base_context": base.reason.split(";")[0],
            "advantage_confirmed": False,
        },
    )


def make_plan(symbol, closed_at, context, now, session_end=None):
    if closed_at % 60 or not 0 <= now - closed_at <= 30:
        return None
    entry = closed_at + 180
    plan = Plan(symbol, closed_at, entry, entry + 60, entry - 60, entry - 120, context)
    if now > plan.delivery_deadline or (
        session_end is not None and plan.expires_at > session_end
    ):
        return None
    return plan


def condition_met(plan, candle):
    if candle.time != plan.condition_from:
        return False
    colour_ok = (
        candle.close < candle.open
        if plan.setup.colour == "red"
        else candle.close > candle.open
    )
    level_ok = (
        candle.close > plan.setup.invalidation
        if plan.setup.direction == "UP"
        else candle.close < plan.setup.invalidation
    )
    return colour_ok and level_ok


def display_symbol(symbol):
    names = {
        "#FB_otc": "Facebook OTC",
        "#AAPL_otc": "Apple OTC",
        "#AXP_otc": "American Express OTC",
        "#CSCO_otc": "Cisco OTC",
        "#INTC_otc": "Intel OTC",
    }
    return names.get(symbol, symbol)


def message(plan, identifier, payout=None, *, compact=True, name=None):
    def clock(at):
        return datetime.fromtimestamp(at, MOSCOW).strftime("%H:%M:%S")

    colour = (
        "КРАСНОЙ (закрытие ниже открытия)"
        if plan.setup.colour == "red"
        else "ЗЕЛЁНОЙ (закрытие выше открытия)"
    )
    direction = "ВВЕРХ / CALL" if plan.setup.direction == "UP" else "ВНИЗ / PUT"
    level = "выше" if plan.setup.direction == "UP" else "ниже"
    payout_text = (
        "неизвестна"
        if payout is None
        else f"{payout:g}% (значение сейчас; перед входом проверь сам)"
    )
    if compact:
        colour_short = "КРАСНАЯ" if plan.setup.colour == "red" else "ЗЕЛЁНАЯ"
        sign = ">" if plan.setup.direction == "UP" else "<"
        direction_short = "ВВЕРХ" if plan.setup.direction == "UP" else "ВНИЗ"
        return (
            f"🧪 {name or display_symbol(plan.symbol)}"
            + (f" · {payout:g}% сейчас" if payout is not None else " · выплата ?")
            + "\n"
            f"Вход {direction_short} — {clock(plan.entry_at)} МСК · 1 мин\n"
            f"Если свеча {clock(plan.condition_from)}–{clock(plan.entry_at)} {colour_short}, закрытие {sign} {plan.setup.invalidation:.8g}\n"
            f"Иначе — ПРОПУСК. Получить до {clock(plan.delivery_deadline)}."
        )
    return (
        f"УСЛОВНЫЙ ПЛАН · ЭКСПЕРИМЕНТАЛЬНЫЙ РЕЖИМ\n{plan.symbol}\n"
        f"Вход: {datetime.fromtimestamp(plan.entry_at, MOSCOW):%d.%m} {clock(plan.entry_at)} МСК · {direction}\nЭкспирация: 1 минута, до {clock(plan.expires_at)} МСК\n"
        f"Проверь на Pocket Option свечу M1 {clock(plan.condition_from)}–{clock(plan.entry_at)}:\n"
        f"если закрылась {colour} и закрытие {level} {plan.setup.invalidation:.8g} — условие выполнено.\n"
        "Другой цвет, доджи, нарушение уровня, отсутствие котировок или опоздание ко входу — ПРОПУСК.\n"
        f"Получено позже {clock(plan.delivery_deadline)} МСК — ПРОПУСК. Проверь совпадение часов и инструмента.\n"
        f"Выплата: {payout_text}; проверь открытие и выплату на платформе.\n"
        "Цвет свечи и вход проверяешь САМ; второго подтверждения не будет. "
        "При остановке/перезапуске бота прежние планы отменяются. Сделки бот не открывает.\n"
        f"Основание сейчас: {plan.setup.reason}. Условия через 3 минуты могут измениться.\nID: {identifier}"
    )


def replay(raw, *, rule="context"):
    """Evaluate a fixed advance-plan rule using future bars only AFTER scheduling.

    No real manual entries are assumed; outcomes remain UNKNOWN without
    independent platform endpoints and known payout at the hypothetical entry.
    """
    from collections import defaultdict

    from services.otc_market import utc_timestamp
    from services.otc_replay import matched_open
    from services.otc_strategy import normalize_candles

    groups = defaultdict(list)
    for row in raw:
        groups[row["symbol"]].append(row)
    result = []
    for symbol, rows in sorted(groups.items()):
        if not rows:
            continue
        stamps = [utc_timestamp(r["time"]) for r in rows]
        if any(b <= a for a, b in zip(stamps, stamps[1:])):
            raise ValueError("History must be strictly chronological")
        bars = normalize_candles(rows, int(stamps[-1] + 60))
        if len(bars) != len(rows) or any(
            b.time <= a.time for a, b in zip(bars, bars[1:])
        ):
            raise ValueError("Invalid or duplicate historical candles")
        by_time = {bar.time: (bar, row) for bar, row in zip(bars, rows)}
        for index in range(250, len(bars)):
            at = bars[index].time
            context = (context_setup if rule == "context" else setup)(bars[:index], at)
            if context is None:
                continue
            plan = make_plan(symbol, at, context, at)
            item = dict(
                symbol=symbol,
                setup_at=at,
                entry_at=plan.entry_at,
                expires_at=plan.expires_at,
                direction=context.direction,
                required_colour=context.colour,
                rule=context.rule,
                condition="UNKNOWN",
                outcome="UNKNOWN",
            )
            check = by_time.get(plan.condition_from)
            entry = by_time.get(plan.entry_at)
            expiry = by_time.get(plan.expires_at)
            if check:
                item["condition"] = (
                    "MET" if condition_met(plan, check[0]) else "NOT_MET"
                )
                if item["condition"] == "NOT_MET":
                    item["outcome"] = "SKIPPED"
            if item["condition"] == "MET" and entry and expiry:
                bar, row = entry
                end, endrow = expiry
                payout = row.get("payout")
                stamp = row.get("payout_at")
                if (
                    matched_open(row, bar)
                    and matched_open(endrow, end)
                    and row.get("open_confirmed") is True
                    and isinstance(payout, (int, float))
                    and not isinstance(payout, bool)
                    and 90 <= payout <= 100
                    and stamp is not None
                    and 0 <= plan.entry_at - float(stamp) <= 60
                ):
                    difference = end.open - bar.open
                    item["outcome"] = (
                        "DRAW"
                        if difference == 0
                        else "WIN"
                        if (difference > 0) == (context.direction == "UP")
                        else "LOSS"
                    )
            result.append(item)
    selected = []
    active_until = {}
    last = float("-inf")
    for item in sorted(result, key=lambda r: (r["setup_at"], r["symbol"])):
        at = item["setup_at"]
        if at - last < 120 or at < active_until.get(item["symbol"], 0):
            continue
        selected.append(item)
        last = at
        active_until[item["symbol"]] = item["expires_at"]
    return dict(
        strategy=CONTEXT_STRATEGY if rule == "context" else STRATEGY,
        mode="hypothetical advance plans, not actual manual trades",
        advantage_confirmed=False,
        plans=selected,
    )


if __name__ == "__main__":
    import argparse
    import json
    from pathlib import Path

    parser = argparse.ArgumentParser(
        description="Offline advance-plan replay; no real orders"
    )
    parser.add_argument("--replay", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--data-kind", choices=("recorded", "synthetic"), required=True)
    parser.add_argument("--rule", choices=("context", "legacy"), default="context")
    args = parser.parse_args()
    if args.output.resolve() == args.replay.resolve():
        parser.error("Output must differ from input")
    rows = [
        json.loads(line)
        for line in args.replay.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    report = replay(rows, rule=args.rule)
    report["data_kind"] = args.data_kind
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    print("Conditional replay saved; advantage remains unconfirmed.")
