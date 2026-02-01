import os
import re
import time
import asyncio
import logging
from datetime import datetime, timedelta, timezone
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

# Higher-quality defaults (you can override in Railway Variables)
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
NEWS_BOOST_EDGE_REDUCTION = float(os.getenv("NEWS_BOOST_EDGE_REDUCTION", "0.02"))  # reduce edge req by 2% on news-lag
NEWS_LAG_MINUTES = int(os.getenv("NEWS_LAG_MINUTES", "6"))          # require price lag after news

# Daily heartbeat (9:00 AM America/New_York default)
USER_TZ_OFFSET = os.getenv("USER_TZ_OFFSET", "-05:00")              # "-05:00"
HEARTBEAT_HOUR = int(os.getenv("HEARTBEAT_HOUR", "9"))
HEARTBEAT_MINUTE = int(os.getenv("HEARTBEAT_MINUTE", "0"))

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)

# =========================
# DISCORD
# =========================
intents = discord.Intents.default()
client = discord.Client(intents=intents)
CHANNEL: Optional[discord.TextChannel] = None

# =========================
# STATE
# =========================
UNIVERSE: Dict[str, Dict[str, Any]] = {}         # market_id -> market object
LAST_UNIVERSE_OK: float = 0.0

LAST_ALERT: Dict[str, float] = {}               # market_id -> ts
LAST_PRICE_SEEN: Dict[str, Tuple[float, float]] = {}  # market_id -> (ts, yes_mid) for "lag"

# news_terms: token -> last_seen_ts (only for recent news)
NEWS_TERMS: Dict[str, float] = {}
LAST_NEWS_OK: float = 0.0

