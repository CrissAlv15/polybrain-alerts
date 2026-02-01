import os
import time
import json
import math
import asyncio
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import aiohttp
import discord

# -----------------------------
# Config (env vars)
# -----------------------------
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
DISCORD_CHANNEL_ID = os.getenv("DISCORD_CHANNEL_ID")

SCAN_INTERVAL_SEC = int(os.getenv("SCAN_INTERVAL_SEC", "20"))          # how often we scan prices
UNIVERSE_REFRESH_SEC = int(os.getenv("UNIVERSE_REFRESH_SEC", "600"))   # refresh market list
MAX_EVENTS_PER_LEAGUE = int(os.getenv("MAX_EVENTS_PER_LEAGUE", "200")) # safety cap

# Alert thresholds (tune later)
ARB_BUFFER = float(os.getenv("ARB_BUFFER", "0.010"))     # require sum(yes_buy + no_buy) < 1 - buffer
MIN_LIQUIDITY = float(os.getenv("MIN_LIQUIDITY", "200")) # minimum top-of-book size to consider "safe"
MAX_SPREAD = float(os.getenv("MAX_SPREAD", "0.12"))      # skip if spread too wide
MOVE_ALERT = float(os.getenv("MOVE_ALERT", "0.09"))      # price change trigger (9%)
Z_ALERT = float(os.getenv("Z_ALERT", "2.5"))             # z-score trigger for "value"
COOLDOWN_SEC = int(os.getenv("COOLDOWN_SEC", "300"))     # per-market alert cooldown

if not DISCORD_BOT_TOKEN or not DISCORD_CHANNEL_ID:
    raise SystemExit("Missing DISCORD_BOT_TOKEN or DISCORD_CHANNEL_ID")

CHANNEL_ID_INT = int(DISCORD_CHANNEL_ID)

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"

# -----------------------------
# Logging
# -----------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("polybrain")

# -----------------------------
# Helpers / Models
# -----------------------------
def now_ts() -> float:
    return time.time()

def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))

@dataclass
class MarketInfo:
    market_id: str
    event_id: str
    event_title: str
    league: str
    question: str
    slug: Optional[str]
    token_yes: str
    token_no: str

@dataclass
class MarketState:
    # rolling window for yes price
    prices: List[Tuple[float, float]] = field(default_factory=list)  # (ts, price_yes)
    last_alert_ts: float = 0.0

    def add_price(self, ts: float, p: float, window_sec: int = 1800) -> None:
        self.prices.append((ts, p))
        cutoff = ts - window_sec
        while self.prices and self.prices[0][0] < cutoff:
            self.prices.pop(0)

    def stats(self) -> Tuple[Optional[float], Optional[float]]:
        if len(self.prices) < 8:
            return None, None
        vals = [p for _, p in self.prices]
        mean = sum(vals) / len(vals)
        var = sum((v - mean) ** 2 for v in vals) / max(1, (len(vals) - 1))
        sd = math.sqrt(var) if var > 0 else 0.0
        return mean, sd

# -----------------------------
# Polymarket API calls
# -----------------------------
async def http_get_json(session: aiohttp.ClientSession, url: str, params: Optional[dict] = None) -> dict:
    async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=20)) as r:
        r.raise_for_status()
        return await r.json()

async def fetch_sports_leagues(session: aiohttp.ClientSession) -> List[dict]:
    # docs: GET https://gamma-api.polymarket.com/sports  [oai_citation:2‡Polymarket Documentation](https://docs.polymarket.com/quickstart/fetching-data)
    return await http_get_json(session, f"{GAMMA}/sports")

async def fetch_events_for_league(session: aiohttp.ClientSession, series_id: int, limit: int = 200) -> List[dict]:
    # docs: GET /events?series_id=...&active=true&closed=false  [oai_citation:3‡Polymarket Documentation](https://docs.polymarket.com/quickstart/fetching-data)
    params = {
        "series_id": series_id,
        "active": "true",
        "closed": "false",
        "limit": str(limit),
        "order": "startTime",
        "ascending": "true",
    }
    return await http_get_json(session, f"{GAMMA}/events", params=params)

async def clob_price(session: aiohttp.ClientSession, token_id: str, side: str = "buy") -> Optional[float]:
    # docs: GET https://clob.polymarket.com/price?token_id=...&side=buy  [oai_citation:4‡Polymarket Documentation](https://docs.polymarket.com/quickstart/fetching-data)
    data = await http_get_json(session, f"{CLOB}/price", params={"token_id": token_id, "side": side})
    try:
        return float(data["price"])
    except Exception:
        return None

