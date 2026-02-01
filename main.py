import os
import re
import time
import math
import asyncio
import logging
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, timezone, timedelta
import xml.etree.ElementTree as ET

import httpx
import discord

# =========================
# CONFIG (env vars)
# =========================
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()
DISCORD_CHANNEL_ID = os.getenv("DISCORD_CHANNEL_ID", "").strip()
if not DISCORD_BOT_TOKEN or not DISCORD_CHANNEL_ID:
    raise SystemExit("Missing DISCORD_BOT_TOKEN or DISCORD_CHANNEL_ID")
CHANNEL_ID = int(DISCORD_CHANNEL_ID)

GAMMA = os.getenv("POLY_GAMMA_BASE", "https://gamma-api.polymarket.com").strip().rstrip("/")

# Only what you see in-app (NO UFC)
SPORTS_MODE = os.getenv("SPORTS_MODE", "nfl,nba,nhl,cbb,cfb").strip().lower()
SPORTS_ALLOW = {s.strip() for s in SPORTS_MODE.split(",") if s.strip()}

# RSS feeds (you can change anytime)
# Put comma-separated RSS urls in Railway env var NEWS_RSS_URLS if you want.
DEFAULT_RSS = [
    "https://www.espn.com/espn/rss/news",            # general sports headlines
    "https://www.espn.com/espn/rss/nfl/news",
    "https://www.espn.com/espn/rss/nba/news",
    "https://www.espn.com/espn/rss/nhl/news",
    "https://www.espn.com/espn/rss/ncb/news",
]
NEWS_RSS_URLS = os.getenv("NEWS_RSS_URLS", "").strip()
RSS_URLS = [u.strip() for u in (NEWS_RSS_URLS.split(",") if NEWS_RSS_URLS else DEFAULT_RSS) if u.strip()]

# Scan tuning
UNIVERSE_REFRESH_SECS = float(os.getenv("UNIVERSE_REFRESH_SECS", "60"))     # refresh market list
SCAN_INTERVAL_SECS = float(os.getenv("SCAN_INTERVAL_SECS", "12"))           # scan loop pace
NEWS_REFRESH_SECS = float(os.getenv("NEWS_REFRESH_SECS", "180"))            # refresh RSS
NEWS_MAX_ITEMS = int(os.getenv("NEWS_MAX_ITEMS", "250"))                    # keep latest N news items
NEWS_KEYWORDS_MAX = int(os.getenv("NEWS_KEYWORDS_MAX", "120"))              # extracted keywords cap

# Alert thresholds (Option A = higher quality / fewer alerts)
ARB_EDGE_MIN = float(os.getenv("ARB_EDGE_MIN", "0.010"))                    # 1.0% arb edge
MOVE_PCT_MIN = float(os.getenv("MOVE_PCT_MIN", "0.18"))                     # 18% move from last seen price
INTEL_MATCH_MIN = int(os.getenv("INTEL_MATCH_MIN", "2"))                    # require >=2 keyword matches

# Daily heartbeat
HEARTBEAT_HOUR_LOCAL = int(os.getenv("HEARTBEAT_HOUR_LOCAL", "9"))          # local hour
HEARTBEAT_MINUTE_LOCAL = int(os.getenv("HEARTBEAT_MINUTE_LOCAL", "30"))     # local minute
LOCAL_TZ_OFFSET_HOURS = int(os.getenv("LOCAL_TZ_OFFSET_HOURS", "-5"))       # default EST-like

# =========================
# LOGGING
# =========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("polybrain")

# =========================
# STATE
# =========================
STATS: Dict[str, Any] = {
    "markets_scanned": 0,
    "value_found": 0,
    "alerts_sent": 0,
    "rate_limits": 0,
    "universe_markets": 0,
    "news_terms": 0,
    "http_4xx": 0,
    "http_5xx": 0,
    "last_error": "",
}

# Universe + price memory
UNIVERSE: Dict[str, Dict[str, Any]] = {}          # market_id -> market payload
LAST_PRICE: Dict[str, float] = {}                 # market_id -> last observed "yes" price estimate
LAST_SEEN_AT: Dict[str, float] = {}               # market_id -> timestamp seconds

# News / injuries intelligence
NEWS_ITEMS: List[Dict[str, Any]] = []             # latest parsed RSS items
NEWS_KEYWORDS: Dict[str, int] = {}                # keyword -> count (recent window)

# Discord
intents = discord.Intents.default()
client = discord.Client(intents=intents)

# =========================
# HELPERS
# =========================
def now_ts() -> float:
    return time.time()

