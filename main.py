import os
import re
import time
import json
import html
import asyncio
import logging
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, timezone
from xml.etree import ElementTree as ET

import httpx
import discord

# =========================
# CONFIG
# =========================
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()
DISCORD_CHANNEL_ID = os.getenv("DISCORD_CHANNEL_ID", "").strip()
if not DISCORD_BOT_TOKEN or not DISCORD_CHANNEL_ID:
    raise SystemExit("Missing DISCORD_BOT_TOKEN or DISCORD_CHANNEL_ID")
CHANNEL_ID = int(DISCORD_CHANNEL_ID)

GAMMA = os.getenv("POLY_GAMMA_BASE", "https://gamma-api.polymarket.com").strip().rstrip("/")

# Sports only (NO UFC)
SPORTS_ALLOW = {s.strip().lower() for s in os.getenv("SPORTS_MODE", "nfl,nba,nhl,cbb,cfb").split(",") if s.strip()}
SPORTS_EXCLUDE = {s.strip().lower() for s in os.getenv("SPORTS_EXCLUDE", "ufc,mma").split(",") if s.strip()}

# CONVICTION MODE (locked)
MIN_SCORE = int(os.getenv("MIN_SCORE", "90"))  # locked >= 90
ALLOW_PRE = os.getenv("ALLOW_PRE", "1").strip() not in ("0", "false", "False")   # locked BOTH
ALLOW_LIVE = os.getenv("ALLOW_LIVE", "1").strip() not in ("0", "false", "False") # locked BOTH
BANKROLL_ALLOC_TEXT = "70–80% bankroll"  # locked text
EXIT_1_PCT = 0.20  # +20% (locked)
EXIT_2_PCT = 0.40  # +40% (locked)

# Intervals
SCAN_EVERY_SEC = float(os.getenv("SCAN_EVERY_SEC", "15"))
UNIVERSE_REFRESH_SEC = float(os.getenv("UNIVERSE_REFRESH_SEC", "60"))
NEWS_REFRESH_SEC = float(os.getenv("NEWS_REFRESH_SEC", "90"))

# Market paging
MARKETS_PAGE_LIMIT = int(os.getenv("MARKETS_PAGE_LIMIT", "200"))
MARKETS_MAX_PAGES = int(os.getenv("MARKETS_MAX_PAGES", "10"))  # 10*200=2000 max

# Signal tuning (keep strict; you can lower later if YOU ask)
MOVE_CENTS_MIN = float(os.getenv("MOVE_CENTS_MIN", "6"))   # meaningful move in cents
COOLDOWN_SEC = int(os.getenv("COOLDOWN_SEC", str(45 * 60)))  # 45 min per market

HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "15"))
MAX_HTTP_RETRIES = int(os.getenv("MAX_HTTP_RETRIES", "6"))

# News feeds (RSS)
DEFAULT_FEEDS = [
    "https://sports.yahoo.com/rss/",
    "https://www.espn.com/espn/rss/news",
    "https://www.espn.com/espn/rss/nfl/news",
    "https://www.espn.com/espn/rss/nba/news",
    "https://www.espn.com/espn/rss/nhl/news",
    "https://www.espn.com/espn/rss/ncb/news",
    "https://www.espn.com/espn/rss/ncf/news",
]
NEWS_FEEDS = os.getenv("NEWS_FEEDS", "").strip()
FEEDS = [f.strip() for f in NEWS_FEEDS.split(",") if f.strip()] if NEWS_FEEDS else DEFAULT_FEEDS

# =========================
# LOGGING
# =========================
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper().strip()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("polybrain")

# =========================
# DISCORD
# =========================
intents = discord.Intents.default()
client = discord.Client(intents=intents)

async def _get_channel() -> discord.abc.Messageable:
    ch = client.get_channel(CHANNEL_ID)
    if ch is None:
        ch = await client.fetch_channel(CHANNEL_ID)
    return ch

async def send_discord(msg: str) -> None:
    try:
        ch = await _get_channel()
        await ch.send(msg)
    except Exception as e:
        log.error("[discord] send failed: %s: %s", type(e).__name__, e)

# =========================
# STATE
# =========================
STATS: Dict[str, Any] = {
    "universe_markets": 0,
    "markets_scanned": 0,
    "signals_sent": 0,
    "rate_limits": 0,
    "http_4xx": 0,
    "http_5xx": 0,
    "last_error": "",
    "news_terms": 0,
}