# =========================
# HEARTBEAT STATS (rolling 24h)
# =========================
STATS: Dict[str, Any] = {
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

def stat_add(k: str, n: int = 1) -> None:
    if k in STATS:
        STATS[k] = int(STATS[k]) + n

def stat_set(k: str, v: Any) -> None:
    if k in STATS:
        STATS[k] = v

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
    q = norm_text(market_title(m))
    if "nfl" in q or "super bowl" in q or "afc" in q or "nfc" in q:
        return "nfl"
    if "nba" in q or "playoffs" in q or "western conference" in q or "eastern conference" in q:
        return "nba"
    if "nhl" in q or "stanley cup" in q:
        return "nhl"
    if "college basketball" in q or "mens college basketball" in q or "ncaa" in q or "cbb" in q or "march madness" in q:
        return "cbb"
    if "college football" in q or "cfb" in q or "bowl game" in q or "ncaa football" in q:
        return "cfb"
    return None

ALLOWED_SPORTS = {x.strip() for x in SPORTS_MODE.split(",") if x.strip()}

# =========================
# PRICES + FAIR VALUE (vig-adjusted)
# =========================
def extract_books(m: Dict[str, Any]) -> Optional[Dict[str, float]]:
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
            "quality": 2.0,
        }

    outs = m.get("outcomePrices")
    if isinstance(outs, list) and len(outs) == 2:
        y = to_float(outs[0])
        n = to_float(outs[1])
        if y is None or n is None:
            return None
        y = clamp01(y); n = clamp01(n)
        return {
            "yes_bid": clamp01(y - 0.005),
            "yes_ask": clamp01(y + 0.005),
            "no_bid":  clamp01(n - 0.005),
            "no_ask":  clamp01(n + 0.005),
            "quality": 1.0,
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

    if yes_spread > MAX_SPREAD and no_spread > MAX_SPREAD:
        return None

    edge_yes = fair_yes - b["yes_ask"]
    edge_no  = fair_no  - b["no_ask"]

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
# NEWS (silent) via ESPN public endpoints
# =========================
LEAGUE_TO_ESPN_PATH = {
    "nba": "basketball/nba",
    "nfl": "football/nfl",
    "nhl": "hockey/nhl",
    "cbb": "basketball/mens-college-basketball",
    "cfb": "football/college-football",
}

INJURY_KEYWORDS = {"injury","injured","out","questionable","doubtful","probable","ruled","inactive","scratch","day-to-day","gtd","downgraded","upgraded"}

async def http_get_with_backoff(http: httpx.AsyncClient, url: str, params: Dict[str, Any]) -> Any:
    waits = [0.8, 1.5, 3.0, 6.0, 12.0]
    last_err = None
    for i, w in enumerate(waits, start=1):
        try:
            r = await http.get(url, params=params, timeout=25)

            if r.status_code == 429:
                stat_add("rate_limits", 1)
                logging.warning(f"[http] status=429 attempt={i}/{len(waits)} wait={w}s url={url}")
                await asyncio.sleep(w)
                continue

            if r.status_code == 422:
                stat_add("http_4xx", 1)
                logging.warning(f"[http] status=422 attempt={i}/{len(waits)} wait={w}s url={url}")
                await asyncio.sleep(w)
                continue

            if 400 <= r.status_code < 500:
                stat_add("http_4xx", 1)
            if 500 <= r.status_code < 600:
                stat_add("http_5xx", 1)

            r.raise_for_status()
            return r.json()

        except Exception as e:
            last_err = e
            STATS["last_error"] = f"http: {type(e).__name__}: {e}"
            logging.warning(f"[http] error={type(e).__name__} attempt={i}/{len(waits)} wait={w}s url={url}")
            await asyncio.sleep(w)

    raise RuntimeError(last_err)

async def fetch_espn_news(http: httpx.AsyncClient, espn_path: str) -> List[Dict[str, Any]]:
    url = f"http://site.api.espn.com/apis/site/v2/sports/{espn_path}/news"
    data = await http_get_with_backoff(http, url, {"limit": "50"})
    arts = data.get("articles") if isinstance(data, dict) else None
    return arts if isinstance(arts, list) else []

async def news_loop() -> None:
    global NEWS_TERMS, LAST_NEWS_OK

    if not NEWS_ENABLED:
        logging.info("[news] disabled")
        return

    async with httpx.AsyncClient(headers={"User-Agent": "polybrain/1.0"}) as http:
        while True:
            try:
                cutoff = now() - (NEWS_LOOKBACK_MIN * 60)
                NEWS_TERMS = {k: ts for k, ts in NEWS_TERMS.items() if ts >= cutoff}

                for sport in sorted(ALLOWED_SPORTS):
                    espn_path = LEAGUE_TO_ESPN_PATH.get(sport)
                    if not espn_path:
                        continue

                    arts = await fetch_espn_news(http, espn_path)
                    for a in arts[:40]:
                        headline = (a.get("headline") or a.get("title") or "").strip()
                        if not headline:
                            continue

                        h = norm_text(headline)
                        if not any(k in h for k in INJURY_KEYWORDS):
                            continue

                        for t in tokens(headline, min_len=4)[:12]:
                            NEWS_TERMS[t] = now()

                LAST_NEWS_OK = now()
                stat_set("news_terms", len(NEWS_TERMS))
                logging.info(f"[news] terms={len(NEWS_TERMS)} fresh={NEWS_LOOKBACK_MIN}min")

            except Exception as e:
                STATS["last_error"] = f"news: {type(e).__name__}: {e}"
                logging.warning(f"[news] loop error: {type(e).__name__}: {e}")

            await asyncio.sleep(NEWS_POLL_SEC)

def news_matches_market(m: Dict[str, Any]) -> Tuple[bool, int]:
    if not NEWS_TERMS:
        return (False, 0)

    q_tokens = set(tokens(market_title(m), min_len=4))
    if not q_tokens:
        return (False, 0)

    score = sum(1 for t in q_tokens if t in NEWS_TERMS)
    return (score >= NEWS_MATCH_MIN_TERMS, score)

# =========================
# POLYMARKET UNIVERSE
# =========================
async def fetch_markets_page(http: httpx.AsyncClient, offset: int, limit: int = 200) -> List[Dict[str, Any]]:
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

            if r.status_code == 429:
                stat_add("rate_limits", 1)
                logging.warning(f"[poly] status=429 attempt={i}/{len(waits)} wait={w}s")
                await asyncio.sleep(w)
                continue

            if r.status_code == 422:
                stat_add("http_4xx", 1)
                logging.warning(f"[poly] status=422 attempt={i}/{len(waits)} (params rejected) wait={w}s")
                await asyncio.sleep(w)
                continue

            if 400 <= r.status_code < 500:
                stat_add("http_4xx", 1)
            if 500 <= r.status_code < 600:
                stat_add("http_5xx", 1)

            r.raise_for_status()
            data = r.json()
            return data if isinstance(data, list) else []

        except Exception as e:
            STATS["last_error"] = f"poly: {type(e).__name__}: {e}"
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
                    stat_set("universe_markets", len(UNIVERSE))
                    logging.info(f"[universe] markets={len(UNIVERSE)} refresh_ok sports={','.join(sorted(ALLOWED_SPORTS))}")
                else:
                    logging.warning(f"[universe] got 0 after filter (keeping cache={len(UNIVERSE)})")

            except Exception as e:
                age = (now() - LAST_UNIVERSE_OK) if LAST_UNIVERSE_OK else -1
                STATS["last_error"] = f"universe: {type(e).__name__}: {e}"
                logging.error(f"[universe] failed: {type(e).__name__} cache_age={age:.0f}s")

            await asyncio.sleep(UNIVERSE_REFRESH_SEC)

# =========================
# ALERTING
# =========================
async def send_discord(msg: str) -> None:
    global CHANNEL
    if CHANNEL is None:
        CHANNEL = client.get_channel(CHANNEL_ID)
        if CHANNEL is None:
            CHANNEL = await client.fetch_channel(CHANNEL_ID)
    await CHANNEL.send(msg)

def build_alert(m: Dict[str, Any], sig: Dict[str, Any], *, news_used: bool) -> str:
    title = market_title(m)
    link = market_link(m)

    conf = 5.0
    conf += min(4.0, sig["edge"] * 50.0)
    conf += 1.0 if sig.get("book_quality", 1.0) >= 2.0 else 0.0
    conf += 1.2 if news_used else 0.0
    conf = min(10.0, conf)

    why = "vig-adjusted fair value"
    if news_used:
        why += " + news/injury lag detected"

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

                # Phase 3: silent news -> require lag, allow slightly lower edge
                news_used = False
                required_edge = MIN_EDGE_BASE

                if NEWS_ENABLED:
                    nm, _score = news_matches_market(m)
                    if nm:
                        news_used = True
                        required_edge = max(0.03, MIN_EDGE_BASE - NEWS_BOOST_EDGE_REDUCTION)

                        yes_mid = (b["yes_bid"] + b["yes_ask"]) / 2.0
                        last = LAST_PRICE_SEEN.get(mid)
                        if last is None:
                            LAST_PRICE_SEEN[mid] = (now(), yes_mid)
                        else:
                            last_ts, last_mid = last
                            if abs(yes_mid - last_mid) >= 0.01:
                                LAST_PRICE_SEEN[mid] = (now(), yes_mid)

                        last_ts, _ = LAST_PRICE_SEEN.get(mid, (now(), yes_mid))
                        if (now() - last_ts) < (NEWS_LAG_MINUTES * 60):
                            continue

                if sig["edge"] < required_edge:
                    continue

                if not should_alert(mid):
                    continue

                await send_discord(build_alert(m, sig, news_used=news_used))
                mark_alert(mid)
                stat_add("alerts_sent", 1)
                alerted += 1

            # Heartbeat counters
            stat_add("markets_scanned", checked)
            stat_add("value_found", value_found)

            logging.info(
                f"[scan] checked={checked} value_found={value_found} alerted={alerted} "
                f"universe={len(UNIVERSE)} news_terms={len(NEWS_TERMS)}"
            )

        except Exception as e:
            STATS["last_error"] = f"scan: {type(e).__name__}: {e}"
            logging.error(f"[scan] loop error: {type(e).__name__}: {e}")

        await asyncio.sleep(SCAN_INTERVAL_SEC)

# =========================
# DAILY HEARTBEAT (9:00 AM local)
# =========================
def _parse_utc_offset(s: str) -> timezone:
    sign = -1 if s.startswith("-") else 1
    hh, mm = s[1:].split(":")
    return timezone(sign * timedelta(hours=int(hh), minutes=int(mm)))

LOCAL_TZ = _parse_utc_offset(USER_TZ_OFFSET)

def _next_local_run(hour: int, minute: int) -> datetime:
    now_local = datetime.now(tz=LOCAL_TZ)
    run = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if run <= now_local:
        run += timedelta(days=1)
    return run

async def heartbeat_loop() -> None:
    while True:
        try:
            next_run = _next_local_run(HEARTBEAT_HOUR, HEARTBEAT_MINUTE)
            sleep_s = (next_run - datetime.now(tz=LOCAL_TZ)).total_seconds()
            await asyncio.sleep(max(5, sleep_s))

            msg = (
                "🧠 **PolyBrain Daily Health Check**\n\n"
                f"⏱ Period: last 24h\n"
                f"📊 Markets scanned: **{STATS['markets_scanned']}**\n"
                f"🔍 Value candidates found: **{STATS['value_found']}**\n"
                f"🚨 Alerts sent: **{STATS['alerts_sent']}**\n"
                f"⚠️ Rate limits hit: **{STATS['rate_limits']}**\n"
                f"🌐 Universe markets: **{STATS['universe_markets']}**\n"
                f"📰 News terms tracked: **{STATS['news_terms']}**\n"
                f"🧱 HTTP 4xx: **{STATS['http_4xx']}** | HTTP 5xx: **{STATS['http_5xx']}**\n"
            )
            if STATS["last_error"]:
    msg += f"\n🔴 Last error: `{str(STATS['last_error'])[:180]}`"

    all_ok = "🟢 Status: **Running**"
    msg += f"\n{all_ok}"

            await send_discord(msg)

            # Reset rolling counters (keep universe/news snapshots)
            for k in ("markets_scanned", "value_found", "alerts_sent", "rate_limits", "http_4xx", "http_5xx", "last_error"):
                STATS[k] = 0

        except Exception as e:
            STATS["last_error"] = f"heartbeat: {type(e).__name__}: {e}"
            await asyncio.sleep(60)

# =========================
# BOT EVENTS
# =========================
@client.event
async def on_ready():
    logging.info(f"Logged in as: {client.user}")

    await send_discord(
        "✅ PolyBrain is live. **Phase 3 (Silent News/Injury Intelligence + Daily Heartbeat)** is ON.\n"
        f"Sports: {', '.join(sorted(ALLOWED_SPORTS))} (UFC excluded)\n"
        f"Daily heartbeat: {HEARTBEAT_HOUR:02d}:{HEARTBEAT_MINUTE:02d} local (offset {USER_TZ_OFFSET})"
    )

    client.loop.create_task(refresh_universe_loop())
    client.loop.create_task(scan_loop())
    if NEWS_ENABLED:
        client.loop.create_task(news_loop())
    client.loop.create_task(heartbeat_loop())

client.run(DISCORD_BOT_TOKEN)