def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))

def local_now() -> datetime:
    return datetime.now(timezone(timedelta(hours=LOCAL_TZ_OFFSET_HOURS)))

def safe_text(s: Any) -> str:
    return (str(s) if s is not None else "").strip()

def normalize_word(w: str) -> str:
    w = w.lower()
    w = re.sub(r"[^a-z0-9\-\s]", "", w)
    w = w.strip()
    return w

def extract_keywords(text: str) -> List[str]:
    """
    Very simple keyword extraction from headlines.
    We keep tokens that are:
      - 3+ chars
      - not purely numbers
      - not common stopwords
    """
    stop = {
        "the","and","for","with","from","that","this","into","over","under","after","before","when","what",
        "your","youre","their","they","them","will","would","could","should","been","are","was","were","its",
        "his","her","him","she","he","to","of","in","on","at","as","by","is","it","a","an","or","vs","v",
        "game","games","match","season","team","teams","player","players","coach","coaches","report","reports",
        "today","live","final","wins","win","loss","losses","injury","injured","out","questionable","doubtful",
    }
    tokens = re.split(r"\s+", re.sub(r"[\(\)\[\]\{\}\:\,\.\!\?\|\/\\\-\_\"\'\+\=\*]", " ", text.lower()))
    out: List[str] = []
    for t in tokens:
        t = normalize_word(t)
        if len(t) < 3:
            continue
        if t.isdigit():
            continue
        if t in stop:
            continue
        out.append(t)
    return out

def market_text(m: Dict[str, Any]) -> str:
    # We try multiple fields so it works across small schema changes.
    fields = [
        safe_text(m.get("question")),
        safe_text(m.get("title")),
        safe_text(m.get("name")),
        safe_text(m.get("description")),
    ]
    return " ".join([f for f in fields if f])

def allowed_sport(m: Dict[str, Any]) -> bool:
    """
    We filter markets by checking if league keywords appear in the market text.
    This matches what you see in-app (NFL/NBA/NHL/CBB/CFB) and excludes UFC.
    """
    t = market_text(m).lower()
    # hard exclude UFC
    if "ufc" in t or "mma" in t:
        return False

    # If user empties SPORTS_ALLOW, allow all.
    if not SPORTS_ALLOW:
        return True

    # Map allow-list keys to common variations
    variants = {
        "nfl": ["nfl", "super bowl"],
        "nba": ["nba"],
        "nhl": ["nhl", "stanley cup"],
        "cbb": ["cbb", "ncaa basketball", "march madness", "ncaab"],
        "cfb": ["cfb", "college football", "ncaa football", "ncaaf"],
    }
    for k in SPORTS_ALLOW:
        for v in variants.get(k, [k]):
            if v in t:
                return True

    return False

async def send_discord(message: str) -> None:
    try:
        channel = client.get_channel(CHANNEL_ID)
        if channel is None:
            channel = await client.fetch_channel(CHANNEL_ID)
        await channel.send(message)
    except Exception as e:
        STATS["last_error"] = f"discord_send: {type(e).__name__}: {e}"
        log.error("Discord send failed: %s", STATS["last_error"])

async def http_get_json(client_http: httpx.AsyncClient, url: str, params: Optional[Dict[str, Any]] = None) -> Any:
    """
    Robust GET w/ exponential backoff.
    Handles 429 / 422 without killing the bot.
    """
    tries = 6
    backoff = 0.8
    for attempt in range(1, tries + 1):
        try:
            r = await client_http.get(url, params=params, timeout=20.0)
            status = r.status_code

            if 400 <= status < 500:
                STATS["http_4xx"] += 1
            if 500 <= status:
                STATS["http_5xx"] += 1

            if status == 429:
                STATS["rate_limits"] += 1
                wait = backoff * (1.8 ** (attempt - 1))
                log.warning("[http] status=429 attempt=%s/%s wait=%.1fs url=%s", attempt, tries, wait, url)
                await asyncio.sleep(wait)
                continue

            # 422 sometimes happens if params are not accepted; treat as soft-fail.
            if status == 422:
                wait = backoff * (1.8 ** (attempt - 1))
                log.warning("[http] status=422 attempt=%s/%s wait=%.1fs url=%s", attempt, tries, wait, url)
                await asyncio.sleep(wait)
                continue

            r.raise_for_status()
            return r.json()

        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            wait = backoff * (1.6 ** (attempt - 1))
            log.warning("[http] status=%s attempt=%s/%s wait=%.1fs url=%s", status, attempt, tries, wait, url)
            await asyncio.sleep(wait)
            continue
        except Exception as e:
            wait = backoff * (1.6 ** (attempt - 1))
            log.warning("[http] error=%s attempt=%s/%s wait=%.1fs url=%s", type(e).__name__, attempt, tries, wait, url)
            await asyncio.sleep(wait)
            continue

    raise RuntimeError(f"HTTP failed after retries: {url}")

