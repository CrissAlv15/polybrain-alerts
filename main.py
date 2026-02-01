import os
import re
import time
import asyncio
import logging
from typing import Any, Dict, List, Optional, Tuple

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

# Sports: only what you see in-app (no UFC)
SPORTS_MODE = os.getenv("SPORTS_MODE", "nfl,nba,nhl,cbb,cfb").strip().lower()

# High-quality defaults (you can override in Railway Variables)
MIN_EDGE_BASE = float(os.getenv("MIN_EDGE_BASE", "0.07"))           # 7% edge baseline
MAX_SPREAD = float(os.getenv("MAX_SPREAD", "0.05"))                 # ignore wide spreads
ALERT_COOLDOWN_SEC = int(os.getenv("ALERT_COOLDOWN_SEC", "1800"))   # 30 min per market
SCAN_INTERVAL_SEC = float(os.getenv("SCAN_INTERVAL_SEC", "3.0"))    # scan cadence
UNIVERSE_REFRESH_SEC = float(os.getenv("UNIVERSE_REFRESH_SEC", "25"))
MAX_MARKETS = int(os.getenv("MAX_MARKETS", "1600"))

# Phase 3: silent news ingestion
NEWS_ENABLED = os.getenv("NEWS_ENABLED", "1").strip() != "0"
NEWS_POLL_SEC = int(os.getenv("NEWS_POLL_SEC", "180"))              # poll every 3 min
NEWS_LOOKBACK_MIN = int(os.getenv("NEWS_LOOKBACK_MIN", "240"))      # consider last 4h of news "fresh"
NEWS_MATCH_MIN_TERMS = int(os.getenv("NEWS_MATCH_MIN_TERMS", "2"))  # require >=2 term matches
NEWS_BOOST_EDGE_REDUCTION = float(os.getenv("NEWS_BOOST_EDGE_REDUCTION", "0.02"))  # reduces edge req by 2% if news-lag
NEWS_LAG_MINUTES = int(os.getenv("NEWS_LAG_MINUTES", "6"))          # require market not moved for at least X minutes after news

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)

# =========================
# DISCORD
# =========================
intents = discord.Intents.default()
bot = discord.Client(intents=intents)
CHANNEL: Optional[discord.TextChannel] = None

# =========================
# STATE
# =========================
UNIVERSE: Dict[str, Dict[str, Any]] = {}         # market_id -> market object
LAST_UNIVERSE_OK: float = 0.0

LAST_ALERT: Dict[str, float] = {}               # market_id -> ts
LAST_PRICE_SEEN: Dict[str, Tuple[float, float]] = {}  # market_id -> (ts, yes_mid) used for "lag"

# news_terms: token -> last_seen_ts (only for recent news)
NEWS_TERMS: Dict[str, float] = {}
LAST_NEWS_OK: float = 0.0

# =========================
# UTIL
# =========================
def now() -> float:
    return time.time()

def clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))

def to_float(x: Any) -> Optional[float]:
    try:
        if x is None:
            return None
        return float(x)
    except Exception:
        return None

def pct(x: float) -> str:
    return f"{x*100:.1f}%"

def should_alert(market_id: str) -> bool:
    return (now() - LAST_ALERT.get(market_id, 0.0)) >= ALERT_COOLDOWN_SEC

def mark_alert(market_id: str) -> None:
    LAST_ALERT[market_id] = now()

def norm_text(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9\s\-]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

STOPWORDS = {
    "with","from","that","this","will","have","over","under","game","match","team","vs","live","today",
    "report","reports","after","before","their","they","them","into","about","been","were","when","what",
    "where","which","injury","injuries","questionable","probable","out","doubtful","ruled","status",
    "update","updates","final","score","wins","win","loss","loses","season"
}

def tokens(s: str, min_len: int = 4) -> List[str]:
    t = [x for x in re.split(r"[\s\-]+", norm_text(s)) if len(x) >= min_len]
    return [x for x in t if x not in STOPWORDS]

def market_title(m: Dict[str, Any]) -> str:
    return str(m.get("question") or m.get("title") or m.get("name") or m.get("slug") or f"Market {m.get('id')}")

