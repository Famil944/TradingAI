"""Calibrated probability diagnostics and day-block return comparisons."""

import math
from collections import defaultdict


def expected_return(probabilities, direction, payout):
    if (
        not isinstance(payout, (int, float))
        or not math.isfinite(payout)
        or not 90 <= payout <= 100
    ):
        return None
    if (
        len(probabilities) != 3
        or not all(math.isfinite(float(p)) and 0 <= p <= 1 for p in probabilities)
        or abs(sum(probabilities) - 1) > 1e-6
    ):
        raise ValueError("Invalid three-class probabilities")
    if direction not in ("UP", "DOWN"):
        raise ValueError("Invalid direction")
    win, loss = (2, 0) if direction == "UP" else (0, 2)
    return float(probabilities[win] * payout / 100 - probabilities[loss])


def policies(examples, probabilities, config):
    records = []
    ml_last = {}
    baseline_last = {}
    for example, p in zip(examples, probabilities):
        direction = "UP" if p[2] >= p[0] else "DOWN"
        win_probability = float(p[2] if direction == "UP" else p[0])
        ev = expected_return(p, direction, example.payout)
        take = (
            win_probability >= config.probability_floor
            and ev is not None
            and ev >= config.min_expected_return
            and example.at - ml_last.get(example.symbol, float("-inf"))
            >= config.cooldown
        )
        baseline = example.baseline
        take_baseline = (
            baseline is not None
            and example.at - baseline_last.get(example.symbol, float("-inf"))
            >= config.cooldown
        )
        if take:
            ml_last[example.symbol] = example.at
        if take_baseline:
            baseline_last[example.symbol] = example.at

        def payoff(side):
            return (
                0.0
                if example.label == 1
                else example.payout / 100
                if (side == "UP") == (example.label == 2)
                else -1.0
            )

        records.append(
            dict(
                at=example.at,
                symbol=example.symbol,
                label=example.label,
                payout=example.payout,
                direction=direction,
                probability=win_probability,
                expected_return=ev,
                ml_trade=bool(take),
                ml_return=payoff(direction) if take else 0.0,
                baseline_trade=bool(take_baseline),
                baseline_return=payoff(baseline) if take_baseline else 0.0,
            )
        )
    return records


def calibration_metrics(examples, probabilities):
    import numpy as np

    if not examples:
        return dict(brier=None, log_loss=None, ece=None, reliability=[])
    p = np.asarray(probabilities, dtype=float)
    y = np.asarray([e.label for e in examples])
    target = np.eye(3)[y]
    brier = float(np.mean(np.sum((p - target) ** 2, axis=1)))
    log_loss = float(-np.mean(np.log(np.clip(p[np.arange(len(y)), y], 1e-9, 1))))
    # Calibrate the actionable UP/DOWN choice, not the most probable DRAW class.
    chosen = np.where(p[:, 2] >= p[:, 0], 2, 0)
    confidence = p[np.arange(len(y)), chosen]
    correct = chosen == y
    ece = 0.0
    reliability = []
    for lower in np.arange(0, 1, 0.1):
        upper = min(1, float(lower + 0.1))
        mask = (confidence >= lower) & (
            confidence < upper if upper < 1 else confidence <= 1
        )
        count = int(mask.sum())
        if count:
            predicted = float(confidence[mask].mean())
            observed = float(correct[mask].mean())
            ece += count / len(y) * abs(predicted - observed)
            reliability.append(
                dict(
                    lower=float(lower),
                    upper=upper,
                    n=count,
                    predicted=predicted,
                    observed=observed,
                )
            )
    return dict(brier=brier, log_loss=log_loss, ece=float(ece), reliability=reliability)


