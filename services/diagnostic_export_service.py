import json
import platform
from collections import Counter
from datetime import datetime, timezone

from config.settings import settings


def build_diagnostic_json(
    scan_diagnostics: dict, trades: list[dict], database_id="unknown",
    service_state: dict = None,
) -> bytes:
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
            "target_price": item.get("tp1") or item.get("target_price"),
            "position_usdt": item.get("position_usdt"),
            "opened_at": item.get("opened_at"),
            "closed_at": item.get("closed_at"),
            "close_price": item.get("close_price"),
            "close_reason": item.get("close_reason"),
            "pending_reason": item.get("pending_reason"),
            "pending_since": item.get("pending_at"),
            "last_checked_at": item.get("last_checked_at"),
            "result_percent": ((current - entry) / entry * 100) if entry else None,
            "max_favorable_percent": ((maximum - entry) / entry * 100) if entry else None,
            "max_adverse_percent": ((minimum - entry) / entry * 100) if entry else None,
        })
    symbols = (scan_diagnostics or {}).get("symbols", [])
    reason_counts = Counter(item.get("reason", "unknown") for item in symbols)
    operational_reasons = {"market_data_unavailable", "analysis_error"}
    operational_failures = sum(
        count for reason, count in reason_counts.items()
        if reason in operational_reasons
    )
    strategy_rejections = sum(reason_counts.values()) - operational_failures
    pending_trades = [item for item in safe_trades if item["status"] == "pending_close"]
    recommendations = []
    checked = int((scan_diagnostics or {}).get("checked") or 0)
    if checked and operational_failures / checked >= 0.10:
        recommendations.append({
            "priority": "critical", "code": "unstable_market_data",
            "message": (
                "Не менять стратегию до восстановления API: не менее 10% пар "
                "потеряны из-за недоступных данных."
            ),
        })
    if pending_trades:
        recommendations.append({
            "priority": "action", "code": "pending_trade_confirmation",
            "message": (
                f"Подтвердите или отмените закрытие {len(pending_trades)} сделок "
                "в разделе /trades."
            ),
        })
    if checked and not (scan_diagnostics or {}).get("accepted") and not operational_failures:
        recommendations.append({
            "priority": "info", "code": "no_valid_setup",
            "message": (
                "Скан исправен, но рынок не дал подтверждённых входов; "
                "автоматически ослаблять фильтры не рекомендуется."
            ),
        })
    report = {
        "report_version": 3,
        "database_id": database_id,
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
        "scan_quality": {
            "checked": checked,
            "accepted": int((scan_diagnostics or {}).get("accepted") or 0),
            "operational_failures": operational_failures,
            "strategy_rejections": strategy_rejections,
            "data_availability_percent": (
                round((checked - operational_failures) / checked * 100, 2)
                if checked else None
            ),
        },
        "filter_summary": dict(reason_counts),
        "service_state": service_state or {},
        "recommendations": recommendations,
        "trades": safe_trades,
        "privacy": "No Telegram token, API key, environment variable or chat id is included.",
    }
    return json.dumps(report, ensure_ascii=False, indent=2, default=str).encode("utf-8")
