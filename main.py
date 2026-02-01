import os
import re
import time
import json
import asyncio
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import httpx
import discord

# ----------------------------
# ENV
# ----------------------------
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()
DISCORD_CHANNEL_ID = os.getenv("DISCORD_CHANNEL_ID", "").strip()

if not DISCORD_BOT_TOKEN or not DISCORD_CHANNEL_ID:
    raise SystemExit("Missing DISCORD_BOT_TOKEN or DISCORD_CHANNEL_ID")

CHANNEL_ID = int(DISCORD_CHANNEL_ID)

# Polymarket Gamma API (markets)
GAMMA = os.getenv("POLY_GAMMA_BASE", "https://gamma-api.polymarket.com").strip()

# Scanning cadence
SCAN_SECONDS = float(os.getenv("SCAN_SECONDS", "20").strip() or "20")

# News ingestion (Option A)
NEWS_ENABLED = (os.getenv("NEWS_ENABLED", "1").strip() != "0")
NEWS_POLL_SECONDS = int(os.getenv("NEWS_POLL_SECONDS", "180").strip() or "180")
NEWS_LEAGUES = os.getenv(
    "NEWS_LEAGUES",
    "basketball/nba,football/nfl,baseball/mlb,hockey/nhl"
).strip()
NEWS_MATCH_MIN_WORDLEN = int(os.getenv("NEWS_MATCH_MIN_WORDLEN", "4").strip() or "4")
NEWS_COOLDOWN_SECONDS = int(os.getenv("NEWS_COOLDOWN_SECONDS", "1800").strip() or "1800")  # 30 min

# Confidence filter (your existing concept)
# 1 = higher confidence / fewer alerts
CONFIDENCE_LEVEL = int(os.getenv("CONFIDENCE_LEVEL", "1").strip() or "1")

# ----------------------------
# Discord
# ----------------------------
intents = discord.Intents.default()
client = discord.Client(intents=intents)

# ----------------------------
# Helpers
# ----------------------------
def now_ts() -> float:
    return time.time()

