import os
import time
import asyncio
import logging
from typing import Any, Dict, Optional, Tuple

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

# Optional tuning knobs (safe defaults)
MIN_EDGE = float(os.getenv("MIN_EDGE", "0.04"))         # 4% edge required
MAX_SPREAD = float(os.getenv("MAX_SPREAD", "0.10"))     # ignore wide spreads
ALERT_COOLDOWN_SEC = int(os.getenv("ALERT_COOLDOWN_SEC", "900"))  # 15 min per market
UNIVERSE_REFRESH_SEC = float(os.getenv("UNIVERSE_REFRESH_SEC", "12"))
SCAN_INTERVAL_SEC = float(os.getenv("SCAN_INTERVAL_SEC", "2.5"))
MAX_MARKETS = int(os.getenv("MAX_MARKETS", "1200"))

GAMMA = "https://gamma-api.polymarket.com"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

# ----------------------------
# Helpers: robust numeric parsing
# ----------------------------
def _to_float(x: Any) -> Optional[float]:
    try:
        if x is None:
            return None
        return float(x)
    except Exception:
        return None


def clamp01(p: float) -> float:
    if p < 0.0:
        return 0.0
    if p > 1.0:
        return 1.0
    return p


def now_ts() -> float:
    return time.time()


# ----------------------------
# Polymarket parsing
# ----------------------------
def extract_binary_prices(m: Dict[str, Any]) -> Optional[Dict[str, float]]:
    """
    Tries to extract YES/NO best bid/ask from common gamma fields.
    Works across slight API differences by trying multiple keys.

    Returns:
      {
        "yes_bid": float,
        "yes_ask": float,
        "no_bid": float,
        "no_ask": float
      }
    or None if missing.
    """
    # Common keys seen in gamma data / variants:
    # yesBuyPrice  = price to buy YES (ask)
    # yesSellPrice = price to sell YES (bid)
    # noBuyPrice   = price to buy NO  (ask)
    # noSellPrice  = price to sell NO (bid)

    yes_ask = _to_float(m.get("yesBuyPrice"))
    yes_bid = _to_float(m.get("yesSellPrice"))
    no_ask = _to_float(m.get("noBuyPrice"))
    no_bid = _to_float(m.get("noSellPrice"))

    # Some variants use bestBid/bestAsk inside outcome objects; try “outcomes” list
    if any(v is None for v in (yes_bid, yes_ask, no_bid, no_ask)):
        outcomes = m.get("outcomes") or m.get("outcomePrices") or None
        # If outcomes are dict-ish entries, we try to find YES/NO
        if isinstance(outcomes, list):
            for o in outcomes:
                if not isinstance(o, dict):
                    continue
                name = (o.get("name") or o.get("outcome") or "").strip().lower()
                bid = _to_float(o.get("bestBid") or o.get("bid") or o.get("sell"))
                ask = _to_float(o.get("bestAsk") or o.get("ask") or o.get("buy"))
                last = _to_float(o.get("last") or o.get("lastPrice") or o.get("price"))
                # If we only have last, approximate bid/ask tightly
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

    # sanity clamp
    yes_bid = clamp01(yes_bid)
    yes_ask = clamp01(yes_ask)
    no_bid = clamp01(no_bid)
    no_ask = clamp01(no_ask)

    return {"yes_bid": yes_bid, "yes_ask": yes_ask, "no_bid": no_bid, "no_ask": no_ask}


def fair_prob_from_books(pr: Dict[str, float]) -> Optional[float]:
    """
    Option A “fair value” (vig/overround adjusted) using BOTH sides:
      p_yes_mid = (yes_bid + yes_ask)/2
      p_no_mid  = (no_bid  + no_ask)/2
    Normalize:
      fair_yes = p_yes_mid / (p_yes_mid + p_no_mid)

    This removes “sum != 1” friction (fees/spread/overround).
    """
    p_yes_mid = (pr["yes_bid"] + pr["yes_ask"]) / 2.0
    p_no_mid = (pr["no_bid"] + pr["no_ask"]) / 2.0
    denom = p_yes_mid + p_no_mid
    if denom <= 0:
        return None
    return clamp01(p_yes_mid / denom)


