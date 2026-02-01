import os
import re
import json
import time
import asyncio
import logging
from typing import Any, Dict, List, Optional, Tuple

import httpx
import discord

# =========================
# LOGGING
# =========================
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper().strip()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
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

# You want ONLY what you see in the app, and NO UFC.
SPORTS_ALLOW = set(
    s.strip().lower()
    for s in os.getenv("SPORTS_MODE", "nfl,nba,nhl,cbb,cfb").split(",")
    if s.strip()
)
SPORTS_EXCLUDE = set(
    s.strip().lower()
    for s in os.getenv("SPORTS_EXCLUDE", "ufc,mma").split(",")
    if s.strip()
)

# Universe refresh + scan pacing
UNIVERSE_REFRESH_SEC = float(os.getenv("UNIVERSE_REFRESH_SEC", "30"))  # refresh markets list
SCAN_INTERVAL_SEC = float(os.getenv("SCAN_INTERVAL_SEC", "12"))        # scan loop

# API paging
MARKETS_PAGE_LIMIT = int(os.getenv("MARKETS_PAGE_LIMIT", "200"))
MARKETS_MAX_PAGES = int(os.getenv("MARKETS_MAX_PAGES", "15"))  # 15*200=3000 max

# News ingestion
NEWS_ENABLED = os.getenv("NEWS_ENABLED", "1").strip() not in ("0", "false", "False")
NEWS_REFRESH_SEC = float(os.getenv("NEWS_REFRESH_SEC", "180"))  # pull RSS every 3 min
NEWS_TERMS_MAX = int(os.getenv("NEWS_TERMS_MAX", "120"))

# Feeds (defaults = 5 feeds)
DEFAULT_NEWS_FEEDS = [
    # NFL
    "https://www.espn.com/espn/rss/nfl/news",
    # NBA
    "https://www.espn.com/espn/rss/nba/news",
    # NHL
    "https://www.espn.com/espn/rss/nhl/news",
    # CFB
    "https://www.espn.com/espn/rss/ncf/news",
    # CBB
    "https://www.espn.com/espn/rss/ncb/news",
]
NEWS_FEEDS = [u.strip() for u in os.getenv("NEWS_FEEDS", ",".join(DEFAULT_NEWS_FEEDS)).split(",") if u.strip()]

# Optional: only alert if "confidence score" >= this number (0-100)
ALERT_SCORE_MIN = int(os.getenv("ALERT_SCORE_MIN", "85"))

# =========================
# DISCORD CLIENT
# =========================
intents = discord.Intents.default()
client = discord.Client(intents=intents)

# =========================
# STATE / STATS
# =========================
STATE: Dict[str, Any] = {
    "universe": [],          # filtered markets list
    "universe_ts": 0.0,
    "news_terms": set(),     # extracted terms
    "news_ts": 0.0,
}

STATS: Dict[str, Any] = {
    "universe_markets": 0,
    "markets_scanned": 0,
    "value_found": 0,
    "alerts_sent": 0,
    "rate_limits": 0,
    "http_4xx": 0,
    "http_5xx": 0,
    "last_error": None,
}

# =========================
# HELPERS
# =========================
def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()

def _collect_market_tokens(m: Dict[str, Any]) -> List[str]:
    """
    Collect possible category/league identifiers from market objects.
    Defensive: Polymarket fields can vary.
    """
    tokens: List[str] = []

    for k in ("category", "group", "subcategory", "eventSlug", "slug", "title", "question"):
        v = m.get(k)
        if isinstance(v, str) and v.strip():
            tokens.append(_norm(v))

    tags = m.get("tags")
    if isinstance(tags, list):
        for t in tags:
            if isinstance(t, dict):
                tokens.append(_norm(t.get("slug", "")))
                tokens.append(_norm(t.get("name", "")))
            elif isinstance(t, str):
                tokens.append(_norm(t))

    event = m.get("event")
    if isinstance(event, dict):
        tokens.append(_norm(event.get("slug", "")))
        tokens.append(_norm(event.get("title", "")))
        tokens.append(_norm(event.get("category", "")))

    series = m.get("series")
    if isinstance(series, dict):
        tokens.append(_norm(series.get("slug", "")))
        tokens.append(_norm(series.get("title", "")))

    blob = " ".join(tokens)
    return [t for t in blob.split() if t]

def is_allowed_sport_market(m: Dict[str, Any]) -> bool:
    """
    Option A: metadata-first filter + hard exclude UFC/MMA.
    """
    tokens = _collect_market_tokens(m)
    if not tokens:
        return False
    tokset = set(tokens)

    if tokset & SPORTS_EXCLUDE:
        return False
    return bool(tokset & SPORTS_ALLOW)

