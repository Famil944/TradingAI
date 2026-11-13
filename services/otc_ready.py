"""Experimental primary signals from already completed candles, no future-colour condition."""

import asyncio
import logging
import math
import time
from dataclasses import asdict

from config.settings import settings
from services.otc_candidate_research import CandidateJournal, TickSampler
from services.otc_conditional import context_setup
from services.otc_confirmations import build
from services.otc_groq import fresh
from services.otc_ml.features import FEATURE_NAMES, indicators
from services.otc_ml.preview import MLPreview
from services.otc_m1_policy import evaluate as evaluate_m1, live_check as check_m1_tick
from services.otc_policy import evaluate as evaluate_policy
from services.otc_strategy import aggregate

VERSION = "primary-ready-v10-early-plan"
logger = logging.getLogger(__name__)


def select_ready_v5(
    histories, assets, quotes, now, seconds, end=None, audit=None, recent_ticks=None
):
    entry = int(now) // 60 * 60 + 60
    if (seconds not in (60, 300, 900) or entry % seconds != 0
            or not 15 <= entry - now <= 23):
        return None
    if end and entry + seconds + 60 > end:
        return None
    known_at = int(now) // 60 * 60
    candidates = []
    for symbol, bars in histories.items():
        asset = assets.get(symbol)
        record = dict(
            symbol=symbol,
            entry_at=entry,
            expires_at=entry + seconds,
            observed_at=now,
            feature_asof=known_at,
            version="primary-ready-v5",
            direction=None,
            payout=getattr(asset, "payout", None),
            payout_observed_at=getattr(asset, "observed_at", None),
            quote_at=quotes.get(symbol),
            decision_lead_seconds=entry - now,
            gates={},
            selected=False,
            reason="asset_or_quote_ineligible",
        )
        if audit is not None:
            audit.append(record)
        if not fresh(asset, quotes.get(symbol), now):
            continue
        past = [bar for bar in bars if bar.time + 60 <= known_at]
        setup = context_setup(past, known_at, seconds)
        if setup is None:
            record["reason"] = "no_context_or_insufficient_history"
            continue
        features = setup.evidence["features"]
        record.update(direction=setup.direction, evidence=setup.evidence)
        required = (
            "atr14",
            "adx14",
            "ema20_gap",
            "ema_spread",
            "macd_hist",
            "rsi14",
            "bb_position",
        )
        if any(
            not isinstance(features.get(k), (int, float))
            or not math.isfinite(features[k])
            for k in required
        ):
            record["reason"] = "invalid_features"
            continue
        atr = features["atr14"]
        if atr <= 0:
            record["reason"] = "invalid_atr"
            continue
        up = setup.direction == "UP"
        gates = dict(
            adx=features["adx14"] >= 0.20,
            ema_distance=abs(features["ema20_gap"]) / atr <= 1.5,
            macd=features["macd_hist"] > 0 if up else features["macd_hist"] < 0,
            rsi=features["rsi14"] <= 0.68 if up else features["rsi14"] >= 0.32,
            bollinger=features["bb_position"] <= 0.9
            if up
            else features["bb_position"] >= 0.1,
        )
        score = (
            features["adx14"]
            + 0.05 * abs(features["ema_spread"]) / atr
            - 0.05 * abs(features["ema20_gap"]) / atr
        )
        record.update(
            gates=gates,
            quality_score=score,
            regime="strong_trend" if features["adx14"] >= 0.25 else "weak_trend",
            reason="eligible" if all(gates.values()) else "filter_rejected",
        )
        # This live feature is recorded only; it does not alter the v5 decision.
        tick = (recent_ticks or {}).get(symbol)
        last = setup.evidence.get("last_closed_candle")
        if last and "time" in last:
            record["last_closed_at"] = last["time"] + seconds
            record["feature_age_at_entry_seconds"] = entry - record["last_closed_at"]
        if tick and last and 0 <= now - tick[1] <= 8 and tick[1] <= tick[2] <= now:
            movement = (tick[0] - last["close"]) / (atr * last["close"])
            record["micro_move_atr"] = movement if up else -movement
            record["micro_tick_at"] = tick[1]
            record["micro_received_at"] = tick[2]
        if not all(gates.values()):
            continue
        candidates.append(
            (
                score,
                symbol,
                dict(
                    symbol=symbol,
                    name=asset.name or symbol,
                    entry_at=entry,
                    expires_at=entry + seconds,
                    direction=setup.direction,
                    payout=asset.payout,
                    model_sha256="primary-ready-v5",
                    model_role="unqualified_primary_experiment",
                    feature_asof=known_at,
                    evidence=setup.evidence,
                    condition_required=False,
                    quality_score=score,
                ),
            )
        )
    selected = (
        max(candidates, key=lambda item: (item[0], item[1]))[2] if candidates else None
    )
    if selected and audit is not None:
        for record in audit:
            record["selected"] = record["symbol"] == selected["symbol"]
    return selected