def compute_value_signal(pr: Dict[str, float]) -> Optional[Dict[str, Any]]:
    """
    Detect value opportunities:
      - BUY YES if yes_ask is meaningfully below fair_yes
      - BUY NO  if no_ask  is meaningfully below fair_no
    with liquidity checks (spread).
    """
    fair_yes = fair_prob_from_books(pr)
    if fair_yes is None:
        return None

    fair_no = 1.0 - fair_yes

    yes_spread = pr["yes_ask"] - pr["yes_bid"]
    no_spread = pr["no_ask"] - pr["no_bid"]

    # Liquidity filter
    if yes_spread > MAX_SPREAD and no_spread > MAX_SPREAD:
        return None

    # Edge definition (how far below fair you can BUY)
    edge_yes = fair_yes - pr["yes_ask"]
    edge_no = fair_no - pr["no_ask"]

    # pick best side
    if edge_yes >= MIN_EDGE and yes_spread <= MAX_SPREAD:
        entry = pr["yes_ask"]
        tp1 = clamp01(fair_yes)  # first target at fair
        tp2 = clamp01(fair_yes + edge_yes)  # second target extends by the edge size
        return {
            "side": "YES",
            "entry": entry,
            "fair": fair_yes,
            "edge": edge_yes,
            "tp1": max(entry, tp1),
            "tp2": max(entry, tp2),
            "spread": yes_spread,
        }

    if edge_no >= MIN_EDGE and no_spread <= MAX_SPREAD:
        entry = pr["no_ask"]
        tp1 = clamp01(fair_no)
        tp2 = clamp01(fair_no + edge_no)
        return {
            "side": "NO",
            "entry": entry,
            "fair": fair_no,
            "edge": edge_no,
            "tp1": max(entry, tp1),
            "tp2": max(entry, tp2),
            "spread": no_spread,
        }

    return None


# ----------------------------
# HTTP with backoff
# ----------------------------
async def http_get_json(client: httpx.AsyncClient, url: str, params: Dict[str, Any]) -> Any:
    """
    Backoff on 429 / transient errors.
    """
    wait = 0.8
    for attempt in range(1, 7):
        try:
            r = await client.get(url, params=params, timeout=20)
            if r.status_code == 429:
                logging.warning(f"[http] status=429 attempt={attempt}/6 wait={wait:.1f}s url={url}")
                await asyncio.sleep(wait)
                wait *= 1.9
                continue
            r.raise_for_status()
            return r.json()
        except httpx.HTTPStatusError as e:
            code = e.response.status_code
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

# Global in-memory universe cache
UNIVERSE: Dict[str, Dict[str, Any]] = {}  # key=market_id(str), value=market dict
LAST_UNIVERSE_OK: float = 0.0

# Alert cooldown
LAST_ALERT_TS: Dict[str, float] = {}  # key=market_id -> timestamp


def market_title(m: Dict[str, Any]) -> str:
    # Try common title-ish fields
    return (
        m.get("question")
        or m.get("title")
        or m.get("name")
        or m.get("slug")
        or f"Market {m.get('id')}"
    )


def market_link(m: Dict[str, Any]) -> str:
    # Polymarket UI links differ; this is a best-effort generic.
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


def should_alert(market_id: str) -> bool:
    last = LAST_ALERT_TS.get(market_id, 0.0)
    return (now_ts() - last) >= ALERT_COOLDOWN_SEC


def mark_alert(market_id: str) -> None:
    LAST_ALERT_TS[market_id] = now_ts()


def fmt_pct(x: float) -> str:
    return f"{x*100:.1f}%"


