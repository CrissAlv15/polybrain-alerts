import os
import re
import time
import json
import html
import asyncio
import logging
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
import discord
from xml.etree import ElementTree as ET

# =========================
# LOGGING
# =========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

# =========================
# CONFIG (env vars)
# =========================
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()
DISCORD_CHANNEL_ID = os.getenv("DISCORD_CHANNEL_ID", "").strip()
if not DISCORD_BOT_TOKEN or not DISCORD_CHANNEL_ID:
    raise SystemExit("Missing DISCORD_BOT_TOKEN or DISCORD_CHANNEL_ID")
CHANNEL_ID = int(DISCORD_CHANNEL_ID)

GAMMA = os.getenv("POLY_GAMMA_BASE", "https://gamma-api.polymarket.com").strip().rstrip("/")

# Sports only (NO UFC). These are the “tabs” you care about.
SPORTS_MODE = os.getenv("SPORTS_MODE", "nfl,nba,nhl,cbb,cfb").strip().lower()
SPORTS_ALLOW = {s.strip() for s in SPORTS_MODE.split(",") if s.strip()}

# Polling / thresholds (tuned for “higher quality”)
SCAN_EVERY_SEC = float(os.getenv("SCAN_EVERY_SEC", "15").strip() or "15")
UNIVERSE_REFRESH_SEC = float(os.getenv("UNIVERSE_REFRESH_SEC", "60").strip() or "60")
NEWS_REFRESH_SEC = float(os.getenv("NEWS_REFRESH_SEC", "90").strip() or "90")

# “Edge” thresholds (you can lower tomorrow)
ARB_SUM_MAX = float(os.getenv("ARB_SUM_MAX", "0.97").strip() or "0.97")  # yes+no <= 0.97 => arb-ish
MOVE_ALERT_CENTS = float(os.getenv("MOVE_ALERT_CENTS", "8").strip() or "8")  # price move in cents for news alert
NEWS_MATCH_MIN = int(os.getenv("NEWS_MATCH_MIN", "1").strip() or "1")

# Universe paging
MARKETS_PAGE_LIMIT = int(os.getenv("MARKETS_PAGE_LIMIT", "200").strip() or "200")
MARKETS_MAX_PAGES = int(os.getenv("MARKETS_MAX_PAGES", "8").strip() or "8")  # 8*200=1600 max

# News feeds (simple + reliable; you can add/remove anytime)
# NOTE: These are RSS feeds, no extra packages needed.
DEFAULT_FEEDS = [
    # General sports headlines
    "https://www.espn.com/espn/rss/news",
    "https://sports.yahoo.com/rss/",
    # NFL / NBA / NHL / CBB / CFB buckets
    "https://www.espn.com/espn/rss/nfl/news",
    "https://www.espn.com/espn/rss/nba/news",
    "https://www.espn.com/espn/rss/nhl/news",
    "https://www.espn.com/espn/rss/ncb/news",
    "https://www.espn.com/espn/rss/ncf/news",
]
NEWS_FEEDS = os.getenv("NEWS_FEEDS", "").strip()
FEEDS = [f.strip() for f in NEWS_FEEDS.split(",") if f.strip()] if NEWS_FEEDS else DEFAULT_FEEDS

# Rate-limit/backoff
HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "15").strip() or "15")
MAX_HTTP_RETRIES = int(os.getenv("MAX_HTTP_RETRIES", "6").strip() or "6")

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
    "last_error": None,
}

# Universe cache
UNIVERSE: List[Dict[str, Any]] = []
UNIVERSE_BY_ID: Dict[str, Dict[str, Any]] = {}

# Price memory (for movement detection)
LAST_PRICE: Dict[str, float] = {}          # market_id -> last mid (0..1)
LAST_PRICE_TS: Dict[str, float] = {}       # market_id -> time.time()

# News memory
NEWS_ITEMS_SEEN: set = set()               # item guid/link hash
NEWS_TERMS: Dict[str, int] = {}            # term -> count recent
NEWS_RECENT: List[Dict[str, str]] = []     # last N items
NEWS_RECENT_MAX = 80