def market_link(m: Dict[str, Any]) -> str:
    slug = m.get("slug")
    mid = m.get("id")
    if slug:
        return f"https://polymarket.com/market/{slug}"
    if mid:
        return f"https://polymarket.com/market/{mid}"
    return "https://polymarket.com"

def classify_sport(m: Dict[str, Any]) -> Optional[str]:
    """
    Gamma doesn't always give clean 'league' fields, so we classify by text.
    This keeps it aligned with what you see on the Polymarket app.
    """
    q = norm_text(market_title(m))

    # quick filters for common league words
    if "nfl" in q or "super bowl" in q or "afc" in q or "nfc" in q:
        return "nfl"
    if "nba" in q or "playoffs" in q or "western conference" in q or "eastern conference" in q:
        return "nba"
    if "nhl" in q or "stanley cup" in q:
        return "nhl"
    if "college basketball" in q or "ncaa" in q or "cbb" in q or "march madness" in q:
        return "cbb"
    if "college football" in q or "cfb" in q or "bowl game" in q or "ncaa football" in q:
        return "cfb"

    # fallback: if question contains typical team formats, we keep it but mark unknown
    return None

ALLOWED_SPORTS = {x.strip() for x in SPORTS_MODE.split(",") if x.strip()}

# =========================
# PRICES + FAIR VALUE (vig-adjusted)
# =========================
def extract_books(m: Dict[str, Any]) -> Optional[Dict[str, float]]:
    """
    Best-effort YES/NO bid/ask extraction:
    - yesBuyPrice / yesSellPrice / noBuyPrice / noSellPrice if present
    - otherwise tries outcomePrices as mid (less ideal)
    """
    yes_ask = to_float(m.get("yesBuyPrice"))
    yes_bid = to_float(m.get("yesSellPrice"))
    no_ask  = to_float(m.get("noBuyPrice"))
    no_bid  = to_float(m.get("noSellPrice"))

    if all(v is not None for v in (yes_ask, yes_bid, no_ask, no_bid)):
        return {
            "yes_ask": clamp01(yes_ask),
            "yes_bid": clamp01(yes_bid),
            "no_ask": clamp01(no_ask),
            "no_bid": clamp01(no_bid),
            "quality": 2.0,  # best
        }

    # fallback: outcomePrices gives us mids only; simulate tight book
    outs = m.get("outcomePrices")
    if isinstance(outs, list) and len(outs) == 2:
        y = to_float(outs[0])
        n = to_float(outs[1])
        if y is None or n is None:
            return None
        y = clamp01(y); n = clamp01(n)
        # create a "pseudo book" with tiny spread so math still works
        return {
            "yes_bid": clamp01(y - 0.005),
            "yes_ask": clamp01(y + 0.005),
            "no_bid":  clamp01(n - 0.005),
            "no_ask":  clamp01(n + 0.005),
            "quality": 1.0,  # weaker
        }

    return None

def fair_yes_from_books(b: Dict[str, float]) -> Optional[float]:
    yes_mid = (b["yes_bid"] + b["yes_ask"]) / 2.0
    no_mid  = (b["no_bid"]  + b["no_ask"])  / 2.0
    denom = yes_mid + no_mid
    if denom <= 0:
        return None
    return clamp01(yes_mid / denom)

def compute_value(b: Dict[str, float]) -> Optional[Dict[str, Any]]:
    fair_yes = fair_yes_from_books(b)
    if fair_yes is None:
        return None
    fair_no = 1.0 - fair_yes

    yes_spread = b["yes_ask"] - b["yes_bid"]
    no_spread  = b["no_ask"]  - b["no_bid"]

    # require at least one side liquid
    if yes_spread > MAX_SPREAD and no_spread > MAX_SPREAD:
        return None

    edge_yes = fair_yes - b["yes_ask"]   # how underpriced YES is to buy
    edge_no  = fair_no  - b["no_ask"]    # how underpriced NO is to buy

    if edge_yes >= edge_no and edge_yes > 0 and yes_spread <= MAX_SPREAD:
        entry = b["yes_ask"]
        tp1 = max(entry, fair_yes)
        tp2 = max(entry, clamp01(fair_yes + edge_yes))
        return {"side": "YES", "entry": entry, "fair": fair_yes, "edge": edge_yes, "spread": yes_spread, "tp1": tp1, "tp2": tp2, "book_quality": b.get("quality", 1.0)}
    if edge_no > 0 and no_spread <= MAX_SPREAD:
        entry = b["no_ask"]
        tp1 = max(entry, fair_no)
        tp2 = max(entry, clamp01(fair_no + edge_no))
        return {"side": "NO", "entry": entry, "fair": fair_no, "edge": edge_no, "spread": no_spread, "tp1": tp1, "tp2": tp2, "book_quality": b.get("quality", 1.0)}
    return None

