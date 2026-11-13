"""Isolated, budgeted experimental Groq signals using causal OTCharts snapshots."""

import asyncio
import hashlib
import json
import logging
import math
import os
import sqlite3
import time
from contextlib import closing

import aiohttp

from services.otc_ml.features import FEATURE_NAMES, indicators
from services.otc_ml.preview import MLPreview
from services.otc_strategy import aggregate

logger = logging.getLogger(__name__)
DURATIONS = (1, 2, 3, 5, 15)


def fresh(asset, quote, now):
    return bool(
        asset
        and asset.open is True
        and asset.payout is not None
        and math.isfinite(asset.payout)
        and 90 <= asset.payout <= 100
        and asset.observed_at is not None
        and 0 <= now - asset.observed_at <= 60
        and quote is not None
        and 0 <= now - quote <= 8
    )


def market_packet(history, assets, quotes, now, end):
    entry = int(now) // 60 * 60 + 60
    durations = [n for n in DURATIONS if not end or entry + n * 60 + 60 <= end]
    result = dict(asof=now, entry_at=entry, durations_minutes=durations, assets=[])
    for symbol, bars in history.items():
        asset = assets.get(symbol)
        if not fresh(asset, quotes.get(symbol), now):
            continue
        past = [b for b in bars if b.time + 60 <= now][-1200:]
        frames = {}
        for seconds in (60, 300, 900):
            values = indicators(aggregate(past, seconds))
            if values:
                frames["M" + str(seconds // 60)] = dict(
                    zip(FEATURE_NAMES, [round(v, 7) for v in values])
                )
        if "M1" not in frames or not past or now - (past[-1].time + 60) > 65:
            continue
        result["assets"].append(
            dict(
                symbol=symbol,
                payout=asset.payout,
                quote_at=quotes[symbol],
                last_closed_at=past[-1].time + 60,
                indicators=frames,
                candles_m1=[
                    [b.time, b.open, b.high, b.low, b.close] for b in past[-4:]
                ],
            )
        )
    return result


class GroqClient:
    def __init__(
        self,
        key,
        path,
        model="openai/gpt-oss-20b",
        clock=time.time,
        max_requests=100,
        max_tokens=150000,
        interval=300,
    ):
        self.key, self.path, self.model, self.clock = key, path, model, clock
        self.max_requests, self.max_tokens = max_requests, max_tokens
        self.interval = max(60, interval)
        self.session = None
        with closing(sqlite3.connect(path)) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS groq_calls(id INTEGER PRIMARY KEY,at REAL,tokens INTEGER,status TEXT,evidence TEXT);
                CREATE TABLE IF NOT EXISTS groq_state(key TEXT PRIMARY KEY,value REAL);
            """)
            db.commit()

    def reserve(self, estimate):
        now = self.clock()
        with closing(sqlite3.connect(self.path, timeout=3)) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT INTO groq_state VALUES('request_estimate',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (estimate,),
            )
            paused = db.execute(
                "SELECT value FROM groq_state WHERE key='paused_until'"
            ).fetchone()
            count, tokens, latest = db.execute(
                "SELECT count(*),coalesce(sum(tokens),0),max(at) FROM groq_calls WHERE at>?",
                (now - 86400,),
            ).fetchone()
            if (
                (paused and now < paused[0])
                or count >= self.max_requests
                or tokens + estimate > self.max_tokens
                or (latest is not None and now - latest < self.interval)
            ):
                db.commit()
                return None
            cursor = db.execute(
                "INSERT INTO groq_calls(at,tokens,status) VALUES(?,?,?)",
                (now, estimate, "RESERVED"),
            )
            db.commit()
            return cursor.lastrowid

    def complete(self, identifier, status, evidence=None, tokens=None, pause=0):
        with closing(sqlite3.connect(self.path, timeout=3)) as db:
            db.execute(
                "UPDATE groq_calls SET status=?,evidence=?,tokens=coalesce(?,tokens) WHERE id=?",
                (
                    status,
                    json.dumps(evidence, allow_nan=False) if evidence else None,
                    tokens,
                    identifier,
                ),
            )
            if pause:
                db.execute(
                    "INSERT INTO groq_state VALUES('paused_until',?) ON CONFLICT(key) DO UPDATE SET value=max(value,excluded.value)",
                    (self.clock() + pause,),
                )
            db.commit()

    async def ask(self, packet):
        if not self.key or not packet["assets"] or not packet["durations_minutes"]:
            return None
        payload = json.dumps(packet, separators=(",", ":"), allow_nan=False)
        identifier = await asyncio.to_thread(
            self.reserve, len(payload.encode()) // 2 + 900
        )
        if identifier is None:
            return None
        schema = dict(
            type="object",
            additionalProperties=False,
            required=["action", "symbol", "minutes", "reason"],
            properties={
                "action": dict(type="string", enum=["UP", "DOWN", "SKIP"]),
                "symbol": dict(
                    type="string", enum=[""] + [a["symbol"] for a in packet["assets"]]
                ),
                "minutes": dict(type="integer", enum=packet["durations_minutes"]),
                "reason": dict(type="string"),
            },
        )
        body = dict(
            model=self.model,
            temperature=0,
            reasoning_effort="low",
            max_completion_tokens=512,
            messages=[
                dict(
                    role="system",
                    content=(
                        "Analyze experimental OTC directional forecasts using only the supplied completed candles and normalized indicators. "
                        "Select at most one asset and duration; SKIP ambiguous or conflicting evidence. "
                        "No internet, future data, chart access, calibrated probabilities or proven profitability is available. "
                        "RSI and ADX are scaled 0..1; EMA/MACD/ATR values are normalized by price. "
                        "Give a concise factual reason. Never invent prices, payouts, news or win probabilities."
                    ),
                ),
                dict(role="user", content=payload),
            ],
            response_format=dict(
                type="json_schema",
                json_schema=dict(name="otc_decision", strict=True, schema=schema),
            ),
        )
        try:
            if self.session is None:
                self.session = aiohttp.ClientSession(
                    proxy=os.getenv("TRADINGAI_HTTP_PROXY"),
                    timeout=aiohttp.ClientTimeout(total=10),
                )
            async with self.session.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": "Bearer " + self.key},
                json=body,
            ) as response:
                if response.status != 200:
                    try:
                        pause = min(
                            86400,
                            max(60, float(response.headers.get("retry-after", "3600"))),
                        )
                    except ValueError:
                        pause = 3600
                    await asyncio.to_thread(
                        self.complete,
                        identifier,
                        "HTTP_" + str(response.status),
                        None,
                        None,
                        pause
                        if response.status == 429
                        else 3600
                        if response.status in (401, 403)
                        else 300,
                    )
                    logger.warning(
                        "Groq paused after HTTP %d; other strategies unaffected",
                        response.status,
                    )
                    return None
                data = await response.json()
                decision = json.loads(data["choices"][0]["message"]["content"])
                allowed = {a["symbol"] for a in packet["assets"]}
                if (
                    decision.get("action") not in ("UP", "DOWN", "SKIP")
                    or type(decision.get("minutes")) is not int
                    or decision["minutes"] not in packet["durations_minutes"]
                    or (
                        decision["action"] != "SKIP"
                        and decision.get("symbol") not in allowed
                    )
                    or not isinstance(decision.get("reason"), str)
                    or len(decision["reason"]) > 1000
                ):
                    raise ValueError("invalid structured decision")
                evidence = dict(
                    packet=packet,
                    decision=decision,
                    model=self.model,
                    response_id=data.get("id"),
                    received_at=self.clock(),
                )
                tokens = data.get("usage", {}).get("total_tokens")
                if type(tokens) is not int or tokens < 0:
                    tokens = None
                pause = (
                    86400
                    if response.headers.get("x-ratelimit-remaining-requests") == "0"
                    else 0
                )
                await asyncio.to_thread(
                    self.complete, identifier, "OK", evidence, tokens, pause
                )
                return evidence
        except asyncio.CancelledError:
            await asyncio.to_thread(self.complete, identifier, "CANCELLED")
            raise
        except Exception as exc:
            await asyncio.to_thread(
                self.complete, identifier, type(exc).__name__, None, None, 300
            )
            logger.warning("Groq request skipped: %s", type(exc).__name__)
            return None

    async def close(self):
        if self.session is not None:
            await self.session.close()

    def health(self):
        now = self.clock()
        with closing(sqlite3.connect(self.path, timeout=3)) as db:
            count, tokens = db.execute(
                "SELECT count(*),coalesce(sum(tokens),0) FROM groq_calls WHERE at>?",
                (now - 86400,),
            ).fetchone()
            paused = db.execute(
                "SELECT value FROM groq_state WHERE key='paused_until'"
            ).fetchone()
            latest = db.execute(
                "SELECT status,evidence FROM groq_calls WHERE at>? AND status!='RESERVED' "
                "ORDER BY id DESC LIMIT 1",
                (now - 86400,),
            ).fetchone()
            estimate_row = db.execute(
                "SELECT value FROM groq_state WHERE key='request_estimate'"
            ).fetchone()
        estimate = estimate_row[0] if estimate_row else 900
        if not estimate_row and latest and latest[1]:
            packet = json.loads(latest[1]).get("packet")
            if packet:
                estimate = (
                    len(
                        json.dumps(
                            packet, separators=(",", ":"), allow_nan=False
                        ).encode()
                    )
                    // 2
                    + 900
                )
        exhausted = count >= self.max_requests or tokens + estimate > self.max_tokens
        rate_limited = bool(
            paused and now < paused[0] and latest and latest[0] == "HTTP_429"
        )
        state = (
            "нет ключа"
            if not self.key
            else "лимит исчерпан"
            if exhausted or rate_limited
            else "пауза"
            if paused and now < paused[0]
            else "API ответил"
            if latest and latest[0] == "OK"
            else "ошибка последнего запроса"
            if latest
            else "ожидает первого ответа API"
        )
        return state == "API ответил", (
            f"{state} · запросы {count}/{self.max_requests}"
            f" · токены {tokens}/{self.max_tokens}"
        )

    def status(self):
        active, detail = self.health()
        return f"{'🟢' if active else '🔴'} Groq · {detail}"


class GroqPreview(MLPreview):
    icon = "🟣"
    label = "Groq"
    planning_window = (30, 35)

    def __init__(self, journal, client, clock=time.time, settlement_provider=None):
        super().__init__(journal, None, clock, settlement_provider)
        self.client = client

    async def plan(self, bot, chat_id, snapshot):
        if await asyncio.to_thread(self.journal.blocked, self.clock()):
            return
        history, assets, quotes, end = snapshot
        now = self.clock()
        packet = await asyncio.to_thread(
            market_packet,
            {s: list(b) for s, b in history.items()},
            dict(assets),
            dict(quotes),
            now,
            end,
        )
        evidence = await self.client.ask(packet)
        if not evidence or evidence["decision"]["action"] == "SKIP" or not self.enabled:
            return
        entry = packet["entry_at"]
        if entry - self.clock() < 15:
            return
        if entry - self.clock() > 20:
            await asyncio.sleep(entry - self.clock() - 20)
        now = self.clock()
        if not self.enabled or not self.snapshot or not 15 <= entry - now <= 20:
            return
        _, current_assets, current_quotes, current_end = self.snapshot
        decision = evidence["decision"]
        asset = current_assets.get(decision["symbol"])
        expiry = entry + decision["minutes"] * 60
        if not fresh(asset, current_quotes.get(decision["symbol"]), now) or (
            current_end and expiry + 60 > current_end
        ):
            return
        selected = dict(
            symbol=asset.symbol,
            name=asset.name or asset.symbol,
            entry_at=entry,
            expires_at=expiry,
            direction=decision["action"],
            payout=asset.payout,
            model_sha256=hashlib.sha256(self.client.model.encode()).hexdigest(),
            model_role="unqualified_groq_experimental",
            groq=evidence,
            feature_asof=packet["asof"],
        )
        await self.publish(selected, bot, chat_id)

    async def run(self, bot, chat_id):
        try:
            await super().run(bot, chat_id)
        finally:
            await self.client.close()
