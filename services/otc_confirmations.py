"""Versioned, causal explanations of a direction; never probability estimates."""

from services.otc_ml.features import FEATURE_NAMES, indicators
from services.otc_regime import classify
from services.otc_strategy import aggregate, ema

VERSION = "direction-confirmations-v1"
LABELS = {
    "ema": "EMA20/50",
    "rsi": "RSI14",
    "macd": "MACD",
    "candle": "Закрытая свеча",
    "alignment": "Согласование ТФ",
}


def _sign(value, direction):
    if value == 0:
        return "neutral"
    return "agree" if (value > 0) == (direction == "UP") else "against"


def build(minutes, direction, seconds, asof):
    if direction not in ("UP", "DOWN") or seconds not in (60, 120, 180, 300, 900):
        raise ValueError("Invalid direction or expiration")
    # Variable M2/M3 forecasts use M1 indicators, as in the preview models.
    period = seconds if seconds in (60, 300, 900) else 60
    past = [b for b in minutes if b.time + 60 <= asof]
    bars = aggregate(past, period)[-250:]
    features = indicators(bars)
    checks = {key: "unavailable" for key in LABELS}
    result = dict(
        version=VERSION,
        asof=asof,
        period=period,
        checks=checks,
        count=0,
        available=0,
        total=5,
        matched=[],
        market={},
    )
    if not bars or not 0 <= asof - bars[-1].time - period < period or features is None:
        return result
    f = dict(zip(FEATURE_NAMES, features))
    checks["ema"] = _sign(f["ema_spread"], direction)
    # The middle RSI band is neutral; extreme values are a separate warning.
    checks["rsi"] = _sign(
        1 if f["rsi14"] >= 0.55 else -1 if f["rsi14"] <= 0.45 else 0, direction
    )
    checks["macd"] = _sign(f["macd_hist"], direction)
    checks["candle"] = _sign(bars[-1].close - bars[-1].open, direction)
    alignment_period = 300 if period == 60 else 900 if period == 300 else 60
    alignment_bars = aggregate(past, alignment_period)[-250:]
    if (
        len(alignment_bars) >= 50
        and 0 <= asof - alignment_bars[-1].time - alignment_period < alignment_period
        and all(
            b.time - a.time == alignment_period
            for a, b in zip(alignment_bars, alignment_bars[1:])
        )
    ):
        prices = [b.close for b in alignment_bars]
        checks["alignment"] = _sign(ema(prices, 20) - ema(prices, 50), direction)
    result.update(
        market_state=classify(f),
        count=sum(v == "agree" for v in checks.values()),
        available=sum(v != "unavailable" for v in checks.values()),
        matched=[k for k, v in checks.items() if v == "agree"],
        alignment_period=alignment_period,
        market=dict(
            adx=f["adx14"] * 100,
            atr_percent=f["atr14"] * 100,
            rsi_extreme=f["rsi14"] >= 0.70 or f["rsi14"] <= 0.30,
        ),
    )
    return result


def format_confirmations(report):
    if not report:
        return "Подтверждения: нет сохранённых данных"
    if not report["available"]:
        return "Подтверждения: недостаточно закрытых свечей"
    icons = dict(agree="✅", against="❌", neutral="➖", unavailable="❔")
    lines = [f"Подтверждения: {report['count']} из {report['total']}"]
    for key, label in LABELS.items():
        if key == "alignment":
            label += f" M{report['alignment_period'] // 60}"
        lines.append(f"{icons[report['checks'][key]]} {label}")
    market = report["market"]
    lines.append(
        f"ADX {market['adx']:.1f} · ATR {market['atr_percent']:.3f}% — характеристики рынка"
    )
    if market["rsi_extreme"]:
        lines.append("RSI в крайней зоне")
    lines.append("Число совпадений — не вероятность выигрыша; признаки связаны.")
    return "\n".join(lines)