# =========================
# NEWS (silent)
# ESPN public endpoints, free, good enough for Option A
# =========================
LEAGUE_TO_ESPN_PATH = {
    "nba": "basketball/nba",
    "nfl": "football/nfl",
    "nhl": "hockey/nhl",
    "cbb": "basketball/mens-college-basketball",
    "cfb": "football/college-football",
}

INJURY_KEYWORDS = {"injury","injured","out","questionable","doubtful","probable","ruled","inactive","scratch","day-to-day","gtd"}

async def fetch_espn_news(http: httpx.AsyncClient, espn_path: str) -> List[Dict[str, Any]]:
    url = f"http://site.api.espn.com/apis/site/v2/sports/{espn_path}/news"
    params = {"limit": "50"}
    data = await http_get(http, url, params)
    arts = data.get("articles") if isinstance(data, dict) else None
    return arts if isinstance(arts, list) else []

async def http_get(http: httpx.AsyncClient, url: str, params: Dict[str, Any]) -> Any:
    # retries/backoff with special care for 429/5xx
    waits = [0.8, 1.5, 3.0, 6.0, 12.0]
    last = None
    for i, w in enumerate(waits, start=1):
        try:
            r = await http.get(url, params=params, timeout=25)
            if r.status_code in (429, 502, 503):
                logging.warning(f"[http] status={r.status_code} attempt={i}/{len(waits)} wait={w}s url={url}")
                await asyncio.sleep(w)
                continue
            if r.status_code == 422:
                # news endpoint rarely does this; just wait and retry once or twice
                logging.warning(f"[http] status=422 attempt={i}/{len(waits)} wait={w}s url={url}")
                await asyncio.sleep(w)
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = e
            logging.warning(f"[http] error={type(e).__name__} attempt={i}/{len(waits)} wait={w}s url={url}")
            await asyncio.sleep(w)
    raise RuntimeError(last)

async def news_loop() -> None:
    global NEWS_TERMS, LAST_NEWS_OK
    if not NEWS_ENABLED:
        logging.info("[news] disabled")
        return

    async with httpx.AsyncClient(headers={"User-Agent": "polybrain/1.0"}) as http:
        while True:
            try:
                cutoff = now() - (NEWS_LOOKBACK_MIN * 60)
                # clean stale terms
                NEWS_TERMS = {k: ts for k, ts in NEWS_TERMS.items() if ts >= cutoff}

                for sport in list(ALLOWED_SPORTS):
                    espn_path = LEAGUE_TO_ESPN_PATH.get(sport)
                    if not espn_path:
                        continue
                    arts = await fetch_espn_news(http, espn_path)

                    for a in arts[:40]:
                        headline = (a.get("headline") or a.get("title") or "").strip()
                        if not headline:
                            continue
                        h = norm_text(headline)
                        # prefer injury-like headlines for this module
                        if not any(k in h for k in INJURY_KEYWORDS):
                            continue

                        # update terms
                        for t in tokens(headline, min_len=4)[:12]:
                            NEWS_TERMS[t] = now()

                LAST_NEWS_OK = now()
                logging.info(f"[news] terms={len(NEWS_TERMS)} fresh={NEWS_LOOKBACK_MIN}min")

            except Exception as e:
                logging.warning(f"[news] loop error: {type(e).__name__}: {e}")

            await asyncio.sleep(NEWS_POLL_SEC)