# =========================
# POLYMARKET UNIVERSE
# =========================
async def fetch_markets(client_http: httpx.AsyncClient, max_pages: int = 10, page_size: int = 200) -> List[Dict[str, Any]]:
    """
    Uses /markets endpoint with simple params that are less likely to 422.
    Paginates with offset.
    """
    all_markets: List[Dict[str, Any]] = []
    for page in range(max_pages):
        offset = page * page_size
        params = {
            "active": "true",
            "closed": "false",
            "limit": page_size,
            "offset": offset,
        }
        url = f"{GAMMA}/markets"
        data = await http_get_json(client_http, url, params=params)

        markets = data if isinstance(data, list) else data.get("markets") or data.get("data") or []
        if not isinstance(markets, list):
            markets = []

        all_markets.extend(markets)
        if len(markets) < page_size:
            break

    return all_markets

def extract_yes_price(m: Dict[str, Any]) -> Optional[float]:
    """
    Best-effort 'yes' price estimate.
    Polymarket schemas vary; we try common fields.
    """
    # 1) outcomePrices aligned with outcomes list
    outcomes = m.get("outcomes")
    outcome_prices = m.get("outcomePrices") or m.get("outcome_prices")
    if isinstance(outcomes, list) and isinstance(outcome_prices, list) and len(outcomes) == len(outcome_prices):
        # Find index of 'Yes'
        for i, o in enumerate(outcomes):
            if safe_text(o).lower() == "yes":
                try:
                    return float(outcome_prices[i])
                except Exception:
                    pass

    # 2) if market has 'yesPrice'
    for k in ["yesPrice", "yes_price", "bestAsk", "best_ask"]:
        if k in m:
            try:
                return float(m[k])
            except Exception:
                pass

    # 3) fallback: try 'lastTradePrice' / 'last_price'
    for k in ["lastTradePrice", "last_trade_price", "last_price"]:
        if k in m:
            try:
                return float(m[k])
            except Exception:
                pass

    return None

def compute_simple_arb_edge(m: Dict[str, Any]) -> Optional[float]:
    """
    Basic two-outcome arb check:
      edge = (1 - (yes + no)) where yes/no are prices (0..1).
    Only works if we can infer both prices.
    """
    outcomes = m.get("outcomes")
    outcome_prices = m.get("outcomePrices") or m.get("outcome_prices")
    if not (isinstance(outcomes, list) and isinstance(outcome_prices, list) and len(outcomes) == len(outcome_prices)):
        return None

    yes_p = None
    no_p = None
    for i, o in enumerate(outcomes):
        ol = safe_text(o).lower()
        try:
            p = float(outcome_prices[i])
        except Exception:
            continue
        if ol == "yes":
            yes_p = p
        elif ol == "no":
            no_p = p

    if yes_p is None or no_p is None:
        return None

    s = yes_p + no_p
    if s <= 0:
        return None
    return 1.0 - s

async def refresh_universe_loop() -> None:
    async with httpx.AsyncClient(headers={"User-Agent": "PolyBrain/1.0"}) as client_http:
        while True:
            try:
                markets = await fetch_markets(client_http)
                filtered = [m for m in markets if allowed_sport(m)]

                UNIVERSE.clear()
                for m in filtered:
                    mid = safe_text(m.get("id") or m.get("marketId") or m.get("market_id"))
                    if not mid:
                        continue
                    UNIVERSE[mid] = m

                STATS["universe_markets"] = len(UNIVERSE)
                log.info("[universe] markets=%s refresh_ok", STATS["universe_markets"])

            except Exception as e:
                STATS["last_error"] = f"universe: {type(e).__name__}: {e}"
                log.error("[universe] failed: %s", STATS["last_error"])

            await asyncio.sleep(UNIVERSE_REFRESH_SECS)

# =========================
# NEWS / INJURY INTELLIGENCE (RSS)
# =========================
def parse_rss(xml_text: str) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    try:
        root = ET.fromstring(xml_text)
        # RSS commonly: rss/channel/item
        for item in root.findall(".//item"):
            title = safe_text(item.findtext("title"))
            link = safe_text(item.findtext("link"))
            pub = safe_text(item.findtext("pubDate"))
            desc = safe_text(item.findtext("description"))
            if title:
                items.append({"title": title, "link": link, "pubDate": pub, "description": desc})
    except Exception:
        pass
    return items

