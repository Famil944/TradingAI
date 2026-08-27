import os
from pathlib import Path

from dotenv import dotenv_values

from config.trading_mode import TradingMode


class TradingModeSwitcher:
    """Validate and persist the mode selected in the Telegram UI."""

    def __init__(self, env_path=None, client_factory=None):
        project_root = Path(__file__).resolve().parent.parent
        self.env_path = Path(env_path or project_root / ".env")
        self.client_factory = client_factory or self._create_target_client
        state_path = os.getenv("TRADING_MODE_STATE_PATH")
        self.state_path = Path(state_path) if state_path else None

    def switch(self, target_mode, current_client):
        if not isinstance(target_mode, TradingMode):
            target_mode = TradingMode(str(target_mode).upper())
        if target_mode not in {TradingMode.DEMO, TradingMode.LIVE}:
            raise ValueError("Через интерфейс доступны только DEMO и LIVE")

        positions = current_client.open_positions()
        if positions:
            symbols = ", ".join(
                position.get("symbol", "?") for position in positions
            )
            raise PermissionError(
                f"Сначала закройте позиции текущего режима: {symbols}"
            )

        values = {
            **dotenv_values(self.env_path),
            **os.environ,
        }
        self._validate_credentials(target_mode, values)

        target_client = self.client_factory(target_mode)
        health = target_client.health_check()
        self._set_env_value("TRADING_MODE", target_mode.value)
        if self.state_path:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.state_path.with_suffix(".tmp")
            temporary.write_text(target_mode.value, encoding="utf-8")
            os.replace(temporary, self.state_path)

        return {
            "mode": target_mode.value,
            "health": health,
            "client": target_client,
            "restart_required": False,
        }

    @staticmethod
    def _validate_credentials(target_mode, values):
        if target_mode == TradingMode.DEMO:
            required = (
                "BINANCE_TESTNET_API_KEY",
                "BINANCE_TESTNET_API_SECRET",
            )
        else:
            required = (
                "BINANCE_LIVE_API_KEY",
                "BINANCE_LIVE_API_SECRET",
            )
            if (
                values.get("LIVE_TRADING_CONFIRMATION")
                != "I_UNDERSTAND_LIVE_RISK"
            ):
                raise PermissionError(
                    "Live не подтверждён в .env"
                )

        missing = [key for key in required if not values.get(key)]
        if missing:
            raise RuntimeError(
                "Не заполнены настройки: " + ", ".join(missing)
            )

    @staticmethod
    def _create_target_client(target_mode):
        if target_mode == TradingMode.DEMO:
            from exchange.binance_testnet_client import BinanceTestnetClient

            return BinanceTestnetClient()

        from exchange.binance_live_client import BinanceLiveClient

        return BinanceLiveClient()

    def _set_env_value(self, key, value):
        lines = (
            self.env_path.read_text(encoding="utf-8-sig").splitlines()
            if self.env_path.exists()
            else []
        )
        prefix = f"{key}="
        output = []
        replaced = False
        for line in lines:
            if line.strip().startswith(prefix):
                output.append(f"{key}={value}")
                replaced = True
            else:
                output.append(line)
        if not replaced:
            output.append(f"{key}={value}")

        temporary = self.env_path.with_suffix(".env.tmp")
        temporary.write_text(
            "\n".join(output) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.env_path)