def _market_text(m: Dict[str, Any]) -> str:
    # used for news term matching
    parts = []
    for k in ("question", "title", "slug"):
        v = m.get(k)
        if isinstance(v, str) and v.strip():
            parts.append(v)
    event = m.get("event")
    if isinstance(event, dict):
        for k in ("title", "slug"):
            v = event.get(k)
            if isinstance(v, str) and v.strip():
                parts.append(v)
    return _norm(" ".join(parts))

async def send_discord(msg: str) -> None:
    """
    Robust send: tries cached channel, otherwise fetches.
    """
    try:
        ch = client.get_channel(CHANNEL_ID)
        if ch is None:
            ch = await client.fetch_channel(CHANNEL_ID)
        await ch.send(msg)
    except Exception as e:
        logging.error(f"[discord] send failed: {type(e).__name__}: {e}")

def _cut(s: str, n: int = 1800) -> str:
    return s if len(s) <= n else s[: n - 3] + "..."

# =========================
# HTTP (with backoff)
# =========================
async def http_get_json(
    hc: httpx.AsyncClient,
    url: str,
    params: Optional[Dict[str, Any]] = None,
    timeout: float = 20.0,
    attempts: int = 6,
) -> Any:
    wait = 0.8
    last_err: Optional[str] = None

    for i in range(1, attempts + 1):
        try:
            r = await hc.get(url, params=params, timeout=timeout)
            status = r.status_code

            if status == 429:
                STATS["rate_limits"] += 1
                logging.warning(f"[http] status=429 attempt={i}/{attempts} wait={wait:.1f}s url={url}")
                await asyncio.sleep(wait)
                wait = min(wait * 1.9, 30.0)
                continue

            if 400 <= status < 500:
                STATS["http_4xx"] += 1
            if status >= 500:
                STATS["http_5xx"] += 1

            r.raise_for_status()
            return r.json()

        except httpx.HTTPStatusError as e:
            last_err = f"HTTP {e.response.status_code} {e.response.text[:160]}"
            if e.response.status_code in (422, 400):
                # Usually bad params; don't hammer
                logging.warning(f"[http] {last_err} url={url} params={params}")
                break
            logging.warning(f"[http] status err attempt={i}/{attempts} wait={wait:.1f}s url={url}")
            await asyncio.sleep(wait)
            wait = min(wait * 1.9, 30.0)

        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            logging.warning(f"[http] err attempt={i}/{attempts} wait={wait:.1f}s url={url} ({last_err})")
            await asyncio.sleep(wait)
            wait = min(wait * 1.9, 30.0)

    raise RuntimeError(last_err or "http_get_json failed")

# =========================
# POLYMARKET UNIVERSE
# =========================
async def fetch_markets_page(hc: httpx.AsyncClient, offset: int) -> List[Dict[str, Any]]:
    """
    /markets endpoint supports paging; keep params conservative to avoid 422.
    """
    url = f"{GAMMA}/markets"
    params = {
    "active": "true",
    "closed": "false",
    "limit": MARKETS_PAGE_LIMIT,
    "offset": offset,
}
    data = await http_get_json(hc, url, params=params)
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and "markets" in data and isinstance(data["markets"], list):
        return data["markets"]
    return []

async def refresh_universe_loop() -> None:
    async with httpx.AsyncClient(headers={"User-Agent": "PolyBrain/1.0"}) as hc:
        while True:
            try:
                t0 = time.time()
                all_markets: List[Dict[str, Any]] = []

                for page in range(MARKETS_MAX_PAGES):
                    offset = page * MARKETS_PAGE_LIMIT
                    batch = await fetch_markets_page(hc, offset)
                    if not batch:
                        break
                    all_markets.extend(batch)
                    # gentle pacing so we don't get 429
                    await asyncio.sleep(0.25)

                filtered = [m for m in all_markets if is_allowed_sport_market(m)]

                STATE["universe"] = filtered
                STATE["universe_ts"] = time.time()

                STATS["universe_markets"] = len(filtered)
                logging.info(f"[universe] markets={len(filtered)} refresh_ok dt={(time.time()-t0):.1f}s sports={sorted(SPORTS_ALLOW)}")

            except Exception as e:
                STATS["last_error"] = f"universe: {type(e).__name__}: {e}"
                logging.error(f"[universe] failed: {type(e).__name__}: {e}")

            await asyncio.sleep(UNIVERSE_REFRESH_SEC)