def news_matches_market(m: Dict[str, Any]) -> Tuple[bool, int]:
    """
    Silent intelligence: do NOT alert on news.
    Just return whether this market is currently 'news-relevant' and how strong.
    """
    if not NEWS_TERMS:
        return (False, 0)

    q = market_title(m)
    q_tokens = set(tokens(q, min_len=4))
    if not q_tokens:
        return (False, 0)

    # score = number of overlapping fresh terms
    score = sum(1 for t in q_tokens if t in NEWS_TERMS)
    return (score >= NEWS_MATCH_MIN_TERMS, score)

# =========================
# POLYMARKET UNIVERSE
# =========================
async def fetch_markets_page(http: httpx.AsyncClient, offset: int, limit: int = 200) -> List[Dict[str, Any]]:
    # IMPORTANT: simple params to avoid 422
    url = f"{GAMMA}/markets"
    params = {
        "active": "true",
        "closed": "false",
        "limit": str(limit),
        "offset": str(offset),
    }
    waits = [0.8, 1.5, 3.0, 6.0, 12.0]
    for i, w in enumerate(waits, start=1):
        try:
            r = await http.get(url, params=params, timeout=25)
            if r.status_code in (429, 502, 503):
                logging.warning(f"[poly] status={r.status_code} attempt={i}/{len(waits)} wait={w}s")
                await asyncio.sleep(w)
                continue
            if r.status_code == 422:
                logging.warning(f"[poly] status=422 attempt={i}/{len(waits)} (params rejected) wait={w}s")
                await asyncio.sleep(w)
                continue
            r.raise_for_status()
            data = r.json()
            return data if isinstance(data, list) else []
        except Exception:
            await asyncio.sleep(w)
    return []

async def refresh_universe_loop() -> None:
    global UNIVERSE, LAST_UNIVERSE_OK
    async with httpx.AsyncClient(headers={"User-Agent": "polybrain/1.0"}) as http:
        while True:
            try:
                all_markets: List[Dict[str, Any]] = []
                limit = 200
                for offset in range(0, MAX_MARKETS, limit):
                    page = await fetch_markets_page(http, offset=offset, limit=limit)
                    if not page:
                        break
                    all_markets.extend(page)
                    if len(page) < limit:
                        break
                    await asyncio.sleep(0.2)

                # filter to your sports
                new_uni: Dict[str, Dict[str, Any]] = {}
                for m in all_markets:
                    mid = m.get("id")
                    if mid is None:
                        continue
                    sport = classify_sport(m)
                    if sport is None:
                        continue
                    if sport not in ALLOWED_SPORTS:
                        continue
                    new_uni[str(mid)] = m

                if new_uni:
                    UNIVERSE = new_uni
                    LAST_UNIVERSE_OK = now()
                    logging.info(f"[universe] markets={len(UNIVERSE)} refresh_ok sports={','.join(sorted(ALLOWED_SPORTS))}")
                else:
                    # keep old cache if fetch is empty
                    logging.warning(f"[universe] got 0 after filter (keeping cache={len(UNIVERSE)})")

            except Exception as e:
                age = (now() - LAST_UNIVERSE_OK) if LAST_UNIVERSE_OK else -1
                logging.error(f"[universe] failed: {type(e).__name__} cache_age={age:.0f}s")

            await asyncio.sleep(UNIVERSE_REFRESH_SEC)

# =========================
# ALERT FORMAT
# =========================
async def send_discord(msg: str) -> None:
    global CHANNEL
    if CHANNEL is None:
        CHANNEL = bot.get_channel(CHANNEL_ID)
        if CHANNEL is None:
            CHANNEL = await bot.fetch_channel(CHANNEL_ID)
    await CHANNEL.send(msg)