async def clob_book(session: aiohttp.ClientSession, token_id: str) -> Optional[dict]:
    # docs: GET https://clob.polymarket.com/book?token_id=...  [oai_citation:5‡Polymarket Documentation](https://docs.polymarket.com/quickstart/fetching-data)
    return await http_get_json(session, f"{CLOB}/book", params={"token_id": token_id})

def parse_outcomes(event_market: dict) -> Optional[Tuple[str, str]]:
    # Polymarket returns clobTokenIds [YES, NO] for binary markets in examples  [oai_citation:6‡Polymarket Documentation](https://docs.polymarket.com/quickstart/fetching-data)
    ids = event_market.get("clobTokenIds")
    if not ids or len(ids) < 2:
        return None
    return str(ids[0]), str(ids[1])

# -----------------------------
# Discord
# -----------------------------
intents = discord.Intents.default()
client = discord.Client(intents=intents)

async def send_discord(channel_id: int, text: str) -> None:
    channel = client.get_channel(channel_id)
    if channel is None:
        channel = await client.fetch_channel(channel_id)
    await channel.send(text)

# -----------------------------
# Intelligence: Phase 1 signals
# -----------------------------
def spread_from_book(book: dict) -> Tuple[Optional[float], Optional[float], Optional[float], Optional[float]]:
    bids = book.get("bids") or []
    asks = book.get("asks") or []
    if not bids or not asks:
        return None, None, None, None
    best_bid = float(bids[0]["price"])
    best_ask = float(asks[0]["price"])
    bid_sz = float(bids[0].get("size", 0))
    ask_sz = float(asks[0].get("size", 0))
    return best_bid, best_ask, bid_sz, ask_sz

def zscore(x: float, mean: float, sd: float) -> float:
    if sd <= 1e-9:
        return 0.0
    return (x - mean) / sd

def build_sell_plan(current: float) -> str:
    # Simple, consistent “two-take-profit + stop” plan
    # (not trading advice; just a template)
    tp1 = clamp(current + 0.06, 0.02, 0.98)
    tp2 = clamp(current + 0.12, 0.02, 0.98)
    stp = clamp(current - 0.07, 0.02, 0.98)
    return f"Plan: TP1 50% @ {tp1:.2f} | TP2 50% @ {tp2:.2f} | Stop @ {stp:.2f}"

# -----------------------------
# Bot state
# -----------------------------
MARKETS: Dict[str, MarketInfo] = {}
STATE: Dict[str, MarketState] = {}

async def refresh_universe_loop():
    global MARKETS
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                leagues = await fetch_sports_leagues(session)
                new_markets: Dict[str, MarketInfo] = {}
                total_events = 0
                total_markets = 0

                for lg in leagues:
                    series_id = lg.get("series_id") or lg.get("seriesId") or lg.get("id")
                    league_name = lg.get("name") or lg.get("league") or "sports"
                    if series_id is None:
                        continue

                    events = await fetch_events_for_league(session, int(series_id), limit=MAX_EVENTS_PER_LEAGUE)
                    if not isinstance(events, list):
                        continue

                    total_events += len(events)

                    for ev in events:
                        ev_id = str(ev.get("id", ""))
                        title = ev.get("title") or ev.get("slug") or "Event"
                        markets = ev.get("markets") or []
                        for m in markets:
                            tokens = parse_outcomes(m)
                            if not tokens:
                                continue
                            token_yes, token_no = tokens
                            market_id = str(m.get("id", "")) or f"{ev_id}:{token_yes}"
                            q = m.get("question") or title
                            slug = m.get("slug")
                            mi = MarketInfo(
                                market_id=market_id,
                                event_id=ev_id,
                                event_title=title,
                                league=str(league_name),
                                question=str(q),
                                slug=str(slug) if slug else None,
                                token_yes=token_yes,
                                token_no=token_no,
                            )
                            new_markets[market_id] = mi
                            total_markets += 1

                MARKETS = new_markets
                log.info(f"[universe] leagues={len(leagues)} events≈{total_events} markets={len(MARKETS)}")
            except Exception as e:
                log.exception(f"[universe] failed: {e}")

            await asyncio.sleep(UNIVERSE_REFRESH_SEC)