# =========================
# NEWS INGESTION (RSS)
# =========================
RSS_TITLE_RE = re.compile(r"<title><!\[CDATA\[(.*?)\]\]></title>|<title>(.*?)</title>", re.IGNORECASE)

def extract_titles_from_rss(xml_text: str) -> List[str]:
    titles: List[str] = []
    for m in RSS_TITLE_RE.finditer(xml_text or ""):
        t = (m.group(1) or m.group(2) or "").strip()
        if t and t.lower() not in ("rss", "channel"):
            titles.append(t)
    return titles

def build_terms_from_titles(titles: List[str], max_terms: int) -> List[str]:
    """
    Build a small term set from RSS titles.
    We keep it simple + safe: take meaningful tokens and short n-grams.
    """
    bad = set(["the","and","for","with","from","that","this","over","under","vs","at","in","on","to","a","an","of"])
    terms: List[str] = []

    for title in titles:
        t = _norm(title)
        words = [w for w in t.split() if w not in bad and len(w) >= 3]
        # single tokens
        for w in words[:12]:
            terms.append(w)
        # 2-grams (helps with team names)
        for i in range(min(len(words)-1, 8)):
            terms.append(f"{words[i]} {words[i+1]}")

    # de-dupe while preserving order
    out: List[str] = []
    seen = set()
    for x in terms:
        if x not in seen:
            seen.add(x)
            out.append(x)
        if len(out) >= max_terms:
            break
    return out

async def news_loop() -> None:
    if not NEWS_ENABLED:
        logging.info("[news] disabled")
        return

    async with httpx.AsyncClient(headers={"User-Agent": "PolyBrain/1.0"}) as hc:
        while True:
            try:
                titles: List[str] = []
                for feed in NEWS_FEEDS:
                    r = await hc.get(feed, timeout=20)
                    r.raise_for_status()
                    titles.extend(extract_titles_from_rss(r.text))
                    await asyncio.sleep(0.2)

                terms = build_terms_from_titles(titles, NEWS_TERMS_MAX)
                STATE["news_terms"] = set(terms)
                STATE["news_ts"] = time.time()

                logging.info(f"[news] terms={len(terms)} feeds={len(NEWS_FEEDS)}")

            except Exception as e:
                STATS["last_error"] = f"news: {type(e).__name__}: {e}"
                logging.error(f"[news] failed: {type(e).__name__}: {e}")

            await asyncio.sleep(NEWS_REFRESH_SEC)

# =========================
# SCAN / ALERT LOGIC
# =========================
def score_market_basic(m: Dict[str, Any], news_terms: set) -> Tuple[int, List[str]]:
    """
    Phase 2-ish: "intelligence" without pretending we have true injury APIs.
    We score with:
      - News match count (terms appearing in the market text)
      - Live-ish markets tend to have more signal, but we keep conservative.

    Returns: (score 0..100, reasons)
    """
    text = _market_text(m)
    reasons: List[str] = []
    if not text:
        return 0, reasons

    hits = 0
    hit_terms: List[str] = []
    for term in list(news_terms)[:NEWS_TERMS_MAX]:
        if term and term in text:
            hits += 1
            if len(hit_terms) < 4:
                hit_terms.append(term)

    if hits > 0:
        reasons.append(f"news_hits={hits} ({', '.join(hit_terms)})")

    # Basic conservative mapping:
    # 0 hits => 0 score
    # 1 hit  => 60
    # 2 hits => 75
    # 3 hits => 85
    # 4+     => 92
    if hits <= 0:
        score = 0
    elif hits == 1:
        score = 60
    elif hits == 2:
        score = 75
    elif hits == 3:
        score = 85
    else:
        score = 92

    return score, reasons

def format_alert(m: Dict[str, Any], score: int, reasons: List[str]) -> str:
    q = (m.get("question") or m.get("title") or "Market").strip()
    slug = m.get("slug", "")
    url = f"https://polymarket.com/market/{slug}" if slug else "https://polymarket.com"

    msg = (
        f"🧠 **PolyBrain Signal (Phase 2)**\n"
        f"🎯 **Score:** {score}/100\n"
        f"📌 **Market:** {q}\n"
        f"🔎 **Why:** {', '.join(reasons) if reasons else 'news-linked context detected'}\n"
        f"🔗 {url}\n"
        f"\n"
        f"⚠️ This is an *info alert* (not a guarantee). Use bankroll rules."
    )
    return _cut(msg, 1800)

