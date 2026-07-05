from paper.telegram_event_listener import TelegramEventListener


def setup_trade_events(paper, notifier):
    listener = TelegramEventListener(notifier)
    paper.engine.dispatcher.register(listener)