async def refresh_news_loop() -> None:
    global NEWS_ITEMS, NEWS_KEYWORDS

    async with httpx.AsyncClient(headers={"User-Agent": "PolyBrain/1.0"}) as client_http:
        while True:
            try:
                all_items: List[Dict[str, Any]] = []
                for url in RSS_URLS:
                    try:
                        r = await client_http.get(url, timeout=20.0)
                        r.raise_for_status()
                        all_items.extend(parse_rss(r.text))
                    except Exception:
                        continue

                # Keep latest unique by title+link
                seen = set()
                uniq: List[Dict[str, Any]] = []
                for it in all_items:
                    key = (it.get("title",""), it.get("link",""))
                    if key in seen:
                        continue
                    seen.add(key)
                    uniq.append(it)

                uniq = uniq[:NEWS_MAX_ITEMS]
                NEWS_ITEMS = uniq

                # Build keyword counts (simple rolling window from latest items)
                kw_counts: Dict[str, int] = {}
                for it in NEWS_ITEMS[:min(len(NEWS_ITEMS), 120)]:
                    text = f"{it.get('title','')} {it.get('description','')}"
                    for kw in extract_keywords(text):
                        kw_counts[kw] = kw_counts.get(kw, 0) + 1

                # Trim to top N
                top = sorted(kw_counts.items(), key=lambda x: x[1], reverse=True)[:NEWS_KEYWORDS_MAX]
                NEWS_KEYWORDS = dict(top)
                STATS["news_terms"] = len(NEWS_KEYWORDS)

                log.info("[news] terms=%s feeds=%s", STATS["news_terms"], len(RSS_URLS))

            except Exception as e:
                STATS["last_error"] = f"news: {type(e).__name__}: {e}"
                log.error("[news] failed: %s", STATS["last_error"])

            await asyncio.sleep(NEWS_REFRESH_SECS)

def intel_matches(m: Dict[str, Any]) -> Tuple[int, List[str]]:
    """
    Returns (match_count, matched_keywords) comparing market text to recent keyword set.
    """
    t = market_text(m).lower()
    matched: List[str] = []
    for kw in NEWS_KEYWORDS.keys():
        if kw and kw in t:
            matched.append(kw)
    return (len(matched), matched[:8])

# =========================
# SCANNING + ALERTS
# =========================
def make_market_url(m: Dict[str, Any]) -> str:
    # best effort; often gamma includes slug
    slug = safe_text(m.get("slug"))
    if slug:
        return f"https://polymarket.com/market/{slug}"
    return "https://polymarket.com"

def fmt_price(p: Optional[float]) -> str:
    if p is None:
        return "n/a"
    return f"{p*100:.1f}¢"

