import asyncio

from paper.trade_events import TradeEvent


class TelegramEventListener:

    def __init__(self, notifier):
        self.notifier = notifier

    def call(self, event):
        text = self._build_text(event)

        if not text:
            return

        print(text)

        try:
            asyncio.create_task(self.notifier.send(text))
        except RuntimeError:
            pass

    def _build_text(self, event):
        data = event.data

        if event.event_type == TradeEvent.PARTIAL_TAKE_PROFIT:
            return (
                f"📈 Частичная фиксация прибыли\n\n"
                f"Монета: {data['symbol']}\n"
                f"Прибыль: {data['profit']:.2f} USDT\n"
                f"Закрыто объёма: {data['close_amount']}\n"
                f"Цена: {data['price']}"
            )

        if event.event_type == TradeEvent.BREAK_EVEN:
            return (
                f"🟢 Stop Loss перенесён в безубыток\n\n"
                f"Монета: {data['symbol']}\n"
                f"Новый SL: {data['stop_loss']}"
            )

        if event.event_type == TradeEvent.TRAILING_STOP:
            return (
                f"📈 Trailing Stop обновлён\n\n"
                f"Монета: {data['symbol']}\n"
                f"Новый SL: {data['stop_loss']}"
            )

        if event.event_type == TradeEvent.POSITION_CLOSED:
            return (
                f"🏁 Сделка закрыта\n\n"
                f"Монета: {data['symbol']}\n"
                f"Прибыль: {data['profit']:.2f} USDT"
            )

        return None