UNIVERSE: List[Dict[str, Any]] = []
UNIVERSE_TS = 0.0

# price memory per market id (for move detection)
LAST_PRICE: Dict[str, float] = {}
LAST_PRICE_TS: Dict[str, float] = {}

# news memory
NEWS_SEEN: set = set()
NEWS_RECENT: List[Dict[str, str]] = []
NEWS_RECENT_MAX = 120

# term -> count
NEWS_TERMS: Dict[str, int] = {}

# alert cooldown
LAST_ALERT: Dict[str, float] = {}

# =========================
# HELPERS
# =========================
def now_ts() -> float:
    return time.time()

def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))

def norm(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    return s

def cents(p: float) -> int:
    return int(round(clamp(p, 0.0, 1.0) * 100))

def fmt_cents(p: float) -> str:
    return f"{cents(p)}¢"

def safe_str(x: Any) -> str:
    return (str(x) if x is not None else "").strip()

def is_cooldown(key: str) -> bool:
    t = now_ts()
    last = LAST_ALERT.get(key, 0.0)
    if t - last < COOLDOWN_SEC:
        return True
    LAST_ALERT[key] = t
    return False

def market_url(m: Dict[str, Any]) -> str:
    slug = safe_str(m.get("slug"))
    if slug:
        return f"https://polymarket.com/market/{slug}"
    return "https://polymarket.com"

# =========================
# HTTP (backoff)
# =========================
async def http_get_text(hc: httpx.AsyncClient, url: str, params: Optional[dict] = None) -> str:
    wait = 0.8
    last_err: Optional[str] = None
    for attempt in range(1, MAX_HTTP_RETRIES + 1):
        try:
            r = await hc.get(url, params=params, timeout=HTTP_TIMEOUT)
            if r.status_code == 429:
                STATS["rate_limits"] += 1
                log.warning("[http] 429 attempt=%d/%d wait=%.1fs url=%s", attempt, MAX_HTTP_RETRIES, wait, url)
                await asyncio.sleep(wait)
                wait = min(wait * 1.9, 30.0)
                continue
            if 400 <= r.status_code < 500:
                STATS["http_4xx"] += 1
            if r.status_code >= 500:
                STATS["http_5xx"] += 1
            r.raise_for_status()
            return r.text
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            log.warning("[http] err attempt=%d/%d wait=%.1fs url=%s (%s)", attempt, MAX_HTTP_RETRIES, wait, url, last_err)
            await asyncio.sleep(wait)
            wait = min(wait * 1.9, 30.0)
    raise RuntimeError(last_err or "http_get_text failed")

async def http_get_json(hc: httpx.AsyncClient, url: str, params: Optional[dict] = None) -> Any:
    txt = await http_get_text(hc, url, params=params)
    try:
        return json.loads(txt)
    except Exception:
        return {"raw": txt}

# =========================
# NEWS (RSS)
# =========================
def parse_rss(xml_text: str) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    try:
        root = ET.fromstring(xml_text)
    except Exception:
        return out

    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        guid = (item.findtext("guid") or link or title).strip()
        desc = (item.findtext("description") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()

        if not title:
            continue

        out.append({
            "title": html.unescape(title),
            "link": link,
            "guid": guid,
            "desc": html.unescape(desc),
            "pub": pub,
        })

    return out

STOPWORDS = {
    "the","and","for","with","from","that","this","into","over","under","after","before","when","what",
    "your","youre","their","they","them","will","would","could","should","been","are","was","were","its",
    "his","her","him","she","he","to","of","in","on","at","as","by","is","it","a","an","or","vs","v",
    "game","games","match","season","team","teams","player","players","coach","coaches","report","reports",
    "today","live","final","wins","win","loss","losses","nfl","nba","nhl","ncaa",
}

def extract_strong_terms(text: str) -> List[str]:
    """
    We only keep "strong" terms to avoid junk alerts:
      - tokens length >= 4
      - NOT stopwords
      - plus 2-grams for names (e.g., "lebron james")
    """
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"[^A-Za-z0-9' \-]", " ", text)
    words = [w.strip(" -").lower() for w in text.split() if w.strip()]
    words = [w for w in words if len(w) >= 4 and w not in STOPWORDS and not w.isdigit()]

    terms: List[str] = []
    # singles
    for w in words[:30]:
        terms.append(w)
    # bigrams (names/teams)
    for i in range(min(len(words) - 1, 20)):
        terms.append(f"{words[i]} {words[i+1]}")

    # de-dupe
    out: List[str] = []
    seen = set()
    for t in terms:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out

async def refresh_news_loop() -> None:
    headers = {"User-Agent": "PolyBrain/1.0 (news)"}
    async with httpx.AsyncClient(headers=headers, follow_redirects=True) as hc:
        while True:
            try:
                new_items = 0
                for feed in FEEDS:
                    xml_text = await http_get_text(hc, feed)
                    items = parse_rss(xml_text)

                    for it in items[:25]:
                        key = (it.get("guid") or it.get("link") or it.get("title") or "").strip()
                        if not key or key in NEWS_SEEN:
                            continue
                        NEWS_SEEN.add(key)
                        new_items += 1

                        combo = f"{it.get('title','')} {it.get('desc','')}"
                        for term in extract_strong_terms(combo):
                            NEWS_TERMS[term] = NEWS_TERMS.get(term, 0) + 1

                        NEWS_RECENT.insert(0, it)
                        if len(NEWS_RECENT) > NEWS_RECENT_MAX:
                            NEWS_RECENT.pop()

                    await asyncio.sleep(0.2)

                STATS["news_terms"] = len(NEWS_TERMS)
                log.info("[news] terms=%d feeds=%d new_items=%d", STATS["news_terms"], len(FEEDS), new_items)

            except Exception as e:
                STATS["last_error"] = f"news: {type(e).__name__}: {e}"
                log.error("[news] failed: %s", STATS["last_error"])

            await asyncio.sleep(NEWS_REFRESH_SEC)

# =========================
# MARKET FILTERING (APP-ONLY GAME MARKETS)
# =========================
def market_text_blob(m: Dict[str, Any]) -> str:
    parts = []
    for k in ("question", "title", "name", "slug"):
        v = m.get(k)
        if isinstance(v, str) and v.strip():
            parts.append(v)
    event = m.get("event")
    if isinstance(event, dict):
        for k in ("title", "slug", "category"):
            v = event.get(k)
            if isinstance(v, str) and v.strip():
                parts.append(v)
    return norm(" ".join(parts))

def is_sport_allowed(m: Dict[str, Any]) -> bool:
    t = market_text_blob(m)
    if any(x in t for x in SPORTS_EXCLUDE):
        return False
    # allow if any allowed league token appears anywhere in metadata/text
    return any(tag in t for tag in SPORTS_ALLOW)

def get_outcomes_and_prices(m: Dict[str, Any]) -> Optional[List[Tuple[str, float]]]:
    """
    Normalize to list of (outcome_name, price_float 0..1)
    Supports common Gamma shapes:
      - outcomes: ["TEAM A", "TEAM B"] and outcomePrices: ["0.61","0.39"]
      - outcomes: [{"name":...,"price":...}, ...]
    """
    outcomes = m.get("outcomes")
    outcome_prices = m.get("outcomePrices") or m.get("outcome_prices")

    # Case A: outcomes list[str] + outcomePrices list
    if isinstance(outcomes, list) and outcomes and all(isinstance(x, str) for x in outcomes) and isinstance(outcome_prices, list):
        if len(outcomes) == len(outcome_prices) == 2:
            try:
                p0 = float(outcome_prices[0])
                p1 = float(outcome_prices[1])
                return [(outcomes[0].strip(), clamp(p0, 0.0, 1.0)), (outcomes[1].strip(), clamp(p1, 0.0, 1.0))]
            except Exception:
                return None

    # Case B: outcomes list[dict]
    if isinstance(outcomes, list) and outcomes and all(isinstance(x, dict) for x in outcomes) and len(outcomes) == 2:
        out: List[Tuple[str, float]] = []
        for o in outcomes:
            name = safe_str(o.get("name") or o.get("outcome") or "")
            p = o.get("price")
            if p is None:
                p = o.get("probability")
            try:
                fp = float(p)
            except Exception:
                return None
            out.append((name.strip(), clamp(fp, 0.0, 1.0)))
        return out if len(out) == 2 else None

    return None

def is_game_market(m: Dict[str, Any]) -> bool:
    """
    App-style game market heuristic:
      - exactly 2 outcomes
      - outcome names are NOT yes/no
      - metadata/title resembles a matchup (vs/@) OR event title suggests matchup
    """
    op = get_outcomes_and_prices(m)
    if not op:
        return False

    names = [norm(op[0][0]), norm(op[1][0])]
    if any(n in ("yes", "no") for n in names):
        return False

    blob = market_text_blob(m)
    # matchup hint
    matchup_hint = (" vs " in blob) or ("@" in blob) or (" v " in blob)

    # Also accept if event title contains two team-ish words separated by vs
    if matchup_hint:
        return True

    # fallback: if both outcome names appear in blob, it’s likely a game listing
    if names[0] and names[1] and (names[0] in blob and names[1] in blob):
        return True

    return False

def market_phase_label(m: Dict[str, Any]) -> str:
    """
    Label PRE or LIVE (best-effort).
    We allow BOTH, but we still label.
    """
    event = m.get("event") if isinstance(m.get("event"), dict) else {}
    # direct live hints
    for key in ("isLive", "live", "inPlay", "in_play"):
        if isinstance(m.get(key), bool) and m.get(key):
            return "LIVE"
        if isinstance(event.get(key), bool) and event.get(key):
            return "LIVE"

    # startTime heuristics
    start = m.get("startTime") or m.get("start_time") or event.get("startTime") or event.get("start_time")
    if isinstance(start, (int, float)):
        start_ts = float(start)
    else:
        start_ts = None
        if isinstance(start, str) and start.strip():
            # try ISO
            try:
                dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
                start_ts = dt.timestamp()
            except Exception:
                start_ts = None

    if start_ts is None:
        # unknown; treat as PRE to avoid claiming live
        return "PRE"

    now = datetime.now(timezone.utc).timestamp()
    # if started within last ~5 hours, call it LIVE
    if now >= start_ts and (now - start_ts) <= (5 * 3600):
        return "LIVE"
    return "PRE"

# =========================
# UNIVERSE REFRESH
# =========================
async def fetch_markets_page(hc: httpx.AsyncClient, offset: int) -> List[Dict[str, Any]]:
    """
    IMPORTANT: no unsupported params like order/ascending (prevents 422).
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
    if isinstance(data, dict) and isinstance(data.get("markets"), list):
        return data["markets"]
    return []

async def refresh_universe_loop() -> None:
    headers = {"User-Agent": "PolyBrain/1.0 (universe)"}
    global UNIVERSE, UNIVERSE_TS

    async with httpx.AsyncClient(headers=headers) as hc:
        while True:
            t0 = now_ts()
            try:
                all_markets: List[Dict[str, Any]] = []
                for page in range(MARKETS_MAX_PAGES):
                    chunk = await fetch_markets_page(hc, page * MARKETS_PAGE_LIMIT)
                    if not chunk:
                        break
                    all_markets.extend(chunk)
                    await asyncio.sleep(0.15)

                filtered = []
                for m in all_markets:
                    if not is_sport_allowed(m):
                        continue
                    if not is_game_market(m):
                        continue
                    phase = market_phase_label(m)
                    if phase == "PRE" and not ALLOW_PRE:
                        continue
                    if phase == "LIVE" and not ALLOW_LIVE:
                        continue
                    filtered.append(m)

                UNIVERSE = filtered
                UNIVERSE_TS = now_ts()
                STATS["universe_markets"] = len(UNIVERSE)

                dt = now_ts() - t0
                log.info("[universe] markets=%d refresh_ok dt=%.1fs allow=%s", len(UNIVERSE), dt, ",".join(sorted(SPORTS_ALLOW)))

            except Exception as e:
                STATS["last_error"] = f"universe: {type(e).__name__}: {e}"
                log.error("[universe] failed: %s", STATS["last_error"])

            await asyncio.sleep(UNIVERSE_REFRESH_SEC)

# =========================
# SCORING (CONVICTION MODE)
# =========================
def match_news_terms(market_blob: str) -> Tuple[int, List[str]]:
    """
    Score “strong” matches only:
      - term count in NEWS_TERMS >= 2 (so it’s actually recurring news)
      - term contains a space (bigrams) OR token length >= 6
    """
    matched: List[str] = []
    score = 0

    # Use top frequent terms first
    items = sorted(NEWS_TERMS.items(), key=lambda x: x[1], reverse=True)[:250]
    for term, cnt in items:
        if cnt < 2:
            continue
        if (" " not in term) and (len(term) < 6):
            continue
        if term in market_blob:
            matched.append(term)
            # weighted; bigrams count more
            score += 14 if " " in term else 8
            if len(matched) >= 5:
                break

    return score, matched

def compute_move_score(market_id: str, current_side_price: float) -> Tuple[int, float]:
    """
    Movement score from the chosen side price.
    """
    prev = LAST_PRICE.get(market_id)
    prev_ts = LAST_PRICE_TS.get(market_id, 0.0)

    LAST_PRICE[market_id] = current_side_price
    LAST_PRICE_TS[market_id] = now_ts()

    if prev is None:
        return 0, 0.0

    # only trust if last seen within 15m
    if now_ts() - prev_ts > 15 * 60:
        return 0, 0.0

    move_c = abs(current_side_price - prev) * 100.0
    if move_c < MOVE_CENTS_MIN:
        return 0, move_c

    # scale move into score (cap)
    move_score = int(min(30, move_c * 2.2))
    return move_score, move_c

def pick_side(outcomes: List[Tuple[str, float]]) -> Tuple[str, float]:
    """
    Choose the “value side” candidate:
      - prefer the cheaper side (bigger potential upside)
      - (this is a heuristic; true fair value comes from intel)
    """
    (n0, p0), (n1, p1) = outcomes
    if p0 <= p1:
        return n0, p0
    return n1, p1

def estimate_fair(entry_price: float, score: int) -> Tuple[float, float]:
    """
    Heuristic fair range for alert display (not a guarantee).
    For conviction signals (>=90) we show a meaningful lift over entry.
    """
    entry = clamp(entry_price, 0.01, 0.99)
    # map score 90..100 -> uplift 18..30 cents (cap)
    uplift_c = 18 + (score - 90) * 1.2
    uplift_c = clamp(uplift_c, 18, 30)
    lo = clamp(entry + uplift_c / 100.0, 0.02, 0.99)
    hi = clamp(lo + 0.06, 0.03, 0.99)
    return lo, hi

def score_market(m: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
    """
    Total score 0..100. Conviction requires >= MIN_SCORE.
    Components:
      - strong news matches (0..70)
      - move confirmation (0..30)
    """
    mid = safe_str(m.get("id") or m.get("marketId") or m.get("market_id") or "")
    if not mid:
        return 0, {}

    blob = market_text_blob(m)
    outcomes = get_outcomes_and_prices(m)
    if not outcomes:
        return 0, {}

    side_name, side_price = pick_side(outcomes)
    side_price = clamp(side_price, 0.01, 0.99)

    news_score, matched = match_news_terms(blob)
    move_score, move_cents = compute_move_score(mid, side_price)

    total = int(clamp(news_score + move_score, 0, 100))

    meta = {
        "market_id": mid,
        "phase": market_phase_label(m),
        "sport": next((s.upper() for s in sorted(SPORTS_ALLOW) if s in blob), "SPORTS"),
        "game": safe_str(m.get("title") or m.get("question") or m.get("name") or "Game"),
        "side": side_name,
        "price": side_price,
        "matched_terms": matched,
        "move_cents": move_cents,
        "url": market_url(m),
    }
    return total, meta

# =========================
# ALERT (LOCKED WORDING)
# =========================
def build_conviction_alert(score: int, meta: Dict[str, Any]) -> str:
    sport = meta.get("sport", "SPORTS")
    phase = meta.get("phase", "PRE")
    game = meta.get("game", "Game")
    side = meta.get("side", "YES")
    price = float(meta.get("price", 0.5))
    url = meta.get("url", "https://polymarket.com")
    matched = meta.get("matched_terms", []) or []
    move_cents = float(meta.get("move_cents", 0.0))

    fair_lo, fair_hi = estimate_fair(price, score)

    # Exit prices based on entry
    tp1 = clamp(price * (1.0 + EXIT_1_PCT), 0.01, 0.99)
    tp2 = clamp(price * (1.0 + EXIT_2_PCT), 0.01, 0.99)

    # Reasons (locked phrasing; filled with specifics)
    reasons = []
    if matched:
        reasons.append(f"• Confirmed news terms: {', '.join(matched[:3])}")
    if move_cents >= MOVE_CENTS_MIN:
        reasons.append(f"• Price confirmation: moved ~{move_cents:.1f}¢ recently")
    reasons.append("• App-only game market filter ✅")

    # Exact locked format + wording
    msg = (
        f"🧠 PolyBrain CONVICTION SIGNAL (Score: {score}/100)\n\n"
        f"🏟️ Phase: {phase}\n"
        f"🎮 Sport: {sport}\n"
        f"📊 Market: {game} — Moneyline\n"
        f"🎯 Side: {side} YES\n"
        f"💰 Price: {fmt_cents(price)}\n"
        f"📈 Fair Value: ~{fmt_cents(fair_lo)}–{fmt_cents(fair_hi)}\n\n"
        f"🔎 Edge:\n"
        + "\n".join(reasons) + "\n\n"
        f"💼 Position:\n"
        f"• Allocate: {BANKROLL_ALLOC_TEXT}\n\n"
        f"📤 Exit Plan:\n"
        f"• Sell 50% at +20% (~{fmt_cents(tp1)})\n"
        f"• Sell remaining 50% at +40% (~{fmt_cents(tp2)})\n\n"
        f"⚠️ High-conviction signal — not a guarantee.\n"
        f"🔗 {url}"
    )
    return msg

# =========================
# SCAN LOOP
# =========================
async def scan_loop() -> None:
    while True:
        try:
            snap = list(UNIVERSE)
            checked = 0
            sent = 0

            for m in snap:
                checked += 1
                score, meta = score_market(m)
                if score < MIN_SCORE:
                    continue

                key = f"{meta.get('market_id','')}|{meta.get('side','')}"
                if is_cooldown(key):
                    continue

                alert = build_conviction_alert(score, meta)
                await send_discord(alert)
                STATS["signals_sent"] += 1
                sent += 1

                # pacing so discord + API aren’t hammered
                await asyncio.sleep(0.6)

            STATS["markets_scanned"] += checked
            log.info("[scan] checked=%d sent=%d universe=%d news_terms=%d", checked, sent, len(snap), len(NEWS_TERMS))

        except Exception as e:
            STATS["last_error"] = f"scan: {type(e).__name__}: {e}"
            log.error("[scan] failed: %s", STATS["last_error"])

        await asyncio.sleep(SCAN_EVERY_SEC)

# =========================
# HEARTBEAT
# =========================
async def heartbeat_loop() -> None:
    # every 6 hours
    while True:
        await asyncio.sleep(6 * 3600)
        msg = (
            "🫀 PolyBrain Health Check\n"
            f"• Universe markets: {STATS['universe_markets']}\n"
            f"• News terms tracked: {len(NEWS_TERMS)}\n"
            f"• Signals sent: {STATS['signals_sent']}\n"
            f"• Rate limits: {STATS['rate_limits']}\n"
            f"• Last error: {str(STATS['last_error'])[:180] if STATS['last_error'] else 'none'}"
        )
        await send_discord(msg)

# =========================
# DISCORD EVENTS
# =========================
@client.event
async def on_ready():
    log.info("Logged in as: %s", client.user)
    await send_discord(
        "✅ PolyBrain is live.\n"
        f"🔒 CONVICTION MODE: alerts only when Score ≥ {MIN_SCORE}/100\n"
        f"🏟️ Markets: app-only game markets (moneyline)\n"
        f"🎮 Sports: {', '.join(sorted([s.upper() for s in SPORTS_ALLOW]))} | Excluding: {', '.join(sorted([s.upper() for s in SPORTS_EXCLUDE]))}\n"
        f"📌 Allowed phases: {'LIVE' if ALLOW_LIVE else ''} {'PRE' if ALLOW_PRE else ''}\n"
        f"💼 Position: {BANKROLL_ALLOC_TEXT}\n"
        f"📤 Exits: +20% / +40%"
    )

    asyncio.create_task(refresh_universe_loop())
    asyncio.create_task(refresh_news_loop())
    asyncio.create_task(scan_loop())
    asyncio.create_task(heartbeat_loop())

# =========================
# RUN
# =========================
client.run(DISCORD_BOT_TOKEN)
