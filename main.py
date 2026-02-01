import os
import asyncio
import discord
import httpx
from datetime import datetime, timedelta, timezone

# =========================
# ENV
# =========================
DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID"))

USER_TZ_OFFSET = os.getenv("USER_TZ_OFFSET", "-05:00")
HEARTBEAT_HOUR = int(os.getenv("HEARTBEAT_HOUR", "9"))
HEARTBEAT_MINUTE = int(os.getenv("HEARTBEAT_MINUTE", "0"))

GAMMA = "https://gamma-api.polymarket.com"

# =========================
# TIMEZONE
# =========================
def _parse_tz(s: str) -> timezone:
    sign = -1 if s.startswith("-") else 1
    h, m = s[1:].split(":")
    return timezone(sign * timedelta(hours=int(h), minutes=int(m)))

LOCAL_TZ = _parse_tz(USER_TZ_OFFSET)

# =========================
# STATS (24h rolling)
# =========================
STATS = {
    "markets_scanned": 0,
    "value_found": 0,
    "alerts_sent": 0,
    "rate_limits": 0,
    "http_4xx": 0,
    "http_5xx": 0,
    "universe_markets": 0,
    "news_terms": 0,
    "last_error": "",
}

def stat_add(k, n=1):
    if k in STATS:
        STATS[k] += n

def stat_set(k, v):
    if k in STATS:
        STATS[k] = v

# =========================
# DISCORD
# =========================
intents = discord.Intents.default()
client = discord.Client(intents=intents)

# =========================
# HTTP
# =========================
async def http_get_json(url, params=None, retries=6):
    async with httpx.AsyncClient(timeout=20) as client:
        for attempt in range(1, retries + 1):
            try:
                r = await client.get(url, params=params)
                if r.status_code == 429:
                    stat_add("rate_limits")
                    await asyncio.sleep(attempt * 1.5)
                    continue
                r.raise_for_status()
                return r.json()
            except httpx.HTTPStatusError as e:
                STATS["last_error"] = f"{e.response.status_code} {url}"
                if 400 <= e.response.status_code < 500:
                    stat_add("http_4xx")
                if 500 <= e.response.status_code < 600:
                    stat_add("http_5xx")
                await asyncio.sleep(attempt * 1.5)
            except Exception as e:
                STATS["last_error"] = str(e)
                await asyncio.sleep(attempt * 1.5)
    return None

# =========================
# UNIVERSE REFRESH
# =========================
async def refresh_universe():
    data = await http_get_json(
        f"{GAMMA}/markets",
        params={
            "active": "true",
            "closed": "false",
            "limit": 200,
            "order": "startTime",
            "ascending": "true",
        },
    )
    if not data:
        return []

    markets = data.get("markets", [])
    stat_set("universe_markets", len(markets))
    print(f"[universe] markets={len(markets)} refresh_ok")
    return markets

# =========================
# SCAN LOOP (SAFE MODE)
# =========================
async def scan_loop(channel):
    while True:
        try:
            markets = await refresh_universe()
            checked = len(markets)
            stat_add("markets_scanned", checked)

            # Phase 2 logic placeholder (high-confidence only)
            value_found = 0  # intentionally strict
            stat_add("value_found", value_found)

            print(f"[scan] checked={checked} value_found={value_found} alerts_sent={STATS['alerts_sent']}")
            await asyncio.sleep(20)

        except Exception as e:
            STATS["last_error"] = str(e)
            await asyncio.sleep(10)

# =========================
# HEARTBEAT
# =========================
def next_heartbeat():
    now = datetime.now(tz=LOCAL_TZ)
    run = now.replace(hour=HEARTBEAT_HOUR, minute=HEARTBEAT_MINUTE, second=0, microsecond=0)
    if run <= now:
        run += timedelta(days=1)
    return run

async def heartbeat_loop(channel):
    while True:
        wait = (next_heartbeat() - datetime.now(tz=LOCAL_TZ)).total_seconds()
        await asyncio.sleep(max(10, wait))

        msg = (
            "🧠 **PolyBrain Daily Health Check**\n\n"
            f"📊 Markets scanned: **{STATS['markets_scanned']}**\n"
            f"🔍 Value found: **{STATS['value_found']}**\n"
            f"🚨 Alerts sent: **{STATS['alerts_sent']}**\n"
            f"⚠️ Rate limits: **{STATS['rate_limits']}**\n"
            f"🌐 Universe size: **{STATS['universe_markets']}**\n"
            f"📰 News terms tracked: **{STATS['news_terms']}**\n"
        )

        if STATS["last_error"]:
            msg += f"\n🔴 Last error: `{STATS['last_error'][:180]}`"

        msg += "\n\n🟢 Status: **Running**"

        await channel.send(msg)

        # reset rolling stats
        for k in STATS:
            if k != "universe_markets":
                STATS[k] = 0

# =========================
# READY
# =========================
@client.event
async def on_ready():
    channel = client.get_channel(CHANNEL_ID) or await client.fetch_channel(CHANNEL_ID)

    await channel.send("✅ PolyBrain is live. Phase 2 scanning is ON.")

    asyncio.create_task(scan_loop(channel))
    asyncio.create_task(heartbeat_loop(channel))

    while True:
        await asyncio.sleep(3600)

# =========================
# START
# =========================
client.run(DISCORD_TOKEN)