async def scan_loop():
    async with aiohttp.ClientSession() as session:
        while True:
            t0 = now_ts()
            markets_list = list(MARKETS.values())
            checked = 0
            matched = 0
            value_found = 0
            arbs_found = 0

            # Light throttling: scan in chunks
            for mi in markets_list:
                checked += 1
                st = STATE.setdefault(mi.market_id, MarketState())

                try:
                    # Prices (buy side)
                    py = await clob_price(session, mi.token_yes, side="buy")
                    pn = await clob_price(session, mi.token_no, side="buy")
                    if py is None or pn is None:
                        continue

                    st.add_price(t0, py)

                    # Orderbook (for spread/liquidity checks)
                    by = await clob_book(session, mi.token_yes)
                    if not by:
                        continue
                    bid, ask, bid_sz, ask_sz = spread_from_book(by)
                    if bid is None or ask is None:
                        continue

                    spread = ask - bid
                    # basic filters
                    if spread > MAX_SPREAD:
                        continue
                    if min(bid_sz or 0, ask_sz or 0) < MIN_LIQUIDITY:
                        # still track, but avoid firing “strong” signals
                        pass

                    matched += 1

                    # Cooldown check
                    if (t0 - st.last_alert_ts) < COOLDOWN_SEC:
                        continue

                    # 1) Arb: buy YES + buy NO < 1 - buffer
                    cost_both = py + pn
                    if cost_both < (1.0 - ARB_BUFFER):
                        arbs_found += 1
                        st.last_alert_ts = t0
                        msg = (
                            f"🟢 **ARB FOUND** ({mi.league})\n"
                            f"**{mi.question}**\n"
                            f"YES buy: {py:.3f} | NO buy: {pn:.3f} | sum: {cost_both:.3f}\n"
                            f"Spread(YES): {spread:.3f} | TopSize≈{min(bid_sz, ask_sz):.0f}\n"
                            f"_MarketID: {mi.market_id}_"
                        )
                        await send_discord(CHANNEL_ID_INT, msg)
                        continue

                    # 2) Momentum move
                    if len(st.prices) >= 2:
                        prev = st.prices[-2][1]
                        move = py - prev
                        if abs(move) >= MOVE_ALERT:
                            value_found += 1
                            st.last_alert_ts = t0
                            direction = "UP ⬆️" if move > 0 else "DOWN ⬇️"
                            msg = (
                                f"⚡ **MOMENTUM {direction}** ({mi.league})\n"
                                f"**{mi.question}**\n"
                                f"YES buy: {py:.3f} (Δ {move:+.3f} since last scan)\n"
                                f"{build_sell_plan(py)}\n"
                                f"_MarketID: {mi.market_id}_"
                            )
                            await send_discord(CHANNEL_ID_INT, msg)
                            continue

                    # 3) “Value” via z-score vs rolling window
                    mean, sd = st.stats()
                    if mean is not None and sd is not None and sd > 0:
                        z = zscore(py, mean, sd)
                        if abs(z) >= Z_ALERT:
                            value_found += 1
                            st.last_alert_ts = t0
                            side = "CHEAP ✅" if z < 0 else "EXPENSIVE ⚠️"
                            msg = (
                                f"📌 **VALUE SIGNAL** {side} ({mi.league})\n"
                                f"**{mi.question}**\n"
                                f"YES buy: {py:.3f} | mean: {mean:.3f} | sd: {sd:.3f} | z: {z:+.2f}\n"
                                f"{build_sell_plan(py)}\n"
                                f"_MarketID: {mi.market_id}_"
                            )
                            await send_discord(CHANNEL_ID_INT, msg)
                            continue

                except Exception as e:
                    # Keep scanning even if one market fails
                    log.debug(f"[scan] market failed {mi.market_id}: {e}")
                    continue

                # Small delay to avoid hammering endpoints
                if checked % 25 == 0:
                    await asyncio.sleep(0.3)

            log.info(f"[scan] checked={checked} matched={matched} value_found={value_found} arbs_found={arbs_found}")
            elapsed = now_ts() - t0
            sleep_for = max(1, SCAN_INTERVAL_SEC - int(elapsed))
            await asyncio.sleep(sleep_for)

@client.event
async def on_ready():
    log.info(f"Logged in as: {client.user}")
    try:
        await send_discord(CHANNEL_ID_INT, "✅ PolyBrain Phase 1 is live (sports universe + arb/value/momentum).")
    except Exception as e:
        log.error(f"Failed to send startup message: {e}")

    # Start loops
    client.loop.create_task(refresh_universe_loop())
    client.loop.create_task(scan_loop())

def main():
    client.run(DISCORD_BOT_TOKEN)

if __name__ == "__main__":
    main()