# Alert de-dup (avoid spamming same market)
ALERT_COOLDOWN_SEC = 30 * 60
LAST_ALERT_TS: Dict[str, float] = {}       # key -> time.time()

# =========================
# DISCORD SETUP
# =========================
intents = discord.Intents.default()
client = discord.Client(intents=intents)

async def get_channel() -> discord.TextChannel:
    ch = client.get_channel(CHANNEL_ID)
    if ch is None:
        # fetch_channel requires bot to have access
        ch = await client.fetch_channel(CHANNEL_ID)
    return ch  # type: ignore

async def send_discord(msg: str) -> None:
    try:
        ch = await get_channel()
        await ch.send(msg)
        STATS["alerts_sent"] += 1
    except Exception as e:
        STATS["last_error"] = f"discord_send: {type(e).__name__}: {e}"
        logging.error("Discord send failed: %s", STATS["last_error"])

# =========================
# HTTP HELPERS
# =========================
async def http_get_text(hc: httpx.AsyncClient, url: str, params: Optional[dict] = None) -> str:
    last_err = None
    wait = 0.8
    for attempt in range(1, MAX_HTTP_RETRIES + 1):
        try:
            r = await hc.get(url, params=params, timeout=HTTP_TIMEOUT)
            status = r.status_code
            if status == 429:
                STATS["rate_limits"] += 1
                logging.warning("[http] status=429 attempt=%d/%d wait=%.1fs url=%s", attempt, MAX_HTTP_RETRIES, wait, url)
                await asyncio.sleep(wait)
                wait = min(wait * 1.9, 30.0)
                continue
            if 400 <= status < 500:
                STATS["http_4xx"] += 1
            if 500 <= status:
                STATS["http_5xx"] += 1
            r.raise_for_status()
            return r.text
        except Exception as e:
            last_err = e
            # backoff on transient errors
            logging.warning("[http] err attempt=%d/%d wait=%.1fs url=%s err=%s", attempt, MAX_HTTP_RETRIES, wait, url, e)
            await asyncio.sleep(wait)
            wait = min(wait * 1.9, 30.0)

    raise RuntimeError(str(last_err) if last_err else "http_get_text failed")

async def http_get_json(hc: httpx.AsyncClient, url: str, params: Optional[dict] = None) -> Any:
    txt = await http_get_text(hc, url, params=params)
    try:
        return json.loads(txt)
    except Exception:
        # If Polymarket returns non-json error body
        return {"raw": txt}

