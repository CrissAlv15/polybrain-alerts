import os
import asyncio
import time
import math
import random
from dataclasses import dataclass
from typing import Dict, Any, Optional, List, Tuple

import aiohttp
import discord

# =========================
# ENV (only 2 required)
# =========================
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
DISCORD_CHANNEL_ID = os.getenv("DISCORD_CHANNEL_ID")

if not DISCORD_BOT_TOKEN or not DISCORD_CHANNEL_ID:
    raise SystemExit("Missing DISCORD_BOT_TOKEN or DISCORD_CHANNEL_ID")

CHANNEL_ID = int(DISCORD_CHANNEL_ID)

# =========================
# POLYMARKET ENDPOINTS
# =========================
GAMMA = "https://gamma-api.polymarket.com"

# =========================
# PHASE 2 SETTINGS (defaults)
# =========================
SCAN_EVERY_SECONDS = int(os.getenv("SCAN_EVERY_SECONDS", "20"))
UNIVERSE_REFRESH_SECONDS = int(os.getenv("UNIVERSE_REFRESH_SECONDS", "120"))

# Phase 1 (arb) thresholds
MIN_ARB_EDGE = float(os.getenv("MIN_ARB_EDGE", "0.005"))  # 0.5%

# Phase 2 (value) thresholds
PHASE2_ENABLED = os.getenv("PHASE2_ENABLED", "1") == "1"
VALUE_MIN_DROP = float(os.getenv("VALUE_MIN_DROP", "0.08"))      # 8% drop from anchor
VALUE_MIN_SPEED = float(os.getenv("VALUE_MIN_SPEED", "0.03"))    # 3% per minute speed
VALUE_COOLDOWN_MIN = int(os.getenv("VALUE_COOLDOWN_MIN", "30"))  # don’t spam same market

# Rate-limit safety
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "15"))
MAX_EVENTS_PER_LEAGUE = int(os.getenv("MAX_EVENTS_PER_LEAGUE", "200"))
MAX_CONCURRENT_HTTP = int(os.getenv("MAX_CONCURRENT_HTTP", "6"))

# =========================
# DISCORD CLIENT
# =========================
intents = discord.Intents.default()
client = discord.Client(intents=intents)

# =========================
# SMALL STRUCTURES
# =========================
@dataclass
class MarketSnap:
    t: float
    p: float  # probability (0..1)

@dataclass
class ValueSignal:
    market_id: str
    label: str
    p_now: float
    p_anchor: float
    drop: float
    speed_per_min: float
    tp1: float
    tp2: float

# =========================
# GLOBAL STATE (in-memory)
# =========================
# market_id -> recent history (we keep a few points)
price_hist: Dict[str, List[MarketSnap]] = {}

# market_id -> last alert time
last_alert_ts: Dict[str, float] = {}

# universe cache
universe_markets: List[Dict[str, Any]] = []
last_universe_refresh = 0.0

# concurrency limiter
sem = asyncio.Semaphore(MAX_CONCURRENT_HTTP)


# =========================
# UTIL
# =========================
def now() -> float:
    return time.time()

def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))

def pct(x: float) -> str:
    return f"{x*100:.1f}%"

def cents(x: float) -> str:
    return f"{int(round(x*100))}¢"

def can_alert(market_id: str, cooldown_min: int = VALUE_COOLDOWN_MIN) -> bool:
    t0 = last_alert_ts.get(market_id, 0.0)
    return (now() - t0) >= cooldown_min * 60

def mark_alert(market_id: str) -> None:
    last_alert_ts[market_id] = now()

def add_hist(market_id: str, p: float) -> None:
    h = price_hist.get(market_id)
    if h is None:
        h = []
        price_hist[market_id] = h
    h.append(MarketSnap(t=now(), p=p))
    # keep only last ~30 mins worth + max 60 points
    cutoff = now() - 30 * 60
    while len(h) > 60:
        h.pop(0)
    while h and h[0].t < cutoff and len(h) > 6:
        h.pop(0)

def anchor_price(market_id: str) -> Optional[float]:
    """
    Anchor = median of last few points (robust vs single spike).
    """
    h = price_hist.get(market_id)
    if not h or len(h) < 6:
        return None
    last = h[-12:]  # last 12 points
    vals = sorted(s.p for s in last)
    mid = len(vals) // 2
    if len(vals) % 2 == 1:
        return vals[mid]
    return 0.5 * (vals[mid - 1] + vals[mid])

def speed_per_min(market_id: str) -> Optional[float]:
    """
    Approx speed = |p_now - p_5min_ago| per minute
    """
    h = price_hist.get(market_id)
    if not h or len(h) < 6:
        return None
    t_now = h[-1].t
    p_now = h[-1].p
    target = t_now - 5 * 60
    # find closest point at or before 5 min ago
    p_old = None
    for s in reversed(h):
        if s.t <= target:
            p_old = s.p
            t_old = s.t
            break
    if p_old is None:
        # fallback: use earliest point
        p_old = h[0].p
        t_old = h[0].t
    dt_min = max((t_now - t_old) / 60.0, 0.5)
    return abs(p_now - p_old) / dt_min

