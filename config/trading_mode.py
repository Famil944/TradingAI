import os
from enum import Enum
from pathlib import Path

from dotenv import load_dotenv


load_dotenv(Path(__file__).resolve().parent.parent / ".env")


class TradingMode(Enum):
    SAFE = "SAFE"
    PAPER = "PAPER"
    DEMO = "DEMO"
    LIVE = "LIVE"


def _read_mode():
    state_path = os.getenv("TRADING_MODE_STATE_PATH")
    value = None
    if state_path:
        path = Path(state_path)
        if path.exists():
            value = path.read_text(encoding="utf-8").strip()
    value = (value or os.getenv(
        "TRADING_MODE",
        TradingMode.DEMO.value,
    )).upper()
    try:
        return TradingMode(value)
    except ValueError as error:
        allowed = ", ".join(mode.value for mode in TradingMode)
        raise RuntimeError(
            f"Некорректный TRADING_MODE={value!r}. Допустимо: {allowed}"
        ) from error


CURRENT_MODE = _read_mode()


def set_current_mode(mode):
    """Update the mode used by the running process after a validated switch."""
    global CURRENT_MODE
    if not isinstance(mode, TradingMode):
        mode = TradingMode(str(mode).upper())
    CURRENT_MODE = mode
    os.environ["TRADING_MODE"] = mode.value
    return CURRENT_MODE

# Deliberately requires an explicit phrase, not a truthy-looking value.
LIVE_TRADING_ENABLED = (
    os.getenv("LIVE_TRADING_CONFIRMATION") == "I_UNDERSTAND_LIVE_RISK"
)

NEW_POSITIONS_ENABLED = (
    os.getenv("TRADING_CLOSE_ONLY", "").lower()
    not in {"1", "true", "yes", "on"}
)