def select_ready(
    histories, assets, quotes, now, seconds, end=None, audit=None, recent_ticks=None,
    min_confirmations=3, m1_strict_enabled=True, policy_enforced=False,
):
    if not isinstance(min_confirmations, int) or not 2 <= min_confirmations <= 5:
        raise ValueError("min_confirmations must be 2..5")
    entry = int(now) // 60 * 60 + 60
    if (seconds not in (60, 300, 900) or entry % seconds != 0
            or not 15 <= entry - now <= 23):
        return None
    if end and entry + seconds + 60 > end:
        return None
    asof = entry - 60
    candidates = []
    for symbol, minutes in histories.items():
        asset = assets.get(symbol)
        row = dict(
            version=VERSION,
            symbol=symbol,
            observed_at=now,
            feature_asof=asof,
            entry_at=entry,
            expires_at=entry + seconds,
            payout=getattr(asset, "payout", None),
            direction=None,
            selected=False,
            gates={},
            reason="asset_or_quote_ineligible",
            m1_baseline_eligible=False,
            m1_strict_eligible=False,
        )
        if audit is not None:
            audit.append(row)
        if not fresh(asset, quotes.get(symbol), now):
            continue
        up = build(minutes, "UP", seconds, asof)
        down = build(minutes, "DOWN", seconds, asof)
        if not up["available"]:
            row["reason"] = "insufficient_indicator_history"
            continue
        # Majority of observed directional votes; ties use a fixed indicator order.
        direction = "UP" if up["count"] > down["count"] else "DOWN"
        if up["count"] == down["count"]:
            for key in ("ema", "macd", "candle", "rsi", "alignment"):
                if up["checks"][key] in ("agree", "against"):
                    direction = "UP" if up["checks"][key] == "agree" else "DOWN"
                    break
        confirmations = up if direction == "UP" else down
        row.update(direction=direction, confirmation_count=confirmations["count"],
                   regime=confirmations.get("market_state", {}).get("state", "unknown"))
        bars = aggregate([b for b in minutes if b.time + 60 <= asof], seconds)[-250:]
        values = indicators(bars)
        if values is None:
            row["reason"] = "insufficient_indicator_history"
            continue
        features = dict(zip(FEATURE_NAMES, values))
        policy = evaluate_policy(bars, features, direction,
                                 confirmations.get("market_state", {}))
        row.update(
            policy=policy,
            policy_reason=policy["reason"],
            strategy=policy["strategy"],
            gates=dict(
                policy["gates"],
                min_confirmations=confirmations["count"] >= min_confirmations,
            ),
        )
        if confirmations["count"] < min_confirmations:
            row["reason"] = "insufficient_directional_confirmations"
            continue
        if policy_enforced and policy["reason"] != "eligible":
            row["reason"] = policy["reason"]
            continue
        # Historical v7 decision: keep this candidate for paired shadow
        # comparison even when the new M1 rule intentionally skips it.
        if seconds == 60:
            row["m1_baseline_eligible"] = True
            m1 = evaluate_m1(
                minutes, features, direction, confirmations.get("market_state", {}),
                asof, (recent_ticks or {}).get(symbol), now,
            )
            row["m1_policy"] = m1
            row["m1_strict_eligible"] = bool(m1["passed"])
            row["gates"].update(
                {"m1_" + key: passed for key, passed in m1["gates"].items()}
            )
            if m1_strict_enabled and not m1["passed"]:
                row["reason"] = m1["reason"]
                continue
        else:
            m1 = None
        evidence = dict(
            confirmations=confirmations,
            features=features,
            policy=policy,
            policy_enforced=bool(policy_enforced),
            m1_policy=m1,
            last_closed_candle=asdict(bars[-1]),
            advantage_confirmed=False,
        )
        row.update(
            direction=direction,
            evidence=evidence,
            reason="eligible",
            regime=confirmations.get("market_state", {}).get("state", "unknown"),
            gates=row["gates"],
            quality_score=confirmations["count"],
            confirmation_count=confirmations["count"],
        )
        candidates.append(
            dict(
                symbol=symbol,
                name=asset.name or symbol,
                entry_at=entry,
                expires_at=entry + seconds,
                direction=direction,
                payout=asset.payout,
                model_sha256=VERSION,
                model_role="unqualified_primary_experiment",
                feature_asof=asof,
                evidence=evidence,
                confirmations=confirmations,
                policy=policy,
                policy_enforced=bool(policy_enforced),
                m1_policy=m1,
                m1_strict_active=bool(seconds == 60 and m1_strict_enabled),
                condition_required=False,
                quality_score=confirmations["count"],
            )
        )
    selected = (
        max(candidates, key=lambda r: (r["quality_score"], r["symbol"]))
        if candidates
        else None
    )
    if selected and audit is not None:
        for row in audit:
            row["selected"] = row["symbol"] == selected["symbol"]
    return selected


def best_expirations(candidates):
    best = {}

    def rank(candidate):
        return (
            candidate.get("confirmations", {}).get(
                "count", candidate.get("quality_score", 0)
            ),
            -(candidate["expires_at"] - candidate["entry_at"]),
        )

    for candidate in candidates:
        key = (candidate["symbol"], candidate["entry_at"])
        if key not in best or rank(candidate) > rank(best[key]):
            best[key] = candidate
    return sorted(best.values(), key=rank, reverse=True)


