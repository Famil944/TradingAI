import json
import platform
from collections import Counter
from datetime import datetime, timezone

from config.settings import settings


def build_diagnostic_json(scan_diagnostics: dict, trades: list[dict]) -> bytes:
    """Создаёт безопасный отчёт без токенов, ключей и переменных окружения."""
    safe_trades = []
    for item in trades:
        entry = item.get("entry_price") or 0
        current = item.get("close_price") or item.get("current_price") or entry
        maximum = item.get("max_price") or current
        minimum = item.get("min_price") or current
        safe_trades.append({
            "id": item.get("id"),
            "symbol": item.get("symbol"),
            "score": item.get("score"),
            "status": item.get("status"),
            "entry_price": item.get("entry_price"),
            "current_price": item.get("current_price"),
            "target_price": item.get("target_price"),
            "position_usdt": item.get("position_usdt"),
            "opened_at": item.get("opened_at"),
            "closed_at": item.get("closed_at"),
            "close_price": item.get("close_price"),
            "close_reason": item.get("close_reason"),
            "result_percent": ((current - entry) / entry * 100) if entry else None,
            "max_favorable_percent": ((maximum - entry) / entry * 100) if entry else None,
            "max_adverse_percent": ((minimum - entry) / entry * 100) if entry else None,
        })
    symbols = (scan_diagnostics or {}).get("symbols", [])
    reason_counts = Counter(item.get("reason", "unknown") for item in symbols)
    report = {
        "report_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "runtime": {"python": platform.python_version(), "system": platform.system()},
        "strategy": {
            "target_percent": 3,
            "min_quote_volume_usdt": settings.min_quote_volume_usdt,
            "max_spread_percent": settings.max_spread_percent,
            "min_volume_ratio": settings.min_volume_ratio,
            "min_ema20_slope_percent": settings.min_ema20_slope_percent,
            "max_short_pump_percent": settings.max_short_pump_percent,
            "min_resistance_room_percent": settings.min_resistance_room_percent,
            "min_listing_days": settings.min_listing_days,
            "min_signal_score": settings.min_signal_score,
        },
        "last_scan": scan_diagnostics or {"note": "scan_not_run_since_restart"},
        "filter_summary": dict(reason_counts),
        "trades": safe_trades,
        "privacy": "No Telegram token, API key, environment variable or chat id is included.",
    }
    return json.dumps(report, ensure_ascii=False, indent=2, default=str).encode("utf-8")