def make_take_profits(p_entry: float, edge: float) -> Tuple[float, float]:
    """
    Simple sell-plan:
    - TP1: recover most of the move
    - TP2: stronger mean reversion
    Caps so we don't output crazy targets.
    """
    # edge is the drop from anchor (positive when oversold)
    tp1 = p_entry + clamp(edge * 0.55, 0.04, 0.10)  # +4% to +10%
    tp2 = p_entry + clamp(edge * 0.90, 0.07, 0.18)  # +7% to +18%
    tp1 = clamp(tp1, 0.01, 0.99)
    tp2 = clamp(tp2, 0.01, 0.99)
    if tp2 < tp1:
        tp2 = tp1 + 0.03
    tp2 = clamp(tp2, 0.01, 0.99)
    return tp1, tp2


# =========================
# HTTP
# =========================
async def http_get_json(session: aiohttp.ClientSession, url: str, params: Dict[str, Any]) -> Any:
    """
    Safe GET with backoff on 429/5xx.
    """
    backoff = 1.0
    for attempt in range(8):
        async with sem:
            try:
                async with session.get(url, params=params, timeout=HTTP_TIMEOUT) as r:
                    if r.status == 200:
                        return await r.json()

                    # Handle 429 / rate limits
                    if r.status == 429:
                        retry_after = r.headers.get("Retry-After")
                        if retry_after:
                            sleep_s = float(retry_after)
                        else:
                            sleep_s = backoff + random.random()
                        print(f"[WARN] 429 rate limit. Sleeping {sleep_s:.1f}s")
                        await asyncio.sleep(sleep_s)
                        backoff = min(backoff * 1.7, 30.0)
                        continue

                    # transient server issues
                    if 500 <= r.status < 600:
                        sleep_s = backoff + random.random()
                        print(f"[WARN] {r.status} server error. Sleeping {sleep_s:.1f}s")
                        await asyncio.sleep(sleep_s)
                        backoff = min(backoff * 1.7, 30.0)
                        continue

                    # other errors: log and stop retrying
                    body = await r.text()
                    raise aiohttp.ClientResponseError(
                        request_info=r.request_info,
                        history=r.history,
                        status=r.status,
                        message=body[:200],
                        headers=r.headers,
                    )
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                sleep_s = backoff + random.random()
                print(f"[WARN] HTTP error: {e}. Sleeping {sleep_s:.1f}s")
                await asyncio.sleep(sleep_s)
                backoff = min(backoff * 1.7, 30.0)
                continue

    raise RuntimeError("HTTP failed after retries")


# =========================
# POLYMARKET DATA
# =========================
async def fetch_events(session: aiohttp.ClientSession, *, offset: int = 0, limit: int = 200) -> List[Dict[str, Any]]:
    """
    Pull open events. We use a generic query; Polymarket will return lots of sports + others,
    but we filter to sports by tags/structure when possible.
    """
    url = f"{GAMMA}/events"
    params = {
        "active": "true",
        "closed": "false",
        "limit": str(limit),
        "offset": str(offset),
        "order": "startTime",
        "ascending": "true",
    }
    return await http_get_json(session, url, params)