def build_value_message(m: Dict[str, Any], sig: Dict[str, Any]) -> str:
    title = market_title(m)
    link = market_link(m)

    side = sig["side"]
    entry = sig["entry"]
    fair = sig["fair"]
    edge = sig["edge"]
    tp1 = sig["tp1"]
    tp2 = sig["tp2"]
    spread = sig["spread"]

    # Convert prices -> probs; in Polymarket “price” ~ probability
    return (
        f"📊 **VALUE ALERT — {side}**\n"
        f"**{title}**\n"
        f"{link}\n\n"
        f"• Market entry (ask): **{fmt_pct(entry)}**\n"
        f"• Fair value (vig-adjusted): **{fmt_pct(fair)}**\n"
        f"• Edge: **+{fmt_pct(edge)}**\n"
        f"• Spread: **{fmt_pct(spread)}**\n\n"
        f"🎯 **Plan**\n"
        f"• Buy {side} at **≤ {fmt_pct(entry)}**\n"
        f"• Sell **50%** at **{fmt_pct(tp1)}**\n"
        f"• Sell remaining **50%** at **{fmt_pct(tp2)}**\n\n"
        f"Confidence: **HIGH** (orderbook/vig adjusted)\n"
        f"_Reminder: This is not financial advice._"
    )


# ----------------------------
# Universe refresh + scan loops
# ----------------------------
async def refresh_universe_loop() -> None:
    global UNIVERSE, LAST_UNIVERSE_OK

    async with httpx.AsyncClient(headers={"User-Agent": "polybrain/1.0"}) as http:
        while True:
            try:
                params = {
                    "active": "true",
                    "closed": "false",
                    "limit": str(min(MAX_MARKETS, 200)),
                    "offset": "0",
                    "order": "startTime",
                    "ascending": "true",
                }

                all_markets = []
                offset = 0

                # paginate up to MAX_MARKETS
                while offset < MAX_MARKETS:
                    params["offset"] = str(offset)
                    data = await http_get_json(http, f"{GAMMA}/markets", params=params)
                    if not isinstance(data, list) or not data:
                        break
                    all_markets.extend(data)
                    if len(data) < int(params["limit"]):
                        break
                    offset += int(params["limit"])
                    await asyncio.sleep(0.25)

                # Build cache
                new_universe: Dict[str, Dict[str, Any]] = {}
                for m in all_markets:
                    mid = m.get("id")
                    if mid is None:
                        continue
                    new_universe[str(mid)] = m

                if new_universe:
                    UNIVERSE = new_universe
                    LAST_UNIVERSE_OK = now_ts()

                logging.info(f"[universe] markets={len(UNIVERSE)} refresh={UNIVERSE_REFRESH_SEC:.1f}s")

            except Exception as e:
                # Keep last known good universe
                age = now_ts() - LAST_UNIVERSE_OK if LAST_UNIVERSE_OK else -1
                logging.error(f"[universe] failed: {type(e).__name__} (cache_age={age:.0f}s)")

            await asyncio.sleep(UNIVERSE_REFRESH_SEC)


async def scan_loop() -> None:
    """
    Scan cached markets and send VALUE alerts when:
      - binary market detected (YES/NO books)
      - fair value found (vig-adjusted)
      - edge >= MIN_EDGE
      - cooldown passed
    """
    while True:
        try:
            checked = 0
            value_found = 0

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
                    try:
                        msg = build_value_message(m, sig)
                        await send_alert(msg)
                        mark_alert(mid)
                        logging.info(
                            f"[alert] market={mid} side={sig['side']} edge={sig['edge']:.4f} entry={sig['entry']:.4f}"
                        )
                    except Exception as e:
                        logging.error(f"[alert] failed market={mid}: {type(e).__name__}")

            logging.info(f"[scan] checked={checked} value_found={value_found}")

        except Exception as e:
            logging.error(f"[scan] loop error: {type(e).__name__}")

        await asyncio.sleep(SCAN_INTERVAL_SEC)


@client.event
async def on_ready():
    logging.info(f"Logged in as: {client.user}")

    # Resolve channel once at startup
    global CHANNEL
    CHANNEL = client.get_channel(DISCORD_CHANNEL_ID_INT)
    if CHANNEL is None:
        CHANNEL = await client.fetch_channel(DISCORD_CHANNEL_ID_INT)

    await send_alert("✅ PolyBrain is live. **Option A (Fair Value Engine)** is ON.")

    # Start loops
    client.loop.create_task(refresh_universe_loop())
    client.loop.create_task(scan_loop())


client.run(DISCORD_BOT_TOKEN)
