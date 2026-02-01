import os
import time
import asyncio
import logging
from typing import Any, Dict, Optional

import httpx
import discord

# ----------------------------
# ENV
# ----------------------------
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")
DISCORD_CHANNEL_ID = os.getenv("DISCORD_CHANNEL_ID", "")

if not DISCORD_BOT_TOKEN or not DISCORD_CHANNEL_ID:
    raise SystemExit("Missing DISCORD_BOT_TOKEN or DISCORD_CHANNEL_ID")

DISCORD_CHANNEL_ID_INT = int(DISCORD_CHANNEL_ID)

# Higher quality defaults (you can override in Railway Variables)
MIN_EDGE = float(os.getenv("MIN_EDGE", "0.07"))              # 7% edge
MAX_SPREAD = float(os.getenv("MAX_SPREAD", "0.05"))          # tighter liquidity
ALERT_COOLDOWN_SEC = int(os.getenv("ALERT_COOLDOWN_SEC", "1800"))  # 30 min

UNIVERSE_REFRESH_SEC = float(os.getenv("UNIVERSE_REFRESH_SEC", "20"))
SCAN_INTERVAL_SEC = float(os.getenv("SCAN_INTERVAL_SEC", "3.0"))
MAX_MARKETS = int(os.getenv("MAX_MARKETS", "1200"))

GAMMA = "https://gamma-api.polymarket.com"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)  # hide noisy httpx request logs


# ----------------------------
# Helpers
# ----------------------------
def _to_float(x: Any) -> Optional[float]:
    try:
        if x is None:
            return None
        return float(x)
    except Exception:
        return None


def clamp01(p: float) -> float:
    return max(0.0, min(1.0, p))


def now_ts() -> float:
    return time.time()


# ----------------------------
# Polymarket parsing
# ----------------------------
def extract_binary_prices(m: Dict[str, Any]) -> Optional[Dict[str, float]]:
    yes_ask = _to_float(m.get("yesBuyPrice"))
    yes_bid = _to_float(m.get("yesSellPrice"))
    no_ask = _to_float(m.get("noBuyPrice"))
    no_bid = _to_float(m.get("noSellPrice"))

    if any(v is None for v in (yes_bid, yes_ask, no_bid, no_ask)):
        outcomes = m.get("outcomes") or m.get("outcomePrices")
        if isinstance(outcomes, list):
            for o in outcomes:
                if not isinstance(o, dict):
                    continue
                name = (o.get("name") or o.get("outcome") or "").strip().lower()
                bid = _to_float(o.get("bestBid") or o.get("bid") or o.get("sell"))
                ask = _to_float(o.get("bestAsk") or o.get("ask") or o.get("buy"))
                last = _to_float(o.get("last") or o.get("lastPrice") or o.get("price"))
                if bid is None and last is not None:
                    bid = last
                if ask is None and last is not None:
                    ask = last
                if name in ("yes", "y"):
                    yes_bid = yes_bid if yes_bid is not None else bid
                    yes_ask = yes_ask if yes_ask is not None else ask
                elif name in ("no", "n"):
                    no_bid = no_bid if no_bid is not None else bid
                    no_ask = no_ask if no_ask is not None else ask

    if any(v is None for v in (yes_bid, yes_ask, no_bid, no_ask)):
        return None

    return {
        "yes_bid": clamp01(yes_bid),
        "yes_ask": clamp01(yes_ask),
        "no_bid": clamp01(no_bid),
        "no_ask": clamp01(no_ask),
    }


def fair_prob_from_books(pr: Dict[str, float]) -> Optional[float]:
    p_yes_mid = (pr["yes_bid"] + pr["yes_ask"]) / 2.0
    p_no_mid = (pr["no_bid"] + pr["no_ask"]) / 2.0
    denom = p_yes_mid + p_no_mid
    if denom <= 0:
        return None
    return clamp01(p_yes_mid / denom)


