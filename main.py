import os
import asyncio
import time
from typing import Dict, Any, List, Optional, Tuple

import discord
import httpx

# ----------------------------
# Required env vars (Railway Variables)
# ----------------------------
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
DISCORD_CHANNEL_ID = os.getenv("DISCORD_CHANNEL_ID")

# ----------------------------
# Optional env vars
# ----------------------------
SCAN_INTERVAL_SECONDS = int(os.getenv("SCAN_INTERVAL_SECONDS", "20"))  # how often we scan
MIN_EDGE = float(os.getenv("MIN_EDGE", "0.01"))  # 0.01 = 1% edge
ALERT_COOLDOWN_SECONDS = int(os.getenv("ALERT_COOLDOWN_SECONDS", "300"))  # 5 min
MAX_LEAGUES = int(os.getenv("MAX_LEAGUES", "999"))  # cap leagues if you want
HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "15"))

# Polymarket public APIs (no auth)
GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"

if not DISCORD_BOT_TOKEN or not DISCORD_CHANNEL_ID:
    raise SystemExit("Missing DISCORD_BOT_TOKEN or DISCORD_CHANNEL_ID in Railway Variables")

CHANNEL_ID_INT = int(DISCORD_CHANNEL_ID)

intents = discord.Intents.default()
client = discord.Client(intents=intents)

# In-memory dedupe: token_id(s) -> last alert time + last edge
last_alert: Dict[str, Dict[str, float]] = {}  # {"key": {"t": epoch, "edge": edge}}

# Reuse HTTP connection pool
http = httpx.AsyncClient(timeout=HTTP_TIMEOUT)

def now() -> float:
    return time.time()

def _dedupe_key(event_id: Any, market_id: Any, token_ids: List[str]) -> str:
    token_part = ",".join(token_ids)
    return f"event:{event_id}|market:{market_id}|tokens:{token_part}"

async def fetch_json(url: str, params: Optional[dict] = None) -> Any:
    r = await http.get(url, params=params)
    r.raise_for_status()
    return r.json()

async def get_sports_leagues() -> List[Dict[str, Any]]:
    # docs: GET https://gamma-api.polymarket.com/sports  (no auth)
    return await fetch_json(f"{GAMMA_BASE}/sports")

async def get_active_events_for_series(series_id: int, limit: int = 100) -> List[Dict[str, Any]]:
    # docs show: /events?series_id=...&active=true&closed=false
    # We'll keep it simple and paginate with offset.
    all_events: List[Dict[str, Any]] = []
    offset = 0
    while True:
        params = {
            "series_id": series_id,
            "active": "true",
            "closed": "false",
            "limit": str(limit),
            "offset": str(offset),
            "order": "startTime",
            "ascending": "true",
        }
        batch = await fetch_json(f"{GAMMA_BASE}/events", params=params)
        if not batch:
            break
        all_events.extend(batch)
        if len(batch) < limit:
            break
        offset += limit
        await asyncio.sleep(0.05)
    return all_events

async def get_buy_price(token_id: str) -> Optional[float]:
    # docs: GET https://clob.polymarket.com/price?token_id=...&side=buy
    try:
        data = await fetch_json(f"{CLOB_BASE}/price", params={"token_id": token_id, "side": "buy"})
        p = float(data["price"])
        if p <= 0 or p >= 1:
            return None
        return p
    except Exception:
        return None

async def detect_binary_arb(event: Dict[str, Any], market: Dict[str, Any]) -> Optional[Tuple[float, str]]:
    """
    Binary arb: buy YES token + buy NO token costs < 1.0
    Profit per 1 share set = 1 - (p_yes + p_no)
    """
    token_ids = market.get("clobTokenIds")
    if not token_ids or len(token_ids) != 2:
        return None

    yes_id, no_id = token_ids[0], token_ids[1]

    p_yes, p_no = await asyncio.gather(get_buy_price(yes_id), get_buy_price(no_id))
    if p_yes is None or p_no is None:
        return None

    cost = p_yes + p_no
    edge = 1.0 - cost
    if edge < MIN_EDGE:
        return None

    title = event.get("title") or market.get("question") or "Unknown market"
    event_id = event.get("id", "?")
    market_id = market.get("id", "?")

    msg = (
        f"🚨 **Polymarket Arb Found (Binary)**\n"
        f"**{title}**\n"
        f"Cost to buy both outcomes: **{cost:.4f}**\n"
        f"Edge (profit per 1-share set): **{edge:.4f}** ({edge*100:.2f}%)\n"
        f"YES buy price: **{p_yes:.4f}** | NO buy price: **{p_no:.4f}**\n"
        f"IDs: event `{event_id}` market `{market_id}`\n"
        f"Token IDs: `{yes_id}`, `{no_id}`"
    )
    return edge, msg

def should_alert(key: str, edge: float) -> bool:
    info = last_alert.get(key)
    t = now()
    if not info:
        return True
    if t - info["t"] >= ALERT_COOLDOWN_SECONDS:
        return True
    # If edge improved meaningfully, alert again (optional)
    if edge > info["edge"] + 0.002:  # +0.2% improvement
        return True
    return False

def mark_alerted(key: str, edge: float) -> None:
    last_alert[key] = {"t": now(), "edge": edge}

async def scan_once(channel: discord.TextChannel) -> None:
    leagues = await get_sports_leagues()
    leagues = leagues[:MAX_LEAGUES]

    # Each league object usually has series_id
    series_ids = []
    for lg in leagues:
        sid = lg.get("series_id") or lg.get("seriesId") or lg.get("id")
        if isinstance(sid, int):
            series_ids.append(sid)

    if not series_ids:
        await channel.send("⚠️ No sports leagues found from /sports. (API issue?)")
        return

    # Pull events per league (concurrently, but not too hard)
    sem = asyncio.Semaphore(6)

    async def fetch_events_guarded(sid: int):
        async with sem:
            return sid, await get_active_events_for_series(sid)

    results = await asyncio.gather(*[fetch_events_guarded(sid) for sid in series_ids])

    found = 0
    checked = 0

    for sid, events in results:
        for event in events:
            markets = event.get("markets") or []
            for market in markets:
                checked += 1
                # Binary markets are the common sports ones (Yes/No or Team A/Team B)
                arb = await detect_binary_arb(event, market)
                if not arb:
                    continue

                edge, msg = arb
                token_ids = market.get("clobTokenIds") or []
                key = _dedupe_key(event.get("id", "?"), market.get("id", "?"), token_ids)

                if should_alert(key, edge):
                    await channel.send(msg)
                    mark_alerted(key, edge)
                    found += 1

                # tiny pause to be nice
                await asyncio.sleep(0.03)

    print(f"[scan] checked={checked} arbs_found={found}")

async def scan_loop() -> None:
    await client.wait_until_ready()

    # Always use fetch_channel to avoid cache issues
    channel = await client.fetch_channel(CHANNEL_ID_INT)

    await channel.send("✅ PolyBrain is live on Railway. Scanning Polymarket sports for internal arbs.")

    while True:
        try:
            await scan_once(channel)
        except Exception as e:
            print(f"[error] scan_loop: {repr(e)}")
        await asyncio.sleep(SCAN_INTERVAL_SECONDS)

@client.event
async def on_ready():
    print(f"Logged in as: {client.user} (id={client.user.id})")
    client.loop.create_task(scan_loop())

def main():
    try:
        client.run(DISCORD_BOT_TOKEN)
    finally:
        # best effort cleanup
        try:
            asyncio.get_event_loop().run_until_complete(http.aclose())
        except Exception:
            pass

if __name__ == "__main__":
    main()
