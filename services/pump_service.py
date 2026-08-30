import asyncio
import json
import logging
import statistics
from datetime import datetime, timedelta, timezone

from analysis.signal_scorer import SignalScorer
from config.settings import settings
from database.db import Database
from exchange.binance_client import BinanceClient
from core.scan_coordinator import market_scan_lock


logger = logging.getLogger(__name__)
CHECKPOINTS_MINUTES = (5, 15, 30, 60, 180, 360, 720, 1440)


class PumpScanner:
    def __init__(self, news_service):
        self.news_service = news_service
        self.last_diagnostics = {}

    @staticmethod
    def _ratio(candles, field="volume", recent=3, baseline=20):
        if len(candles) < recent + baseline:
            return None
        new = [getattr(item, field) or 0 for item in candles[-recent:]]
        old = [getattr(item, field) or 0 for item in candles[-recent-baseline:-recent]]
        average = sum(old) / len(old)
        return (sum(new) / len(new)) / average if average > 0 else None

    @staticmethod
    def _buy_ratio(candles, count=3):
        recent = candles[-count:]
        total = sum(item.volume for item in recent)
        bought = sum(item.taker_buy_quote_volume or 0 for item in recent)
        return bought / total if total > 0 else None

    @staticmethod
    def _band_width(closes):
        if len(closes) < 20:
            return None
        sample = closes[-20:]
        mean = statistics.fmean(sample)
        return statistics.pstdev(sample) / mean * 100 if mean else None

    async def _analyze(self, client, ticker):
        symbol = ticker["symbol"]
        bid, ask = ticker["bid"], ticker["ask"]
        spread = ((ask - bid) / ((ask + bid) / 2) * 100) if bid > 0 and ask >= bid else 99
        if ticker["quote_volume"] < settings.pump_min_quote_volume_usdt:
            return None, "low_liquidity"
        if spread > 0.30:
            return None, "wide_spread"
        one, five, fifteen = await asyncio.gather(
            client.get_klines(symbol, "1m", 65),
            client.get_klines(symbol, "5m", 65),
            client.get_klines(symbol, "15m", 65),
        )
        if min(len(one), len(five), len(fifteen)) < 40:
            return None, "candles_unavailable"
        one, five, fifteen = one[:-1], five[:-1], fifteen[:-1]
        volume_1m = self._ratio(one)
        volume_5m = self._ratio(five)
        trades_ratio = self._ratio(one, "trade_count")
        buy_ratio = self._buy_ratio(one)
        closes = [item.close for item in five]
        current_width = self._band_width(closes)
        previous_width = self._band_width(closes[:-10])
        squeeze_ratio = (
            current_width / previous_width
            if current_width is not None and previous_width else None
        )
        resistance = max(item.high for item in five[-21:-1])
        breakout_percent = (ticker["price"] / resistance - 1) * 100 if resistance else 0
        ema_5m = SignalScorer.ema_slope_percent(five)
        ema_15m = SignalScorer.ema_slope_percent(fifteen)
        hour_move = (one[-1].close / one[-61].close - 1) * 100 if len(one) >= 61 else 0

        score, reasons = 10, []
        if volume_1m and volume_1m >= 1.5:
            score += 20 + (10 if volume_1m >= 2.5 else 0)
            reasons.append(f"объём 1m x{volume_1m:.1f}")
        if volume_5m and volume_5m >= 1.3:
            score += 15
            reasons.append(f"объём 5m x{volume_5m:.1f}")
        if buy_ratio and buy_ratio >= 0.55:
            score += 15
            reasons.append(f"покупатели {buy_ratio * 100:.0f}%")
        if trades_ratio and trades_ratio >= 1.5:
            score += 10
            reasons.append(f"сделок x{trades_ratio:.1f}")
        if squeeze_ratio and squeeze_ratio <= 0.75:
            score += 10
            reasons.append("сжатие волатильности")
        if breakout_percent >= 0:
            score += 15
            reasons.append("пробой локального максимума")
        elif breakout_percent >= -1:
            score += 8
            reasons.append("рядом с сопротивлением")
        if max(ema_5m or -99, ema_15m or -99) > 0.05:
            score += 10
            reasons.append("EMA ускоряется")
        if ticker["quote_volume"] >= 10_000_000:
            score += 5

        if hour_move >= 8:
            stage = "late"
            score -= 30
            reasons.append(f"уже выросла на {hour_move:.1f}%")
        elif breakout_percent >= 0 and (volume_1m or 0) >= 1.5:
            stage = "confirmed"
        elif (volume_1m or 0) >= 1.5 or (trades_ratio or 0) >= 1.5:
            stage = "impulse"
        else:
            stage = "preparation"

        metrics = {
            "quote_volume_usdt": ticker["quote_volume"], "spread_percent": spread,
            "volume_ratio_1m": volume_1m, "volume_ratio_5m": volume_5m,
            "trade_count_ratio": trades_ratio, "buyer_ratio": buy_ratio,
            "squeeze_ratio": squeeze_ratio, "breakout_percent": breakout_percent,
            "ema_slope_5m": ema_5m, "ema_slope_15m": ema_15m,
            "hour_move_percent": hour_move, "reasons": reasons,
        }
        # Новости могут только понизить риск/приоритет уже сильного
        # технического кандидата, но не создать Pump-сигнал самостоятельно.
        if score < settings.pump_min_score:
            return None, "pump_score_low"
        news = await self.news_service.assess(symbol)
        if news.available:
            score += max(-30, min(10, news.score // 5))
            if news.critical_risk:
                score -= 30
        score = max(0, min(100, score))
        if score < settings.pump_min_score:
            return None, "pump_rejected_by_news"
        return {
            "symbol": symbol, "price": ticker["price"], "score": score,
            "stage": stage, "metrics": metrics,
            "news_score": news.score if news.available else 0,
            "news_items": news.relevant_items if news.available else 0,
            "news_critical": news.critical_risk if news.available else False,
        }, "candidate"

    async def scan(self, progress=None):
        async with BinanceClient() as client:
            tickers = await client.get_all_usdt_tickers()
            semaphore = asyncio.Semaphore(settings.scanner_concurrency)
            reasons, candidates = {}, []

            async def analyze(item):
                async with semaphore:
                    try:
                        return await self._analyze(client, item)
                    except Exception:
                        logger.exception("Pump analysis failed for %s", item["symbol"])
                        return None, "analysis_error"

            tasks = [asyncio.create_task(analyze(item)) for item in tickers]
            for index, task in enumerate(asyncio.as_completed(tasks), 1):
                candidate, reason = await task
                reasons[reason] = reasons.get(reason, 0) + 1
                if candidate:
                    candidates.append(candidate)
                if progress and (index % 25 == 0 or index == len(tasks)):
                    await progress(index, len(tasks), len(candidates))
        candidates.sort(key=lambda item: item["score"], reverse=True)
        self.last_diagnostics = {
            "checked": len(tickers), "candidates": len(candidates), "reasons": reasons,
        }
        return candidates


class PumpService:
    def __init__(self, bot, news_service):
        self.bot = bot
        self.db = Database()
        self.scanner = PumpScanner(news_service)
        self.lock = market_scan_lock
        self._stop = asyncio.Event()
        self._last_background_scan = None

    def stop(self):
        self._stop.set()

    async def _send_message_safe(self, user_id, text):
        try:
            await self.bot.send_message(user_id, text)
            return True
        except Exception:
            logger.exception("Cannot notify Pump user %s", user_id)
            return False

    async def scan_for_user(self, user_id, progress=None):
        async with self.lock:
            candidates = await self.scanner.scan(progress)
        saved = []
        for candidate in candidates:
            prediction_id = self.db.save_pump_prediction(user_id, candidate)
            if prediction_id:
                saved.append((prediction_id, candidate))
        return candidates, saved

    async def run(self):
        while not self._stop.is_set():
            try:
                await self.monitor_predictions()
                users = self.db.get_pump_background_users()
                now = datetime.now(timezone.utc)
                due = (not self._last_background_scan or now - self._last_background_scan >=
                       timedelta(minutes=settings.pump_scan_interval_minutes))
                if users and due and not self.lock.locked():
                    async with self.lock:
                        candidates = await self.scanner.scan()
                    self._last_background_scan = now
                    for user_id in users:
                        new_predictions = []
                        for candidate in candidates:
                            prediction_id = self.db.save_pump_prediction(user_id, candidate)
                            if prediction_id:
                                new_predictions.append((prediction_id, candidate))
                        for prediction_id, candidate in new_predictions[:5]:
                            await self._send_message_safe(
                                user_id, self.format_candidate(candidate, prediction_id)
                            )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Pump background service failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=60)
            except asyncio.TimeoutError:
                pass

    async def monitor_predictions(self):
        predictions = self.db.get_pump_predictions(status="observing", limit=500)
        if not predictions:
            return
        async with BinanceClient() as client:
            for row in predictions:
                ticker, candles = await asyncio.gather(
                    client.get_ticker(row["symbol"]),
                    client.get_klines(row["symbol"], "5m", 300),
                )
                if not ticker:
                    continue
                detected = datetime.fromisoformat(str(row["detected_at"])).replace(tzinfo=timezone.utc)
                relevant = [item for item in candles if item.timestamp / 1000 >= detected.timestamp()]
                prices_high = [item.high for item in relevant] + [ticker["price"]]
                prices_low = [item.low for item in relevant] + [ticker["price"]]
                maximum = max(float(row["max_price"]), *prices_high)
                minimum = min(float(row["min_price"]), *prices_low)
                max_at = row.get("max_at")
                min_at = row.get("min_at")
                for candle in relevant:
                    stamp = datetime.fromtimestamp(candle.timestamp / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                    if candle.high >= maximum:
                        maximum, max_at = candle.high, stamp
                    if candle.low <= minimum:
                        minimum, min_at = candle.low, stamp
                if ticker["price"] >= maximum:
                    maximum = ticker["price"]
                    max_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                if ticker["price"] <= minimum:
                    minimum = ticker["price"]
                    min_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                elapsed = (datetime.now(timezone.utc) - detected).total_seconds() / 60
                checkpoints = json.loads(row.get("checkpoints_json") or "{}")
                for minutes in CHECKPOINTS_MINUTES:
                    key = str(minutes)
                    if elapsed >= minutes and key not in checkpoints:
                        target = detected.timestamp() + minutes * 60
                        closest = min(relevant, key=lambda item: abs(item.timestamp / 1000 - target), default=None)
                        value = closest.close if closest else ticker["price"]
                        checkpoints[key] = (value / row["start_price"] - 1) * 100
                max_gain = (maximum / row["start_price"] - 1) * 100
                success = max_gain >= settings.pump_success_percent
                expired = elapsed >= settings.pump_observation_hours * 60
                updates = {
                    "current_price": ticker["price"], "max_price": maximum,
                    "min_price": minimum, "checkpoints_json": json.dumps(checkpoints),
                    "max_at": max_at, "min_at": min_at,
                    "last_checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                }
                if success and not row["result_notified"]:
                    notified = await self._send_message_safe(
                        row["user_id"],
                        f"✅ Pump-прогноз #{row['id']} подтвердился\n"
                        f"{row['symbol']} · максимальный рост {max_gain:+.2f}%"
                    )
                    if notified:
                        updates["result_notified"] = 1
                if expired:
                    updates.update(
                        status="completed", outcome="pump" if success else "no_pump",
                        completed_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                    )
                    if not success:
                        await self._send_message_safe(
                            row["user_id"],
                            f"❌ Pump-прогноз #{row['id']} не подтвердился за "
                            f"{settings.pump_observation_hours} ч.\n{row['symbol']} · максимум {max_gain:+.2f}%"
                        )
                self.db.update_pump_prediction(row["id"], **updates)

    @staticmethod
    def format_candidate(candidate, prediction_id=None):
        stages = {
            "preparation": "🟡 Подготовка", "impulse": "🟠 Начало импульса",
            "confirmed": "🟢 Пробой подтверждён", "late": "🔴 Движение уже началось",
        }
        reasons = ", ".join(candidate["metrics"].get("reasons", [])[:4]) or "совокупность факторов"
        number = f"#{prediction_id} · " if prediction_id else ""
        return (
            f"{stages[candidate['stage']]}\n\n{number}{candidate['symbol']} · "
            f"Pump Score {candidate['score']}/100\nЦена наблюдения: ${candidate['price']:g}\n"
            f"Признаки: {reasons}\n📰 Новостей по монете: {candidate['news_items']}\n"
            "Это экспериментальный прогноз, не торговый сигнал."
        )