async def scan_loop() -> None:
    await client.wait_until_ready()
    await asyncio.sleep(2)

    while True:
        try:
            markets = list(UNIVERSE.items())
            if not markets:
                log.info("[scan] checked=0 universe=0 news_terms=%s", STATS["news_terms"])
                await asyncio.sleep(SCAN_INTERVAL_SECS)
                continue

            checked = 0
            value_found = 0
            arbs_found = 0

            # Scan all markets in memory
            for mid, m in markets:
                checked += 1

                yes_price = extract_yes_price(m)
                if yes_price is not None:
                    yes_price = clamp(yes_price, 0.0, 1.0)

                # --- Arb detection (basic) ---
                edge = compute_simple_arb_edge(m)
                if edge is not None and edge >= ARB_EDGE_MIN:
                    arbs_found += 1
                    value_found += 1
                    STATS["value_found"] += 1

                    msg = (
                        "💸 **ARB FOUND (high confidence)**\n"
                        f"• Edge: **{edge*100:.2f}%** (Yes+No < 1)\n"
                        f"• Market: **{safe_text(m.get('question') or m.get('title') or 'Market')}**\n"
                        f"• Link: {make_market_url(m)}\n"
                        f"• Note: This is a *pricing mismatch* alert, not a guarantee.\n"
                    )
                    await send_discord(msg)
                    STATS["alerts_sent"] += 1

                    # small pause to avoid spam bursts
                    await asyncio.sleep(0.6)
                    continue

                # --- Move + intelligence match (phase 3) ---
                # Only trigger if we have a price
                if yes_price is None:
                    continue

                prev = LAST_PRICE.get(mid)
                LAST_PRICE[mid] = yes_price
                LAST_SEEN_AT[mid] = now_ts()

                if prev is None:
                    continue

                move = abs(yes_price - prev)
                # percent relative to prev (avoid division by tiny)
                move_pct = move / max(prev, 0.03)

                if move_pct >= MOVE_PCT_MIN:
                    match_count, matched = intel_matches(m)
                    if match_count >= INTEL_MATCH_MIN:
                        value_found += 1
                        STATS["value_found"] += 1

                        msg = (
                            "🧠 **VALUE CANDIDATE (news/injury-backed)**\n"
                            f"• Move: **{move_pct*100:.1f}%** (from {fmt_price(prev)} → {fmt_price(yes_price)})\n"
                            f"• Intel matches: **{match_count}** ({', '.join(matched)})\n"
                            f"• Market: **{safe_text(m.get('question') or m.get('title') or 'Market')}**\n"
                            f"• Link: {make_market_url(m)}\n"
                            f"• Plan (safe default): take partial profit if price snaps back; keep remainder if momentum continues.\n"
                        )
                        await send_discord(msg)
                        STATS["alerts_sent"] += 1
                        await asyncio.sleep(0.6)

            STATS["markets_scanned"] += checked
            log.info(
                "[scan] checked=%s value_found=%s arbs_found=%s universe=%s news_terms=%s",
                checked, value_found, arbs_found, STATS["universe_markets"], STATS["news_terms"]
            )

        except Exception as e:
            STATS["last_error"] = f"scan: {type(e).__name__}: {e}"
            log.error("[scan] failed: %s", STATS["last_error"])

        await asyncio.sleep(SCAN_INTERVAL_SECS)

# =========================
# HEARTBEAT (daily)
# =========================
def next_heartbeat_run() -> datetime:
    n = local_now()
    target = n.replace(hour=HEARTBEAT_HOUR_LOCAL, minute=HEARTBEAT_MINUTE_LOCAL, second=0, microsecond=0)
    if target <= n:
        target = target + timedelta(days=1)
    return target

async def heartbeat_loop() -> None:
    await client.wait_until_ready()
    while True:
        try:
            nxt = next_heartbeat_run()
            sleep_s = (nxt - local_now()).total_seconds()
            await asyncio.sleep(max(5, sleep_s))

            msg = (
                "🧠 **PolyBrain Daily Health Check**\n\n"
                f"🕒 Period: last 24h\n"
                f"📊 Markets scanned: **{STATS['markets_scanned']}**\n"
                f"🔎 Value candidates found: **{STATS['value_found']}**\n"
                f"🚨 Alerts sent: **{STATS['alerts_sent']}**\n"
                f"⚠️ Rate limits hit: **{STATS['rate_limits']}**\n"
                f"🌐 Universe markets: **{STATS['universe_markets']}**\n"
                f"📰 News terms tracked: **{STATS['news_terms']}**\n"
                f"📦 HTTP 4xx: **{STATS['http_4xx']}** | HTTP 5xx: **{STATS['http_5xx']}**\n"
            )

            if STATS["last_error"]:
                msg += f"\n🔴 Last error: `{str(STATS['last_error'])[:180]}`"

            msg += "\n🟢 Status: **Running**"

            await send_discord(msg)

            # Reset rolling counters (keep universe/news in memory)
            for k in ["markets_scanned", "value_found", "alerts_sent", "rate_limits", "http_4xx", "http_5xx"]:
                STATS[k] = 0

        except Exception as e:
            STATS["last_error"] = f"heartbeat: {type(e).__name__}: {e}"
            log.error("[heartbeat] failed: %s", STATS["last_error"])
            await asyncio.sleep(60)

# =========================
# DISCORD EVENTS
# =========================
@client.event
async def on_ready():
    log.info("Logged in as: %s", client.user)
    await send_discord(
        "✅ PolyBrain is live. **Sports only** (NFL/NBA/NHL/CBB/CFB). "
        "**Phase 3 (news/injury intelligence)** is ON.\n"
        "I will alert for **high-confidence arbs** or **big moves with strong intel matches**."
    )

# =========================
# MAIN
# =========================
async def main():
    # run background tasks
    asyncio.create_task(refresh_universe_loop())
    asyncio.create_task(refresh_news_loop())
    asyncio.create_task(scan_loop())
    asyncio.create_task(heartbeat_loop())

    # start discord client
    await client.start(DISCORD_BOT_TOKEN)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
