"""Owner-only read-only Telegram Mini App.

Reverse-proxy the loopback port with HTTPS. API never accepts unsigned user ids
or exposes secret tokens, price history, unredacted model packets, or order APIs.
"""
import asyncio
import hashlib
import hmac
import json
import time
from collections import Counter
from pathlib import Path
from urllib.parse import parse_qsl

from aiohttp import web

HERE = Path(__file__).parent
MAX_AUTH_AGE = 3600


def verify_init_data(raw, token, owner_id, now=None):
    if not isinstance(raw, str) or len(raw) > 8192 or not token or owner_id <= 0:
        return False
    now = time.time() if now is None else now
    try:
        pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True)
        fields = dict(pairs)
        if len(pairs) != len(fields) or len(pairs) > 30:
            return False
        received = fields.pop("hash")
        fields.pop("signature", None)
        timestamp = int(fields["auth_date"])
        if not 0 <= now - timestamp <= MAX_AUTH_AGE:
            return False
        user = json.loads(fields["user"])
        if type(user) is not dict or type(user.get("id")) is not int:
            return False
        if user["id"] != owner_id or len(received) != 64:
            return False
        payload = "\n".join(k + "=" + v for k, v in sorted(fields.items()))
        secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, received)
    except (KeyError, ValueError, TypeError, UnicodeError):
        return False


def read_dashboard(service, now):
    from services.otc_signal_report import read_journal

    sources = (("Основной", "main_ready"), ("XGBoost", "ml_preview"),
               ("Groq", "groq_preview"))
    recent, stats = [], {}
    for label, attr in sources:
        runner = getattr(service, attr, None)
        rows = read_journal(runner.journal, now - 7 * 86400, now + 1) if runner else []
        counts = Counter(row["outcome"] for row in rows)
        wins, losses = counts["WIN"], counts["LOSS"]
        stats[label] = dict(
            total=len(rows), wins=wins, losses=losses, draws=counts["DRAW"],
            unknown=len(rows) - wins - losses - counts["DRAW"],
            win_rate=round(wins / (wins + losses), 4) if wins + losses else None,
        )
        for row in rows[-12:]:
            try:
                evidence = json.loads(row["evidence"] or "{}")
            except (ValueError, TypeError):
                evidence = {}
            conf = evidence.get("confirmations") or {}
            recent.append({
                "source": label, "symbol": row["symbol"],
                "direction": row["direction"], "entry_at": row["entry_at"],
                "minutes": int((row["expires_at"] - row["entry_at"]) / 60),
                "payout": row["payout"], "outcome": row["outcome"],
                "confirmations": conf.get("count") if conf.get("available") else None,
                "regime": (conf.get("market_state") or {}).get("label"),
                "quote_age_s": (evidence.get("timing") or {}).get("quote_age_s"),
            })
    recent.sort(key=lambda row: row["entry_at"], reverse=True)
    return {
        "generated_at": now,
        "running": bool(service.running()),
        "phase": str(service.phase)[:100],
        "monitored": int(service.monitored),
        "quote_age_s": round(now - service.last_tick, 2)
            if service.last_tick is not None and 0 <= now - service.last_tick < 86400
            else None,
        "mode": "virtual_research",
        "platform_prices_verified": False,
        "strategies": stats,
        "recent": recent[:30],
    }


CSP = ("default-src 'none'; base-uri 'none'; object-src 'none'; "
       "connect-src 'self'; img-src 'self' data:; "
       "script-src 'self' https://telegram.org; "
       "style-src 'self' 'unsafe-inline'; frame-ancestors https://web.telegram.org")


def create_app(service, token, owner_id):
    app = web.Application(client_max_size=16384)

    async def index(request):
        return web.Response(
            text=(HERE / "index.html").read_text(encoding="utf-8"),
            content_type="text/html",
            headers={"Cache-Control": "no-store", "Content-Security-Policy": CSP,
                     "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"},
        )

    async def app_js(request):
        return web.Response(
            text=(HERE / "app.js").read_text(encoding="utf-8"),
            content_type="application/javascript",
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )

    async def dashboard(request):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("tma ") or not verify_init_data(
            auth[4:], token, owner_id
        ):
            raise web.HTTPUnauthorized(text="Telegram authentication required")
        try:
            data = await asyncio.to_thread(read_dashboard, service, time.time())
        except (OSError, ValueError):
            raise web.HTTPServiceUnavailable(text="Journal temporarily unavailable")
        return web.json_response(data, headers={"Cache-Control": "no-store"})

    app.router.add_get("/", index)
    app.router.add_get("/app.js", app_js)
    app.router.add_get("/api/dashboard", dashboard)
    return app


async def start_server(service, token, owner_id, port=8091):
    runner = web.AppRunner(create_app(service, token, owner_id), access_log=None)
    await runner.setup()
    try:
        await web.TCPSite(runner, "127.0.0.1", port).start()
    except BaseException:
        await runner.cleanup()
        raise
    return runner