def compute_value_signal(pr: Dict[str, float]) -> Optional[Dict[str, Any]]:
    fair_yes = fair_prob_from_books(pr)
    if fair_yes is None:
        return None

    fair_no = 1.0 - fair_yes
    yes_spread = pr["yes_ask"] - pr["yes_bid"]
    no_spread = pr["no_ask"] - pr["no_bid"]

    # Liquidity filter: require at least one side liquid
    if yes_spread > MAX_SPREAD and no_spread > MAX_SPREAD:
        return None

    edge_yes = fair_yes - pr["yes_ask"]
    edge_no = fair_no - pr["no_ask"]

    if edge_yes >= MIN_EDGE and yes_spread <= MAX_SPREAD:
        entry = pr["yes_ask"]
        tp1 = max(entry, clamp01(fair_yes))
        tp2 = max(entry, clamp01(fair_yes + edge_yes))
        return {"side": "YES", "entry": entry, "fair": fair_yes, "edge": edge_yes, "tp1": tp1, "tp2": tp2, "spread": yes_spread}

    if edge_no >= MIN_EDGE and no_spread <= MAX_SPREAD:
        entry = pr["no_ask"]
        tp1 = max(entry, clamp01(fair_no))
        tp2 = max(entry, clamp01(fair_no + edge_no))
        return {"side": "NO", "entry": entry, "fair": fair_no, "edge": edge_no, "tp1": tp1, "tp2": tp2, "spread": no_spread}

    return None


# ----------------------------
# HTTP with backoff
# ----------------------------
async def http_get_json(client: httpx.AsyncClient, url: str, params: Dict[str, Any]) -> Any:
    wait = 0.8
    for attempt in range(1, 7):
        try:
            r = await client.get(url, params=params, timeout=20)
            if r.status_code == 429:
                logging.warning(f"[http] status=429 attempt={attempt}/6 wait={wait:.1f}s url={url}")
                await asyncio.sleep(wait)
                wait *= 1.9
                continue
            if r.status_code == 422:
                # Don't keep retrying the same invalid params forever
                logging.warning(f"[http] status=422 attempt={attempt}/6 (bad params) url={url}")
                raise httpx.HTTPStatusError("422", request=r.request, response=r)
            r.raise_for_status()
            return r.json()
        except httpx.HTTPStatusError as e:
            code = e.response.status_code if e.response else "?"
            logging.warning(f"[http] status={code} attempt={attempt}/6 wait={wait:.1f}s url={url}")
            await asyncio.sleep(wait)
            wait *= 1.8
        except Exception as e:
            logging.warning(f"[http] error={type(e).__name__} attempt={attempt}/6 wait={wait:.1f}s url={url}")
            await asyncio.sleep(wait)
            wait *= 1.8

    raise RuntimeError(f"Failed after retries: {url}")


# ----------------------------
# Discord bot
# ----------------------------
intents = discord.Intents.default()
client = discord.Client(intents=intents)

CHANNEL: Optional[discord.TextChannel] = None

UNIVERSE: Dict[str, Dict[str, Any]] = {}
LAST_UNIVERSE_OK: float = 0.0
LAST_ALERT_TS: Dict[str, float] = {}


def should_alert(market_id: str) -> bool:
    last = LAST_ALERT_TS.get(market_id, 0.0)
    return (now_ts() - last) >= ALERT_COOLDOWN_SEC


def mark_alert(market_id: str) -> None:
    LAST_ALERT_TS[market_id] = now_ts()


def fmt_pct(x: float) -> str:
    return f"{x*100:.1f}%"


def market_title(m: Dict[str, Any]) -> str:
    return m.get("question") or m.get("title") or m.get("name") or m.get("slug") or f"Market {m.get('id')}"


def market_link(m: Dict[str, Any]) -> str:
    slug = m.get("slug")
    mid = m.get("id")
    if slug:
        return f"https://polymarket.com/market/{slug}"
    if mid:
        return f"https://polymarket.com/market/{mid}"
    return "https://polymarket.com"


async def send_alert(text: str) -> None:
    global CHANNEL
    if CHANNEL is None:
        CHANNEL = client.get_channel(DISCORD_CHANNEL_ID_INT)
        if CHANNEL is None:
            CHANNEL = await client.fetch_channel(DISCORD_CHANNEL_ID_INT)
    await CHANNEL.send(text)