# =========================
# RSS PARSER
# =========================
def parse_rss(xml_text: str) -> List[Dict[str, str]]:
    items: List[Dict[str, str]] = []
    try:
        root = ET.fromstring(xml_text)
    except Exception:
        return items

    # RSS2: channel/item
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        guid = (item.findtext("guid") or link or title).strip()
        desc = (item.findtext("description") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
        if not title:
            continue
        items.append({
            "title": html.unescape(title),
            "link": link,
            "guid": guid,
            "desc": html.unescape(desc),
            "pub": pub,
        })
    return items

def extract_terms(text: str) -> List[str]:
    # Keep it simple: words, team names, short player names etc.
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[^A-Za-z0-9' \-]", " ", text)
    tokens = [t.strip(" -").lower() for t in text.split() if t.strip()]
    # Filter tiny junk tokens
    tokens = [t for t in tokens if len(t) >= 3 and t not in {"the","and","for","with","that","from","this","into","your"}]
    return tokens

# =========================
# POLYMARKET HELPERS
# =========================
def market_sport_tag(m: Dict[str, Any]) -> Optional[str]:
    """
    Best-effort: Polymarket markets often have tags / categories.
    We try multiple common fields. If none, return None.
    """
    for key in ("category", "sport", "league", "slug", "title"):
        val = m.get(key)
        if isinstance(val, str) and val:
            s = val.lower()
            for tag in SPORTS_ALLOW:
                if tag in s:
                    return tag
    # tags might be list of objects/strings
    tags = m.get("tags")
    if isinstance(tags, list):
        for t in tags:
            if isinstance(t, str):
                s = t.lower()
                for tag in SPORTS_ALLOW:
                    if tag in s:
                        return tag
            if isinstance(t, dict):
                name = str(t.get("name", "")).lower()
                slug = str(t.get("slug", "")).lower()
                for tag in SPORTS_ALLOW:
                    if tag in name or tag in slug:
                        return tag
    return None

def market_title(m: Dict[str, Any]) -> str:
    return str(m.get("question") or m.get("title") or "").strip()

def get_outcome_prices(m: Dict[str, Any]) -> Dict[str, float]:
    """
    Normalize yes/no style market prices.
    Polymarket API shapes can vary. We'll try common layouts.
    Returns dict like {"yes": 0.61, "no": 0.41} if possible.
    """
    out: Dict[str, float] = {}
    # Some markets expose "outcomes" list
    outcomes = m.get("outcomes")
    if isinstance(outcomes, list):
        for o in outcomes:
            if not isinstance(o, dict):
                continue
            name = str(o.get("name") or o.get("outcome") or "").lower().strip()
            p = o.get("price")
            if p is None:
                p = o.get("probability")
            try:
                fp = float(p)
            except Exception:
                continue
            if "yes" in name:
                out["yes"] = fp
            elif name == "no" or "no" in name:
                out["no"] = fp

    # Some markets have "bestBid"/"bestAsk"/"lastTradePrice"
    # We'll fall back to "lastTradePrice" for "yes" if it's a single-outcome style (rare).
    if "yes" not in out:
        for k in ("lastTradePrice", "last_trade_price", "price"):
            if k in m:
                try:
                    out["yes"] = float(m[k])
                    break
                except Exception:
                    pass

    return out

def mid_price_from_yesno(prices: Dict[str, float]) -> Optional[float]:
    y = prices.get("yes")
    n = prices.get("no")
    if y is None and n is None:
        return None
    if y is None and n is not None:
        return max(0.0, min(1.0, 1.0 - n))
    if y is not None:
        return max(0.0, min(1.0, y))
    return None

def should_alert_key(key: str) -> bool:
    now = time.time()
    last = LAST_ALERT_TS.get(key, 0.0)
    if now - last < ALERT_COOLDOWN_SEC:
        return False
    LAST_ALERT_TS[key] = now
    return True

# =========================
# UNIVERSE REFRESH
# =========================
async def fetch_markets_page(hc: httpx.AsyncClient, offset: int) -> List[Dict[str, Any]]:
    """
    /markets endpoint supports paging. IMPORTANT: do NOT pass unsupported params like 'order'/'ascending'.
    """
    url = f"{GAMMA}/markets"
    params = {
        "active": "true",
        "closed": "false",
        "limit": MARKETS_PAGE_LIMIT,
        "offset": offset,
    }
    data = await http_get_json(hc, url, params=params)

    # Polymarket may return list OR dict with "markets"
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and "markets" in data and isinstance(data["markets"], list):
        return data["markets"]
    return []

async def refresh_universe_loop() -> None:
    headers = {"User-Agent": "PolyBrain/1.0 (alerts bot)"}
    async with httpx.AsyncClient(headers=headers) as hc:
        while True:
            t0 = time.time()
            try:
                all_markets: List[Dict[str, Any]] = []
                for page in range(MARKETS_MAX_PAGES):
                    chunk = await fetch_markets_page(hc, offset=page * MARKETS_PAGE_LIMIT)
                    if not chunk:
                        break
                    all_markets.extend(chunk)
                    # small pacing helps avoid 429
                    await asyncio.sleep(0.15)

                # Filter: only sports tabs you care about
                filtered: List[Dict[str, Any]] = []
                for m in all_markets:
                    title = market_title(m)
                    if not title:
                        continue
                    tag = market_sport_tag(m)
                    if tag is None:
                        continue
                    filtered.append(m)

                # Update globals
                UNIVERSE.clear()
                UNIVERSE.extend(filtered)
                UNIVERSE_BY_ID.clear()
                for m in UNIVERSE:
                    mid = str(m.get("id") or m.get("marketId") or m.get("market_id") or "")
                    if mid:
                        UNIVERSE_BY_ID[mid] = m

                STATS["universe_markets"] = len(UNIVERSE)

                dt = time.time() - t0
                logging.info("[universe] markets=%d refresh_ok dt=%.1fs allow=%s", len(UNIVERSE), dt, ",".join(sorted(SPORTS_ALLOW)))

            except Exception as e:
                STATS["last_error"] = f"universe: {type(e).__name__}: {e}"
                logging.error("[universe] failed: %s", STATS["last_error"])

            await asyncio.sleep(UNIVERSE_REFRESH_SEC)

# =========================
# NEWS / "INJURY INTEL" REFRESH
# =========================
async def refresh_news_loop() -> None:
    headers = {"User-Agent": "PolyBrain/1.0 (news)"}
    async with httpx.AsyncClient(headers=headers, follow_redirects=True) as hc:
        while True:
            try:
                new_items = 0
                for feed in FEEDS:
                    xml_text = await http_get_text(hc, feed, params=None)
                    items = parse_rss(xml_text)
                    for it in items[:25]:
                        key = (it.get("guid") or it.get("link") or it.get("title") or "").strip()
                        if not key or key in NEWS_ITEMS_SEEN:
                            continue
                        NEWS_ITEMS_SEEN.add(key)
                        new_items += 1

                        combined = f"{it.get('title','')} {it.get('desc','')}"
                        terms = extract_terms(combined)
                        for t in terms:
                            NEWS_TERMS[t] = NEWS_TERMS.get(t, 0) + 1

                        NEWS_RECENT.insert(0, it)
                        if len(NEWS_RECENT) > NEWS_RECENT_MAX:
                            NEWS_RECENT.pop()

                STATS["news_terms"] = len(NEWS_TERMS)
                logging.info("[news] terms=%d feeds=%d new_items=%d", STATS["news_terms"], len(FEEDS), new_items)

            except Exception as e:
                STATS["last_error"] = f"news: {type(e).__name__}: {e}"
                logging.error("[news] failed: %s", STATS["last_error"])

            await asyncio.sleep(NEWS_REFRESH_SEC)

# =========================
# SCANNER
# =========================
def match_news_to_market(title: str) -> Tuple[int, List[str]]:
    """
    Returns (match_score, matched_terms)
    Very simple: if any news term appears in the market title, count it.
    """
    t = title.lower()
    matched = []
    score = 0
    # only check “stronger” terms (appeared at least twice recently)
    for term, cnt in list(NEWS_TERMS.items()):
        if cnt < 2:
            continue
        if term in t:
            score += 1
            matched.append(term)
            if score >= 6:
                break
    return score, matched

def build_sell_plan(entry_price: float) -> str:
    """
    Simple 50/50 plan. Not financial advice; it’s a rule-based template.
    """
    entry_c = int(round(entry_price * 100))
    tp1 = min(98, entry_c + 8)
    tp2 = min(99, entry_c + 15)
    sl = max(1, entry_c - 10)

    return (
        f"📌 **50/50 plan (template)**\n"
        f"• Sell **50%** at **{tp1}¢** (or first big spike)\n"
        f"• Sell remaining **50%** at **{tp2}¢** (or late move)\n"
        f"• If it dumps to **{sl}¢**, consider reducing risk\n"
    )

async def scan_loop() -> None:
    while True:
        try:
            checked = 0
            value_found = 0

            # Copy universe snapshot so refresh thread can update safely
            snapshot = list(UNIVERSE)

            for m in snapshot:
                title = market_title(m)
                if not title:
                    continue
                checked += 1

                mid = str(m.get("id") or m.get("marketId") or m.get("market_id") or "")
                if not mid:
                    continue

                prices = get_outcome_prices(m)
                mid_price = mid_price_from_yesno(prices)
                if mid_price is None:
                    continue

                # movement detection
                prev = LAST_PRICE.get(mid)
                prev_ts = LAST_PRICE_TS.get(mid, 0.0)
                LAST_PRICE[mid] = mid_price
                LAST_PRICE_TS[mid] = time.time()

                move_cents = 0.0
                if prev is not None and (time.time() - prev_ts) <= (10 * 60):
                    move_cents = abs(mid_price - prev) * 100.0

                # 1) Arb-ish detection (yes+no < threshold)
                y = prices.get("yes")
                n = prices.get("no")
                if y is not None and n is not None:
                    s = y + n
                    if s <= ARB_SUM_MAX:
                        key = f"arb:{mid}"
                        if should_alert_key(key):
                            value_found += 1
                            msg = (
                                f"🟣 **ARB Candidate**\n"
                                f"**Market:** {title}\n"
                                f"YES={int(round(y*100))}¢  |  NO={int(round(n*100))}¢  |  SUM={s:.3f}\n"
                                f"Edge (rough): **{(1.0 - s)*100:.1f}%**\n"
                                f"{build_sell_plan(y)}"
                                f"⚠️ This is a *signal*, not a guarantee. Verify liquidity/spread in-app.\n"
                            )
                            await send_discord(msg)

                # 2) News/injury “intel” alert (only if meaningful move + match)
                if move_cents >= MOVE_ALERT_CENTS:
                    score, matched_terms = match_news_to_market(title)
                    if score >= NEWS_MATCH_MIN:
                        key = f"news:{mid}"
                        if should_alert_key(key):
                            # pick the most recent news item containing a matched term
                            picked = None
                            for it in NEWS_RECENT[:40]:
                                txt = (it.get("title","") + " " + it.get("desc","")).lower()
                                if any(t in txt for t in matched_terms[:4]):
                                    picked = it
                                    break

                            headline = picked["title"] if picked else "Relevant news detected"
                            link = picked.get("link","") if picked else ""

                            value_found += 1
                            msg = (
                                f"🟦 **NEWS / INJURY INTEL**\n"
                                f"**Market:** {title}\n"
                                f"**Move:** ~{move_cents:.1f}¢ (last ~10m)\n"
                                f"**Matched terms:** `{', '.join(matched_terms[:6])}`\n"
                                f"**Headline:** {headline}\n"
                            )
                            if link:
                                msg += f"🔗 {link}\n"
                            msg += build_sell_plan(mid_price)
                            msg += "✅ Higher-confidence filter: requires both **price move** + **news match**.\n"
                            await send_discord(msg)

            STATS["markets_scanned"] += checked
            STATS["value_found"] += value_found
            logging.info(
                "[scan] checked=%d value_found=%d universe=%d news_terms=%d",
                checked, value_found, len(snapshot), len(NEWS_TERMS)
            )

        except Exception as e:
            STATS["last_error"] = f"scan: {type(e).__name__}: {e}"
            logging.error("[scan] failed: %s", STATS["last_error"])

        await asyncio.sleep(SCAN_EVERY_SEC)

# =========================
# HEARTBEAT / STATUS
# =========================
async def heartbeat_loop() -> None:
    # every 6 hours, send a small status message
    while True:
        await asyncio.sleep(6 * 3600)
        last_err = str(STATS["last_error"])[:180] if STATS["last_error"] else "none"
        msg = (
            "🫀 **PolyBrain Health Check**\n"
            f"• Universe markets: **{STATS['universe_markets']}**\n"
            f"• Markets scanned (lifetime): **{STATS['markets_scanned']}**\n"
            f"• Signals found (lifetime): **{STATS['value_found']}**\n"
            f"• Alerts sent (lifetime): **{STATS['alerts_sent']}**\n"
            f"• Rate limits hit: **{STATS['rate_limits']}**\n"
            f"• HTTP 4xx: **{STATS['http_4xx']}** | HTTP 5xx: **{STATS['http_5xx']}**\n"
            f"• Last error: `{last_err}`\n"
        )
        await send_discord(msg)

# =========================
# DISCORD EVENTS
# =========================
@client.event
async def on_ready():
    logging.info("Logged in as: %s", client.user)
    await send_discord("✅ PolyBrain is live. **Phase 2 (News/Injury Intel)** scanning is ON.")
    # start loops
    asyncio.create_task(refresh_universe_loop())
    asyncio.create_task(refresh_news_loop())
    asyncio.create_task(scan_loop())
    asyncio.create_task(heartbeat_loop())

# =========================
# RUN
# =========================
client.run(DISCORD_BOT_TOKEN)