def norm_text(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9\s\-\.\'/]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def tokenize(s: str, min_len: int = 4) -> List[str]:
    s = norm_text(s)
    toks = [t for t in re.split(r"[\s\-\/]+", s) if len(t) >= min_len]
    # drop very common words
    stop = {
        "with","from","that","this","will","have","over","under","game","match","team",
        "odds","line","vs","live","news","report","reports","after","before","their",
        "they","them","into","your","about","been","were","when","what","where","which",
        "injury","injuries","questionable","probable"
    }
    return [t for t in toks if t not in stop]

def chunked(lst: List[Any], n: int) -> List[List[Any]]:
    return [lst[i:i+n] for i in range(0, len(lst), n)]

# ----------------------------
# Data models
# ----------------------------
@dataclass
class Market:
    id: str
    question: str
    url: str
    active: bool
    closed: bool
    best_yes: Optional[float] = None
    best_no: Optional[float] = None
    tags: List[str] = None

# ----------------------------
# HTTP with retries / backoff
# ----------------------------
async def http_get_json(client_http: httpx.AsyncClient, url: str, params: Optional[Dict[str, Any]] = None) -> Any:
    # Handles transient failures and rate limits.
    backoffs = [0.8, 1.5, 3.0, 5.6, 11.4, 20.0]
    last_err: Optional[str] = None

    for attempt, wait_s in enumerate(backoffs, start=1):
        try:
            r = await client_http.get(url, params=params, timeout=25)
            # Rate limit / temporary blocks
            if r.status_code in (429, 503, 502):
                print(f"[http] status={r.status_code} attempt={attempt}/{len(backoffs)} wait={wait_s}s url={url}")
                await asyncio.sleep(wait_s)
                continue

            # “Unprocessable entity” usually means bad params — log and bail (don’t hammer)
            if r.status_code == 422:
                print(f"[http] status=422 attempt={attempt}/{len(backoffs)} url={r.url}")
                # small backoff then try again (sometimes Gamma is picky / inconsistent)
                await asyncio.sleep(wait_s)
                last_err = f"422 Unprocessable Entity for {r.url}"
                continue

            r.raise_for_status()
            return r.json()

        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            print(f"[http] error attempt={attempt}/{len(backoffs)} wait={wait_s}s url={url} err={last_err}")
            await asyncio.sleep(wait_s)

    raise RuntimeError(last_err or "HTTP failed")

# ----------------------------
# Polymarket: universe + scan
# ----------------------------
async def fetch_markets_page(client_http: httpx.AsyncClient, offset: int, limit: int = 200) -> List[Dict[str, Any]]:
    url = f"{GAMMA}/markets"
    params = {
        "active": "true",
        "closed": "false",
        "limit": str(limit),
        "offset": str(offset),
    }
    data = await http_get_json(client_http, url, params=params)
    if isinstance(data, list):
        return data
    return []

def market_url(m: Dict[str, Any]) -> str:
    # Polymarket web links are not always included; fall back to a generic market path.
    # If 'slug' exists, it’s nicer.
    slug = m.get("slug")
    mid = m.get("id") or ""
    if slug:
        return f"https://polymarket.com/market/{slug}"
    if mid:
        return f"https://polymarket.com/market/{mid}"
    return "https://polymarket.com/"

def extract_prices(m: Dict[str, Any]) -> Tuple[Optional[float], Optional[float]]:
    # Gamma fields vary; try a few common patterns.
    # If your repo already has a better price extractor, keep that logic there.
    yes = None
    no = None

    # Common: "outcomes" with "price"
    outs = m.get("outcomes")
    if isinstance(outs, list) and outs:
        try:
            # heuristic: first is YES, second is NO
            if len(outs) >= 1 and isinstance(outs[0], dict):
                yes = outs[0].get("price")
            if len(outs) >= 2 and isinstance(outs[1], dict):
                no = outs[1].get("price")
        except Exception:
            pass

    # Other: "bestBid"/"bestAsk" style is possible; ignore for now

    def to_float(x):
        try:
            if x is None:
                return None
            return float(x)
        except Exception:
            return None

    return to_float(yes), to_float(no)

async def refresh_universe(client_http: httpx.AsyncClient, max_markets: int = 1600) -> List[Market]:
    markets: List[Market] = []
    limit = 200
    for offset in range(0, max_markets, limit):
        page = await fetch_markets_page(client_http, offset=offset, limit=limit)
        if not page:
            break
        for raw in page:
            q = raw.get("question") or raw.get("title") or ""
            mid = str(raw.get("id") or "")
            if not mid or not q:
                continue
            yes, no = extract_prices(raw)
            markets.append(
                Market(
                    id=mid,
                    question=q,
                    url=market_url(raw),
                    active=bool(raw.get("active", True)),
                    closed=bool(raw.get("closed", False)),
                    best_yes=yes,
                    best_no=no,
                    tags=[],
                )
            )
    return markets

def find_simple_value_signals(m: Market) -> Optional[str]:
    """
    Phase 1/2 “intelligence” placeholder:
    - This does NOT guarantee profit.
    - It only flags obvious pricing weirdness / rounding / stale-ish patterns.
    You can swap this later with better models.
    """
    if m.best_yes is None:
        return None

    y = m.best_yes
    # ultra-basic filters to keep alerts high quality
    if CONFIDENCE_LEVEL == 1:
        # only extreme prices (rare) to reduce spam
        if y <= 0.03:
            return f"YES looks extremely cheap (≈{y:.3f})."
        if y >= 0.97:
            return f"YES looks extremely expensive (≈{y:.3f})."
    else:
        if y <= 0.08:
            return f"YES looks cheap (≈{y:.3f})."
        if y >= 0.92:
            return f"YES looks expensive (≈{y:.3f})."

    return None

# ----------------------------
# News ingestion (Option A)
# ----------------------------
async def fetch_espn_news(client_http: httpx.AsyncClient, league_path: str) -> List[Dict[str, Any]]:
    # Example league_path: "basketball/nba"
    url = f"http://site.api.espn.com/apis/site/v2/sports/{league_path}/news"
    data = await http_get_json(client_http, url, params={"limit": "50"})
    arts = data.get("articles") if isinstance(data, dict) else None
    if isinstance(arts, list):
        return arts
    return []

def build_market_keyword_index(markets: List[Market]) -> Dict[str, List[Market]]:
    """
    Create a keyword -> markets map for headline matching.
    We only keep tokens with min length to avoid junk matches.
    """
    idx: Dict[str, List[Market]] = {}
    for m in markets:
        toks = tokenize(m.question, min_len=NEWS_MATCH_MIN_WORDLEN)
        # keep it light: top N tokens per market
        toks = toks[:12]
        for t in toks:
            idx.setdefault(t, []).append(m)
    return idx

def score_headline_against_markets(headline: str, idx: Dict[str, List[Market]]) -> Tuple[int, List[Market], List[str]]:
    toks = set(tokenize(headline, min_len=NEWS_MATCH_MIN_WORDLEN))
    matched_markets: List[Market] = []
    matched_terms: List[str] = []
    for t in toks:
        if t in idx:
            matched_terms.append(t)
            matched_markets.extend(idx[t])

    # de-dup markets
    seen = set()
    uniq: List[Market] = []
    for m in matched_markets:
        if m.id not in seen:
            seen.add(m.id)
            uniq.append(m)

    return len(matched_terms), uniq[:6], matched_terms[:8]

# ----------------------------
# Discord send
# ----------------------------
async def send_discord(channel: discord.TextChannel, text: str) -> None:
    # split into chunks if needed
    for chunk in chunked(text.split("\n"), 20):
        await channel.send("\n".join(chunk))

# ----------------------------
# Loops
# ----------------------------
async def scan_loop(channel: discord.TextChannel) -> None:
    async with httpx.AsyncClient(headers={"User-Agent": "polybrain/1.0"}) as http:
        universe: List[Market] = []
        last_universe_refresh = 0.0

        alerted: Dict[str, float] = {}  # market_id -> last_alert_ts
        universe_refresh_seconds = 90  # refresh markets list often, but not insane

        while True:
            # refresh universe
            if now_ts() - last_universe_refresh > universe_refresh_seconds or not universe:
                t0 = now_ts()
                try:
                    universe = await refresh_universe(http, max_markets=1600)
                    dt = now_ts() - t0
                    print(f"[universe] markets={len(universe)} refresh={dt:.1f}s")
                    last_universe_refresh = now_ts()
                except Exception as e:
                    print(f"[universe] failed: {e}")
                    await asyncio.sleep(10)

            checked = 0
            value_found = 0
            alerts_sent = 0

            for m in universe:
                checked += 1
                msg = find_simple_value_signals(m)
                if not msg:
                    continue
                value_found += 1

                # cooldown per market
                last = alerted.get(m.id, 0.0)
                if now_ts() - last < 3600:  # 1 hour per market
                    continue

                alerted[m.id] = now_ts()
                alerts_sent += 1

                text = (
                    f"📈 **VALUE WATCH**\n"
                    f"**Market:** {m.question}\n"
                    f"**YES:** {m.best_yes}\n"
                    f"**Note:** {msg}\n"
                    f"{m.url}\n"
                    f"—\n"
                    f"Risk note: this is a heuristic flag, not guaranteed edge."
                )
                await send_discord(channel, text)

            print(f"[scan] checked={checked} value_found={value_found} alerts_sent={alerts_sent}")
            await asyncio.sleep(SCAN_SECONDS)

async def news_loop(channel: discord.TextChannel) -> None:
    if not NEWS_ENABLED:
        print("[news] disabled")
        return

    leagues = [x.strip() for x in NEWS_LEAGUES.split(",") if x.strip()]
    if not leagues:
        print("[news] no leagues configured")
        return

    async with httpx.AsyncClient(headers={"User-Agent": "polybrain/1.0"}) as http:
        universe: List[Market] = []
        idx: Dict[str, List[Market]] = {}
        last_universe_refresh = 0.0

        seen_articles: Dict[str, float] = {}  # url/id -> ts

        while True:
            # Refresh market index occasionally for matching
            if now_ts() - last_universe_refresh > 180 or not idx:
                try:
                    universe = await refresh_universe(http, max_markets=1600)
                    idx = build_market_keyword_index(universe)
                    last_universe_refresh = now_ts()
                    print(f"[news] index_ready markets={len(universe)} keywords={len(idx)}")
                except Exception as e:
                    print(f"[news] universe refresh failed: {e}")

            for league in leagues:
                try:
                    articles = await fetch_espn_news(http, league)
                except Exception as e:
                    print(f"[news] fetch failed league={league} err={e}")
                    continue

                for a in articles[:30]:
                    headline = (a.get("headline") or a.get("title") or "").strip()
                    links = a.get("links") or {}
                    web = (links.get("web") or {}) if isinstance(links, dict) else {}
                    url = (web.get("href") or a.get("link") or "").strip()

                    if not headline or not url:
                        continue

                    key = url
                    if key in seen_articles and (now_ts() - seen_articles[key] < NEWS_COOLDOWN_SECONDS):
                        continue

                    score, markets_hit, terms = score_headline_against_markets(headline, idx)
                    if score <= 0:
                        continue

                    # High quality: require at least 2 matching terms if CONFIDENCE_LEVEL=1
                    if CONFIDENCE_LEVEL == 1 and score < 2:
                        continue

                    seen_articles[key] = now_ts()

                    mk_lines = ""
                    if markets_hit:
                        mk_lines = "\n".join([f"- {m.question}" for m in markets_hit[:5]])

                    text = (
                        f"🗞️ **NEWS MATCH ({league})**\n"
                        f"**Headline:** {headline}\n"
                        f"**Matched terms:** {', '.join(terms)}\n"
                        f"**Related markets:**\n{mk_lines if mk_lines else '- (no top markets listed)'}\n"
                        f"{url}\n"
                        f"—\n"
                        f"Tip: news can move prices fast; verify before acting."
                    )
                    await send_discord(channel, text)

            await asyncio.sleep(NEWS_POLL_SECONDS)

@client.event
async def on_ready():
    print(f"Logged in as: {client.user}")
    channel = client.get_channel(CHANNEL_ID)
    if channel is None:
        # fallback: fetch channel
        try:
            channel = await client.fetch_channel(CHANNEL_ID)
        except Exception as e:
            raise SystemExit(f"Could not access channel {CHANNEL_ID}: {e}")

    await send_discord(channel, "✅ PolyBrain is live. Phase 2 (Option A) scanning is ON.")

    # Run both loops
    tasks = [asyncio.create_task(scan_loop(channel))]
    if NEWS_ENABLED:
        tasks.append(asyncio.create_task(news_loop(channel)))

    await asyncio.gather(*tasks)

client.run(DISCORD_BOT_TOKEN)