async def scan_loop() -> None:
    """
    Scans filtered universe and emits alerts when score >= ALERT_SCORE_MIN.
    This is still conservative; you can lower ALERT_SCORE_MIN later.
    """
    last_alerted: Dict[str, float] = {}  # slug -> ts (cooldown)
    cooldown_sec = 60 * 20  # 20 min

    while True:
        try:
            universe: List[Dict[str, Any]] = STATE.get("universe") or []
            news_terms: set = STATE.get("news_terms") or set()

            checked = 0
            value_found = 0
            alerted = 0

            for m in universe:
                checked += 1
                slug = str(m.get("slug") or "")
                if not slug:
                    continue

                # cooldown
                ts = last_alerted.get(slug, 0.0)
                if time.time() - ts < cooldown_sec:
                    continue

                score, reasons = score_market_basic(m, news_terms)
                if score >= ALERT_SCORE_MIN:
                    value_found += 1
                    await send_discord(format_alert(m, score, reasons))
                    STATS["alerts_sent"] += 1
                    alerted += 1
                    last_alerted[slug] = time.time()
                    await asyncio.sleep(0.35)  # discord pacing

            STATS["markets_scanned"] = checked
            STATS["value_found"] = value_found

            logging.info(
                f"[scan] checked={checked} value_found={value_found} alerted={alerted} "
                f"universe={STATS['universe_markets']} news_terms={len(news_terms)}"
            )

        except Exception as e:
            STATS["last_error"] = f"scan: {type(e).__name__}: {e}"
            logging.error(f"[scan] failed: {type(e).__name__}: {e}")

        await asyncio.sleep(SCAN_INTERVAL_SEC)

# =========================
# HEARTBEAT (daily)
# =========================
def _next_local_run(hour: int, minute: int) -> float:
    # simple next run in local time (server time)
    now = time.localtime()
    today = time.mktime((now.tm_year, now.tm_mon, now.tm_mday, hour, minute, 0, now.tm_wday, now.tm_yday, now.tm_isdst))
    if today <= time.time():
        tomorrow = time.mktime((now.tm_year, now.tm_mon, now.tm_mday + 1, hour, minute, 0, now.tm_wday, now.tm_yday, now.tm_isdst))
        return tomorrow
    return today

async def heartbeat_loop() -> None:
    # daily at 9:00 server local time
    while True:
        try:
            nxt = _next_local_run(9, 0)
            sleep_s = max(5.0, nxt - time.time())
            await asyncio.sleep(sleep_s)

            msg = (
                f"🧠 **PolyBrain Daily Health Check**\n\n"
                f"📊 Markets scanned: **{STATS['markets_scanned']}**\n"
                f"💎 Value candidates: **{STATS['value_found']}**\n"
                f"📣 Alerts sent: **{STATS['alerts_sent']}**\n"
                f"⚠️ Rate limits hit: **{STATS['rate_limits']}**\n"
                f"🧩 Universe markets: **{STATS['universe_markets']}**\n"
                f"📰 News terms tracked: **{len(STATE.get('news_terms') or set())}**\n"
                f"📦 HTTP 4xx: **{STATS['http_4xx']}** | HTTP 5xx: **{STATS['http_5xx']}**\n"
            )
            if STATS.get("last_error"):
                msg += f"\n🔴 Last error: `{str(STATS['last_error'])[:180]}`"

            await send_discord(_cut(msg, 1800))

        except Exception as e:
            STATS["last_error"] = f"heartbeat: {type(e).__name__}: {e}"
            logging.error(f"[heartbeat] failed: {type(e).__name__}: {e}")
            await asyncio.sleep(60)

# =========================
# BOT EVENTS
# =========================
@client.event
async def on_ready():
    logging.info(f"Logged in as: {client.user}")
    await send_discord(
        "✅ PolyBrain is live.\n"
        f"🏈 Sports: {', '.join(sorted(SPORTS_ALLOW))} | ❌ Excluding: {', '.join(sorted(SPORTS_EXCLUDE))}\n"
        f"📰 News ingestion: {'ON' if NEWS_ENABLED else 'OFF'} | 🎯 Alert threshold: {ALERT_SCORE_MIN}/100"
    )

    # start background tasks (only once)
    if not STATE.get("_tasks_started"):
        STATE["_tasks_started"] = True
        asyncio.create_task(refresh_universe_loop())
        asyncio.create_task(scan_loop())
        asyncio.create_task(heartbeat_loop())
        if NEWS_ENABLED:
            asyncio.create_task(news_loop())

# =========================
# ENTRY
# =========================
def main() -> None:
    client.run(DISCORD_BOT_TOKEN)

if __name__ == "__main__":
    main()