class ReadyPrimary(MLPreview):
    label = "Основной · готовый"
    icon = "🔵"
    require_payout = True
    planning_window = (20, 23)

    def __init__(self, journal, clock=time.time, settlement_provider=None):
        super().__init__(journal, None, clock, settlement_provider)
        self.allowed_timeframes = (60, 300, 900)
        self.min_confirmations = settings.ready_min_confirmations
        self.m1_strict_enabled = settings.m1_strict_enabled
        self.policy_enforced = settings.primary_policy_enforced
        self.sent_count = 0
        self.last_reasons = {}
        self.last_attempt_at = None
        self.last_selected = None
        self.research = CandidateJournal(journal.path.with_name("ready-candidates.db"))
        self.sampler = TickSampler()
        self.recent_ticks = {}
        self.next_research_flush = 0

    def observe_tick(self, tick, received_at):
        if self.sampler.valid(tick, received_at):
            previous = self.recent_ticks.get(tick.symbol)
            if previous is None or tick.time > previous[1]:
                self.recent_ticks[tick.symbol] = (tick.price, tick.time, received_at)
            self.sampler.observe(tick, received_at)

    async def settle_pending(self, history):
        if self.clock() >= self.next_research_flush:
            self.next_research_flush = self.clock() + 5
            samples = self.sampler.drain(self.clock())
            if samples:
                try:
                    await asyncio.to_thread(self.research.samples, samples)
                except Exception:
                    self.sampler.restore(samples)
                    logger.exception("Candidate tick journal unavailable; will retry")
        await super().settle_pending(history)

    async def stop(self):
        await super().stop()
        samples = self.sampler.drain(self.clock())
        if samples:
            await asyncio.to_thread(self.research.samples, samples)

    async def publish(self, selected, bot, chat_id):
        # M1 changes within the still-forming minute; check actual latest
        # observed tick once more before committing a Telegram notification.
        if selected["expires_at"] - selected["entry_at"] == 60 and selected.get("m1_strict_active"):
            metrics = (selected.get("m1_policy") or {}).get("metrics", {})
            checked = check_m1_tick(
                metrics.get("m1_reference_close"),
                metrics.get("m1_atr_price"),
                selected["direction"],
                self.recent_ticks.get(selected["symbol"]),
                self.clock(),
            )
            if not checked["passed"]:
                logger.info("Strict M1 final tick veto: %s", checked["reason"])
                return False
            selected["m1_final_tick"] = checked
        return await super().publish(selected, bot, chat_id)

    async def _wait_for_delivery_window(self, entry_at):
        delay = entry_at - self.clock() - 20
        if delay > 0:
            await asyncio.sleep(delay)

    async def plan(self, bot, chat_id, snapshot):
        history, assets, quotes, end = snapshot
        histories = {s: list(b) for s, b in history.items()}
        offered = []
        for seconds in self.allowed_timeframes:
            now = self.clock()
            next_entry = int(now) // 60 * 60 + 60
            if next_entry % seconds:
                continue
            if not self.enabled or not 15 <= next_entry - now <= 23:
                continue
            unavailable = await asyncio.to_thread(self.journal.blocked, now, seconds)
            audit = []
            selected = await asyncio.to_thread(
                select_ready,
                histories,
                dict(assets),
                dict(quotes),
                now,
                seconds,
                end,
                audit,
                dict(self.recent_ticks),
                self.min_confirmations,
                self.m1_strict_enabled,
                self.policy_enforced,
            )
            for record in audit:
                record["operational_block"] = unavailable
            reason_counts = {}
            for record in audit:
                reason = record.get("reason", "unknown")
                reason_counts[reason] = reason_counts.get(reason, 0) + 1
            self.last_attempt_at = now
            self.last_reasons = reason_counts
            try:
                await asyncio.to_thread(self.research.record, audit)
                self.sampler.register(audit, self.clock())
            except Exception:
                logger.exception("Candidate research journal unavailable")
            if unavailable:
                continue
            if not selected or not self.enabled or not self.snapshot:
                if reason_counts:
                    logger.info(
                        "Primary skipped M%d: %s",
                        seconds // 60,
                        reason_counts,
                    )
                continue
            self.last_selected = selected["symbol"]
            offered.append(selected)
        for selected in best_expirations(offered):
            if not self.enabled or not self.snapshot:
                break
            await self._wait_for_delivery_window(selected["entry_at"])
            if not self.enabled or not 15 <= selected["entry_at"] - self.clock() <= 20:
                logger.info("Primary skipped: analysis missed delivery window")
                continue
            _, current_assets, current_quotes, current_end = self.snapshot
            asset = current_assets.get(selected["symbol"])
            if not fresh(asset, current_quotes.get(selected["symbol"]), self.clock()):
                continue
            if current_end and selected["expires_at"] + 60 > current_end:
                continue
            selected["payout"] = asset.payout
            if await self.publish(selected, bot, chat_id):
                self.sent_count += 1