def extract_markets_from_events(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    markets: List[Dict[str, Any]] = []
    for ev in events:
        # Events include "markets" list
        ms = ev.get("markets") or []
        for m in ms:
            # We only care about binary markets (YES/NO style)
            # Many markets use "outcomes"/"outcomePrices" etc
            markets.append(m)
    return markets

def market_prob(m: Dict[str, Any]) -> Optional[float]:
    """
    Return YES price probability as float 0..1.
    Polymarket market objects can vary; we try several fields.
    """
    # common fields seen: "yesPrice" / "noPrice"
    yes = m.get("yesPrice")
    if yes is not None:
        try:
            return float(yes)
        except:
            pass

    # sometimes "outcomePrices": ["0.43","0.57"] with outcomes ["Yes","No"]
    outs = m.get("outcomes")
    prices = m.get("outcomePrices")
    if outs and prices and len(outs) == len(prices):
        for o, p in zip(outs, prices):
            if str(o).lower() == "yes":
                try:
                    return float(p)
                except:
                    return None

    return None

def market_label(m: Dict[str, Any]) -> str:
    # best effort label
    title = m.get("question") or m.get("title") or "Market"
    return str(title)

def market_id(m: Dict[str, Any]) -> str:
    return str(m.get("id") or m.get("marketId") or m.get("slug") or market_label(m))


# =========================
# SIGNAL LOGIC
# =========================
def detect_arbs(markets: List[Dict[str, Any]]) -> List[Tuple[str, float, float]]:
    """
    Phase 1: simple arb detection on YES/NO pricing.
    Arb exists if yes + no < 1 - MIN_ARB_EDGE
    Returns list of (label, yes, no)
    """
    out = []
    for m in markets:
        yes = market_prob(m)
        if yes is None:
            continue
        no = 1.0 - yes
        # In real Polymarket you often have both sides available; this is a simplified check.
        # We treat it as potential arb only when spread implies < 1 significantly.
        if (yes + no) < (1.0 - MIN_ARB_EDGE):
            out.append((market_label(m), yes, no))
    return out

def detect_value(m: Dict[str, Any], p_now: float) -> Optional[ValueSignal]:
    """
    Phase 2: value detection using only price history (no news).
    Signal when price is significantly below its recent anchor AND move speed is meaningful.
    """
    mid = market_id(m)
    a = anchor_price(mid)
    spd = speed_per_min(mid)

    if a is None or spd is None:
        return None

    drop = a - p_now
    if drop < VALUE_MIN_DROP:
        return None

    if spd < VALUE_MIN_SPEED:
        return None

    if not can_alert(mid, VALUE_COOLDOWN_MIN):
        return None

    tp1, tp2 = make_take_profits(p_now, drop)
    return ValueSignal(
        market_id=mid,
        label=market_label(m),
        p_now=p_now,
        p_anchor=a,
        drop=drop,
        speed_per_min=spd,
        tp1=tp1,
        tp2=tp2,
    )


# =========================
# ALERTS
# =========================
async def send_discord(text: str) -> None:
    channel = client.get_channel(CHANNEL_ID) or await client.fetch_channel(CHANNEL_ID)
    await channel.send(text)

async def alert_value(sig: ValueSignal) -> None:
    mark_alert(sig.market_id)
    msg = (
        "🧠 **POLYBRAIN VALUE ALERT (Phase 2)**\n\n"
        f"**Market:** {sig.label}\n"
        f"**Now:** {pct(sig.p_now)} ({cents(sig.p_now)})\n"
        f"**Anchor (recent):** {pct(sig.p_anchor)} ({cents(sig.p_anchor)})\n"
        f"**Drop:** {pct(sig.drop)}\n"
        f"**Move speed:** ~{pct(sig.speed_per_min)}/min\n\n"
        "**Suggested plan (manual):**\n"
        f"• Entry zone: ≤ {pct(sig.p_now)}\n"
        f"• Sell 50% at: {pct(sig.tp1)} ({cents(sig.tp1)})\n"
        f"• Sell rest at: {pct(sig.tp2)} ({cents(sig.tp2)})\n\n"
        "_Note: This is a market-only signal (no injuries/news yet)._"
    )
    await send_discord(msg)

# =========================
# MAIN LOOPS
# =========================
async def refresh_universe_loop() -> None:
    global universe_markets, last_universe_refresh
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                t0 = now()
                # pull first page only (fast + rate-limit safe)
                events = await fetch_events(session, offset=0, limit=200)
                markets = extract_markets_from_events(events)

                universe_markets = markets
                last_universe_refresh = now()
                print(f"[universe] markets={len(universe_markets)} refresh={(now()-t0):.1f}s")

            except Exception as e:
                print(f"[ERROR] [universe] failed: {e}")

            await asyncio.sleep(UNIVERSE_REFRESH_SECONDS)

async def scan_loop() -> None:
    """
    Main loop: update history + detect Phase 2 value signals.
    """
    await asyncio.sleep(3)  # let universe populate
    while True:
        try:
            markets = universe_markets
            checked = 0
            value_found = 0
            arbs_found = 0

            for m in markets:
                p = market_prob(m)
                if p is None:
                    continue

                mid = market_id(m)
                add_hist(mid, p)
                checked += 1

                if PHASE2_ENABLED:
                    sig = detect_value(m, p)
                    if sig:
                        value_found += 1
                        await alert_value(sig)

                # placeholder: arb logic can be expanded later
                # (we keep arbs_found for logging)
                # arbs_found += ...

            print(f"[scan] checked={checked} value_found={value_found} arbs_found={arbs_found}")

        except Exception as e:
            print(f"[ERROR] [scan] {e}")

        await asyncio.sleep(SCAN_EVERY_SECONDS)

@client.event
async def on_ready():
    print(f"Logged in as: {client.user}")
    try:
        await send_discord("✅ PolyBrain is live. Phase 2 scanning is ON.")
    except Exception as e:
        print(f"[ERROR] could not send startup message: {e}")

    # start background tasks
    asyncio.create_task(refresh_universe_loop())
    asyncio.create_task(scan_loop())

client.run(DISCORD_BOT_TOKEN)