def build_alert(m: Dict[str, Any], sig: Dict[str, Any], *, news_used: bool, news_score: int) -> str:
    title = market_title(m)
    link = market_link(m)

    # Confidence scoring (simple but effective):
    # - start from edge size
    # - boost if book quality is better
    # - boost if "news lag" is in play
    conf = 5.0
    conf += min(4.0, sig["edge"] * 50.0)        # 0.07 edge -> +3.5
    conf += 1.0 if sig.get("book_quality", 1.0) >= 2.0 else 0.0
    conf += 1.2 if news_used else 0.0
    conf = min(10.0, conf)

    why_bits = []
    why_bits.append(f"vig-adjusted fair value")
    if news_used:
        why_bits.append("news/injury lag detected")
    why = " + ".join(why_bits)

    return (
        f"📊 **HIGH-QUALITY VALUE — {sig['side']}**\n"
        f"**{title}**\n"
        f"{link}\n\n"
        f"• Entry (ask): **{pct(sig['entry'])}**\n"
        f"• Fair (vig-adjusted): **{pct(sig['fair'])}**\n"
        f"• Edge: **+{pct(sig['edge'])}**\n"
        f"• Spread: **{pct(sig['spread'])}**\n"
        f"• Confidence: **{conf:.1f}/10**\n"
        f"• Why: {why}\n\n"
        f"🎯 **Plan**\n"
        f"• Sell **50%** at **{pct(sig['tp1'])}**\n"
        f"• Sell **50%** at **{pct(sig['tp2'])}**\n"
        f"_Not financial advice._"
    )

# =========================
# SCAN LOOP (Phase 3 logic)
# =========================
async def scan_loop() -> None:
    while True:
        try:
            checked = 0
            value_found = 0
            alerted = 0

            for mid, m in list(UNIVERSE.items()):
                checked += 1

                b = extract_books(m)
                if b is None:
                    continue

                sig = compute_value(b)
                if sig is None:
                    continue

                value_found += 1

                # Phase 3: use silent news to require "lag" and allow slightly lower edge threshold.
                news_used = False
                news_score = 0
                required_edge = MIN_EDGE_BASE

                if NEWS_ENABLED:
                    nm, score = news_matches_market(m)
                    if nm:
                        news_used = True
                        news_score = score
                        # allow slightly lower edge requirement if news appears relevant
                        required_edge = max(0.03, MIN_EDGE_BASE - NEWS_BOOST_EDGE_REDUCTION)

                        # require "lag": price not recently updating (we approximate with YES mid stability)
                        yes_mid = (b["yes_bid"] + b["yes_ask"]) / 2.0
                        last = LAST_PRICE_SEEN.get(mid)
                        if last is None:
                            LAST_PRICE_SEEN[mid] = (now(), yes_mid)
                        else:
                            last_ts, last_mid = last
                            # if price moved recently, reset timer
                            if abs(yes_mid - last_mid) >= 0.01:
                                LAST_PRICE_SEEN[mid] = (now(), yes_mid)

                        # require at least NEWS_LAG_MINUTES since last meaningful move
                        last_ts, _ = LAST_PRICE_SEEN.get(mid, (now(), yes_mid))
                        if (now() - last_ts) < (NEWS_LAG_MINUTES * 60):
                            continue  # not lagging yet → don't alert

                # Enforce edge threshold (higher quality)
                if sig["edge"] < required_edge:
                    continue

                if not should_alert(mid):
                    continue

                await send_discord(build_alert(m, sig, news_used=news_used, news_score=news_score))
                mark_alert(mid)
                alerted += 1

            logging.info(
                f"[scan] checked={checked} value_found={value_found} alerted={alerted} "
                f"universe={len(UNIVERSE)} news_terms={len(NEWS_TERMS)}"
            )

        except Exception as e:
            logging.error(f"[scan] loop error: {type(e).__name__}: {e}")

        await asyncio.sleep(SCAN_INTERVAL_SEC)

# =========================
# BOT EVENTS
# =========================
@bot.event
async def on_ready():
    logging.info(f"Logged in as: {bot.user}")
    await send_discord(
        "✅ PolyBrain is live. **Phase 3 (Silent News/Injury Intelligence)** is ON.\n"
        f"Sports: {', '.join(sorted(ALLOWED_SPORTS))} (UFC excluded)"
    )

    bot.loop.create_task(refresh_universe_loop())
    bot.loop.create_task(scan_loop())
    if NEWS_ENABLED:
        bot.loop.create_task(news_loop())

bot.run(DISCORD_BOT_TOKEN)
