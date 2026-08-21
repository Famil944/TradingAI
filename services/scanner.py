import asyncio
import logging
from decimal import Decimal, ROUND_DOWN
from typing import List
from datetime import datetime, timedelta
from exchange.binance_client import BinanceClient
from config.models import MarketData
from analysis.signal_scorer import SignalScorer
from database.db import Database
from config.settings import settings

logger = logging.getLogger(__name__)


class MarketScanner:
    """Фоновый сканер рынка для поиска торговых сигналов."""
    
    def __init__(self, signal_callback=None):
        self.db = Database()
        self.running = False
        self._stop_event = asyncio.Event()
        self.signal_callback = signal_callback
        self.last_scan_diagnostics = {}

    async def _analyze_symbol(self, client, symbol: str):
        candles_5m, candles_15m, candles_1h, candles_4h, daily, ticker, tick_size = await asyncio.gather(
            client.get_klines(symbol, "5m", limit=121),
            client.get_klines(symbol, "15m", limit=121),
            client.get_klines(symbol, "1h", limit=121),
            client.get_klines(symbol, "4h", limit=121),
            client.get_klines(symbol, "1d", limit=settings.min_listing_days + 1),
            client.get_ticker(symbol),
            client.get_tick_size(symbol),
        )
        if not ticker or any(not series for series in (
            candles_5m, candles_15m, candles_1h, candles_4h, daily
        )):
            return None
        # Binance включает текущую, ещё формирующуюся свечу. Она исключается,
        # чтобы сигнал не менялся после закрытия интервала.
        candles_5m = candles_5m[:-1]
        candles_15m = candles_15m[:-1]
        candles_1h = candles_1h[:-1]
        candles_4h = candles_4h[:-1]
        closed_daily = daily[:-1]
        if len(closed_daily) < settings.min_listing_days:
            logger.debug(
                "%s исключён: история %s дней меньше %s",
                symbol, len(closed_daily), settings.min_listing_days,
            )
            return None
        if ticker.get("quote_asset_volume", 0) < settings.min_quote_volume_usdt:
            return None

        market_data = MarketData(
            symbol=symbol,
            current_price=ticker["price"],
            price_change_24h=ticker["price_change"],
            price_change_percent_24h=ticker["price_change_percent"],
        )
        signal = SignalScorer.generate_signal(
            symbol,
            market_data,
            candles_5m,
            min_score=settings.min_signal_score,
            candles_15m=candles_15m,
            candles_1h=candles_1h,
            candles_4h=candles_4h,
            min_drawdown_percent=settings.min_drawdown_percent,
            max_drawdown_percent=settings.max_drawdown_percent,
        )
        if signal:
            if tick_size:
                tick = Decimal(str(tick_size))

                def normalize(value):
                    return float((Decimal(str(value)) / tick).to_integral_value(
                        rounding=ROUND_DOWN
                    ) * tick)

                signal.tick_size = tick_size
                signal.entry_zone_min = normalize(signal.entry_zone_min)
                signal.entry_zone_max = normalize(signal.entry_zone_max)
                signal.targets.tp1 = normalize(signal.targets.tp1)
                signal.targets.tp2 = normalize(signal.targets.tp2)
                signal.targets.tp3 = normalize(signal.targets.tp3)
                signal.targets.tp4 = normalize(signal.targets.tp4)
                signal.stop_loss = normalize(signal.stop_loss)
            signal.reasons.append(
                f"✅ История торгов не менее {settings.min_listing_days} дней"
            )
        return signal

    async def analyze_symbol(self, symbol: str):
        """Публичный анализ одной Binance Spot пары без сохранения в БД."""
        normalized = symbol.upper().replace("/", "")
        if not normalized.endswith("USDT"):
            normalized += "USDT"
        async with BinanceClient() as client:
            return await self._analyze_symbol(client, normalized)
    
    async def scan_market(
        self, top_limit: int = None, respect_cooldown: bool = True,
        progress_callback=None,
    ) -> List[dict]:
        """
        Выполнить скан рынка.
        
        Returns:
            Список найденных сигналов
        """
        top_limit = top_limit or settings.scanner_top_limit
        logger.info(f"Начало сканирования TOP-{top_limit} пар...")
        signals_found = []
        
        # Популярные пары для тестирования
        test_symbols = [
            "BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT", "ADAUSDT",
            "DOGEUSDT", "LINKUSDT", "LTCUSDT", "UNIUSDT", "SHIBUSDT",
            "MATICUSDT", "TRXUSDT", "AVAXUSDT", "ARBUSDT", "OPUSDT",
            "SOLUSDT", "APTUSDT", "ORDIUSDT", "NOTUSDT", "WIFUSDT"
        ]
        
        async with BinanceClient() as client:
            try:
                # Сначала пробуем получить TOP пары
                symbols = await client.get_top_symbols(top_limit)
                
                # Если пусто или ошибка, используем тестовые пары
                if not symbols:
                    logger.warning("Не удалось получить TOP пары, используем тестовый список")
                    symbols = test_symbols[:top_limit]
                
                semaphore = asyncio.Semaphore(settings.scanner_concurrency)

                async def analyze(symbol):
                    async with semaphore:
                        try:
                            return symbol, await self._analyze_symbol(client, symbol)
                        except Exception as error:
                            logger.error(f"Ошибка при анализе {symbol}: {error}")
                            return symbol, None

                tasks = [asyncio.create_task(analyze(symbol)) for symbol in symbols]
                analyzed = []
                for completed, task in enumerate(asyncio.as_completed(tasks), 1):
                    analyzed.append(await task)
                    if progress_callback and (completed % 10 == 0 or completed == len(tasks)):
                        found_so_far = sum(item[1] is not None for item in analyzed)
                        await progress_callback(completed, len(tasks), found_so_far)
                # Сначала лучшие сигналы, чтобы /signals сразу показывал качество.
                analyzed.sort(key=lambda pair: pair[1].score if pair[1] else -1, reverse=True)
                for symbol, signal in analyzed:
                    try:
                        if signal:
                            # Проверяем cooldown
                            should_send = (
                                await self._check_cooldown(symbol, signal.score)
                                if respect_cooldown else True
                            )
                            
                            if should_send:
                                # Сохраняем сигнал в БД
                                signal_dict = {
                                    "symbol": signal.symbol,
                                    "score": signal.score,
                                    "entry_price": signal.current_price,
                                    "entry_zone_min": signal.entry_zone_min,
                                    "entry_zone_max": signal.entry_zone_max,
                                    "tp1": signal.targets.tp1,
                                    "tp2": signal.targets.tp2,
                                    "tp3": signal.targets.tp3,
                                    "tp4": signal.targets.tp4,
                                    "stop_loss": signal.stop_loss,
                                    "stop_loss_percent": signal.stop_loss_percent,
                                    "support": signal.support,
                                    "resistance": signal.resistance,
                                    "rsi_5m": signal.rsi_5m,
                                    "rsi_15m": signal.rsi_15m,
                                    "rsi_1h": signal.rsi_1h,
                                    "volume_change_percent": signal.volume_change_percent,
                                    "risk_reward": signal.risk_reward,
                                    "reasons": signal.reasons,
                                    "warnings": signal.warnings
                                }
                                
                                signal_id = self.db.save_signal(signal_dict)
                                self.db.update_cooldown(symbol, signal_id, signal.score)
                                
                                signals_found.append({
                                    "signal_id": signal_id,
                                    "signal_object": signal
                                })
                                
                                logger.info(f"✅ Сигнал найден: {symbol} Score={signal.score}")
                    
                    except Exception as e:
                        logger.error(f"Ошибка при анализе {symbol}: {e}")
                        continue
                
                logger.info(f"Скан завершён. Найдено сигналов: {len(signals_found)}")
                self.last_scan_diagnostics = {
                    "checked": len(symbols),
                    "accepted": len(signals_found),
                    "strategy_filtered": len(symbols) - sum(
                        signal is not None for _, signal in analyzed
                    ),
                }
            
            except Exception as e:
                logger.error(f"Критическая ошибка при сканировании: {e}")
                self.last_scan_diagnostics = {"error": str(e)}
        
        return signals_found
    
    async def _check_cooldown(self, symbol: str, new_score: int) -> bool:
        """
        Проверить, прошёл ли cooldown для символа.
        
        Returns:
            True если сигнал должен быть отправлен, False если в cooldown
        """
        cooldown_data = self.db.get_cooldown_signal(symbol)
        
        if not cooldown_data:
            # Нет предыдущего сигнала
            return True
        
        last_signal_id, last_score, sent_at = cooldown_data
        
        # Парсим время отправки
        try:
            sent_time = datetime.fromisoformat(sent_at)
        except:
            return True
        
        # Проверяем прошёл ли cooldown
        cooldown_minutes = settings.signal_cooldown_minutes
        if datetime.utcnow() - sent_time < timedelta(minutes=cooldown_minutes):
            # Проверяем изменение Score
            score_diff = abs(new_score - last_score)
            threshold = settings.signal_score_change_threshold
            
            if score_diff < threshold:
                logger.debug(f"{symbol}: в cooldown, изменение Score слишком мало ({score_diff})")
                return False
            else:
                logger.debug(f"{symbol}: cooldown, но Score изменился существенно ({score_diff})")
                return True
        
        return True
    
    async def start_background_scanner(self):
        """Запустить фоновый сканер на бесконечном цикле."""
        self.running = True
        self._stop_event.clear()
        scan_interval_minutes = settings.scan_interval_minutes
        
        logger.info(f"🚀 Фоновый сканер запущен. Интервал сканирования: {scan_interval_minutes} мин")
        
        while self.running:
            try:
                results = await self.scan_market()
                if self.signal_callback and results:
                    await self.signal_callback(results)
            except Exception as e:
                logger.error(f"Ошибка в фоновом сканере: {e}")
            
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=scan_interval_minutes * 60
                )
            except asyncio.TimeoutError:
                pass
    
    def stop(self):
        """Остановить фоновый сканер."""
        self.running = False
        self._stop_event.set()
        logger.info("Фоновый сканер остановлен")
