from config.trading_mode import (
    CURRENT_MODE,
    LIVE_TRADING_ENABLED,
    NEW_POSITIONS_ENABLED,
)
from exchange.trading_client_factory import create_trading_client


def main():
    client = create_trading_client()
    health = client.health_check()
    btc_rules = client.symbol_rules("BTCUSDT")

    print(f"Trading mode: {CURRENT_MODE.value}")
    print(f"New positions: {NEW_POSITIONS_ENABLED}")
    print(f"Live unlocked: {LIVE_TRADING_ENABLED}")
    print(f"Binance health: {health}")
    print(
        "BTCUSDT rules: "
        f"tick={btc_rules.tick_size}, "
        f"step={btc_rules.step_size}, "
        f"min_notional={btc_rules.min_notional}"
    )
    print("Preflight completed. No orders were submitted.")


if __name__ == "__main__":
    main()