def build_value_message(m: Dict[str, Any], sig: Dict[str, Any]) -> str:
    title = market_title(m)
    link = market_link(m)
    side = sig["side"]
    return (
        f"📊 **HIGH-QUALITY VALUE — {side}**\n"
        f"**{title}**\n{link}\n\n"
        f"• Entry (ask): **{fmt_pct(sig['entry'])}**\n"
        f"• Fair (vig-adjusted): **{fmt_pct(sig['fair'])}**\n"
        f"• Edge: **+{fmt_pct(sig['edge'])}**\n"
        f"• Spread: **{fmt_pct(sig['spread'])}**\n\n"
        f"🎯 **Plan**\n"
        f"• Sell **50%** at **{fmt_pct(sig['tp1'])}**\n"
        f"• Sell **50%** at **{fmt_pct(sig['tp2'])}**\n"
        f"_Not financial advice._"
    )


# ----------------------------
# Universe refresh + scan loops
# ----------------------------
async def fetch_markets_page(http: httpx.AsyncClient, offset: int, limit: int) -> Any:
    """
    IMPORTANT FIX:
    Start with SIMPLE params (no order/ascending) to avoid 422.
    If needed later we can re-add ordering, but simple works most reliably.
    """
    params_simple = {
        "active": "true",
        "closed": "false",
        "limit": str(limit),
        "offset": str(offset),
    }
    return await http_get_json(http, f"{GAMMA}/markets", params=params_simple)


async def refresh_universe_loop() -> None:
    global UNIVERSE, LAST_UNIVERSE_OK

    async with httpx.AsyncClient(headers={"User-Agent": "polybrain/1.0"}) as http:
        while True:
            try:
                all_markets = []
                limit = 200
                offset = 0

                while offset < MAX_MARKETS:
                    data = await fetch_markets_page(http, offset, limit)
                    if not isinstance(data, list) or not data:
                        break
                    all_markets.extend(data)
                    if len(data) < limit:
                        break
                    offset += limit
                    await asyncio.sleep(0.25)

                new_universe: Dict[str, Dict[str, Any]] = {}
                for m in all_markets:
                    mid = m.get("id")
                    if mid is None:
                        continue
                    new_universe[str(mid)] = m

                if new_universe:
                    UNIVERSE = new_universe
                    LAST_UNIVERSE_OK = now_ts()
                    logging.info(f"[universe] markets={len(UNIVERSE)} refresh_ok")
                else:
                    logging.warning("[universe] got 0 markets (keeping previous cache)")

            except Exception as e:
                age = now_ts() - LAST_UNIVERSE_OK if LAST_UNIVERSE_OK else -1
                logging.error(f"[universe] failed: {type(e).__name__} (cache_age={age:.0f}s)")

            await asyncio.sleep(UNIVERSE_REFRESH_SEC)


async def scan_loop() -> None:
    while True:
        try:
            checked = 0
            value_found = 0
            alerted = 0

            for mid, m in list(UNIVERSE.items()):
                checked += 1

                pr = extract_binary_prices(m)
                if pr is None:
                    continue

                sig = compute_value_signal(pr)
                if sig is None:
                    continue

                value_found += 1

                if should_alert(mid):
                    await send_alert(build_value_message(m, sig))
                    mark_alert(mid)
                    alerted += 1

            logging.info(f"[scan] checked={checked} value_found={value_found} alerted={alerted}")

        except Exception as e:
            logging.error(f"[scan] loop error: {type(e).__name__}")

        await asyncio.sleep(SCAN_INTERVAL_SEC)


@client.event
async def on_ready():
    logging.info(f"Logged in as: {client.user}")
    await send_alert("✅ PolyBrain is live. **Option A (HIGH quality)** is ON.")
    client.loop.create_task(refresh_universe_loop())
    client.loop.create_task(scan_loop())


client.run(DISCORD_BOT_TOKEN)