def summary(records, key):
    trades = [r for r in records if r[key + "_trade"]]
    returns = [r[key + "_return"] for r in trades]
    equity = peak = drawdown = 0.0
    for value in returns:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return dict(
        opportunities=len(records),
        trades=len(trades),
        net_units=sum(returns),
        return_per_trade=sum(returns) / len(trades) if trades else None,
        return_per_opportunity=sum(returns) / len(records) if records else None,
        wins=sum(v > 0 for v in returns),
        losses=sum(v < 0 for v in returns),
        draws=sum(v == 0 for v in returns),
        win_rate=sum(v > 0 for v in returns) / len(trades) if trades else None,
        max_drawdown_units=drawdown,
    )


def block_intervals(records, config):
    import numpy as np

    groups = defaultdict(lambda: [0.0, 0.0, 0.0, 0.0])
    for r in records:
        group = groups[r["at"] // 86400]
        group[0] += r["ml_return"]
        group[1] += r["baseline_return"]
        group[2] += int(r["ml_trade"])
        group[3] += 1
    days = len(groups)
    if days < 2:
        return dict(
            days=days,
            ml_return_per_trade_95=None,
            paired_improvement_per_opportunity_95=None,
        )
    blocks = np.asarray(list(groups.values()))
    rng = np.random.default_rng(config.seed)
    ev = []
    differences = []
    for _ in range(config.bootstrap_replicates):
        sampled = blocks[rng.integers(0, days, size=days)].sum(axis=0)
        if sampled[2] > 0:
            ev.append(sampled[0] / sampled[2])
        differences.append((sampled[0] - sampled[1]) / sampled[3])

    def interval(values):
        return np.quantile(values, [0.025, 0.975]).tolist() if values else None

    return dict(
        days=days,
        ml_return_per_trade_95=interval(ev),
        paired_improvement_per_opportunity_95=interval(differences),
    )


def evaluate(examples, model, training, config):
    import numpy as np

    probabilities = model.predict(examples)
    raw = model.raw(examples)
    records = policies(examples, probabilities, config)
    frequencies = np.bincount([e.label for e in training], minlength=3) / len(training)
    benchmark = np.tile(frequencies, (len(examples), 1))
    selected_indices = [i for i, r in enumerate(records) if r["ml_trade"]]
    selected = [examples[i] for i in selected_indices]
    return dict(
        samples=len(examples),
        selected_calibration=calibration_metrics(
            selected, probabilities[selected_indices]
        ),
        raw_calibration=calibration_metrics(examples, raw),
        calibration=calibration_metrics(examples, probabilities),
        climatology=calibration_metrics(examples, benchmark),
        ml=summary(records, "ml"),
        baseline=summary(records, "baseline"),
        intervals=block_intervals(records, config),
    ), records


def gate(evaluation, model, config):
    reasons = []
    if min(model.calibration_counts) < config.min_class_count:
        reasons.append("insufficient_class_support_in_calibration")
    if evaluation["ml"]["trades"] < config.min_trades:
        reasons.append("insufficient_trades")
    if evaluation["intervals"]["days"] < config.min_days:
        reasons.append("insufficient_independent_day_blocks")
    ece = evaluation["calibration"]["ece"]
    if ece is None or ece > config.max_ece:
        reasons.append("calibration_not_confirmed")
    selected_ece = evaluation.get("selected_calibration", {}).get("ece")
    if selected_ece is None or selected_ece > config.max_ece:
        reasons.append("selected_signal_calibration_not_confirmed")
    brier = evaluation["calibration"]["brier"]
    reference = evaluation["climatology"]["brier"]
    if brier is None or reference is None or brier >= reference:
        reasons.append("not_better_than_climatology")
    ev = evaluation["intervals"]["ml_return_per_trade_95"]
    improvement = evaluation["intervals"]["paired_improvement_per_opportunity_95"]
    if ev is None or ev[0] <= 0:
        reasons.append("positive_return_not_confirmed")
    if improvement is None or improvement[0] <= 0:
        reasons.append("improvement_over_baseline_not_confirmed")
    return dict(passed=not reasons, reasons=reasons)
