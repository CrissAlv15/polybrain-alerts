import os
import asyncio
import random
import time
import traceback
from typing import Any, Dict, List, Optional, Tuple

import discord
import httpx

# =========================
# ENV / CONFIG
# =========================
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()
DISCORD_CHANNEL_ID_RAW = os.getenv("DISCORD_CHANNEL_ID", "").strip()

if not DISCORD_BOT_TOKEN or not DISCORD_CHANNEL_ID_RAW:
    raise SystemExit("Missing DISCORD_BOT_TOKEN or DISCORD_CHANNEL_ID")

DISCORD_CHANNEL_ID = int(DISCORD_CHANNEL_ID_RAW)

# Polymarket Gamma API
GAMMA = os.getenv("GAMMA_API_BASE", "https://gamma-api.polymarket.com").strip().rstrip("/")

# Timings
SCAN_INTERVAL_SEC = float(os.getenv("SCAN_INTERVAL_SEC", "20"))          # scan loop
UNIVERSE_REFRESH_SEC = float(os.getenv("UNIVERSE_REFRESH_SEC", "60"))    # refresh markets list
HEARTBEAT_MIN = float(os.getenv("HEARTBEAT_MIN", "180"))                 # "I'm alive" ping

# Universe size / pagination
PAGE_LIMIT = int(os.getenv("PAGE_LIMIT", "200"))
MAX_MARKETS = int(os.getenv("MAX_MARKETS", "1500"))

# Alerts & thresholds
# Arb definition: if (price_yes + price_no) < 1.0 by this margin -> alert
MIN_ARB_EDGE = float(os.getenv("MIN_ARB_EDGE", "0.02"))  # 0.02 = 2 cents edge
ALERT_COOLDOWN_MIN = float(os.getenv("ALERT_COOLDOWN_MIN", "45"))  # per-market per-type cooldown

# Error controls
ERROR_COOLDOWN_MIN = float(os.getenv("ERROR_COOLDOWN_MIN", "10"))  # don't spam same error repeatedly

# =========================
# DISCORD SETUP
# =========================
intents = discord.Intents.default()
client = discord.Client(intents=intents)

# =========================
# STATE
# =========================
_market_universe: List[Dict[str, Any]] = []
_last_universe_refresh: float = 0.0

_last_alert_at: Dict[str, float] = {}  # key: f"{kind}:{market_id}"
_last_error_at: float = 0.0
_last_heartbeat_at: float = 0.0

_http: Optional[httpx.AsyncClient] = None


# =========================
# HELPERS
# =========================
def now_ts() -> float:
    return time.time()

def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))

def _mk_alert_key(kind: str, market_id: str) -> str:
    return f"{kind}:{market_id}"

async def get_channel() -> discord.TextChannel:
    ch = client.get_channel(DISCORD_CHANNEL_ID)
    if ch is None:
        # Fallback to fetch (works even if not cached yet)
        ch = await client.fetch_channel(DISCORD_CHANNEL_ID)
    return ch  # type: ignore

async def send_discord(msg: str) -> None:
    ch = await get_channel()
    await ch.send(msg)

def should_cooldown(kind: str, market_id: str) -> bool:
    k = _mk_alert_key(kind, market_id)
    last = _last_alert_at.get(k, 0.0)
    return (now_ts() - last) < (ALERT_COOLDOWN_MIN * 60.0)

def mark_alert(kind: str, market_id: str) -> None:
    _last_alert_at[_mk_alert_key(kind, market_id)] = now_ts()

def parse_float(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str) and v.strip() == "":
            return None
        return float(v)
    except Exception:
        return None

async def http_get_json(url: str, params: Optional[Dict[str, Any]] = None) -> Any:
    """
    Robust GET with:
    - retries
    - exponential backoff + jitter
    - special handling for 429
    """
    assert _http is not None
    max_tries = 6
    base_delay = 0.7

    for attempt in range(1, max_tries + 1):
        try:
            r = await _http.get(url, params=params)
            if r.status_code == 429:
                retry_after = r.headers.get("Retry-After")
                if retry_after:
                    try:
                        wait_s = float(retry_after)
                    except Exception:
                        wait_s = 2.0
                else:
                    # exponential backoff + jitter
                    wait_s = base_delay * (2 ** (attempt - 1)) + random.random() * 0.5
                wait_s = clamp(wait_s, 1.0, 25.0)
                print(f"[rate-limit] 429 from {url} -> sleeping {wait_s:.1f}s")
                await asyncio.sleep(wait_s)
                continue

            r.raise_for_status()
            return r.json()

        except httpx.HTTPStatusError as e:
            # Other status codes
            code = e.response.status_code if e.response else None
            if attempt < max_tries:
                wait_s = base_delay * (2 ** (attempt - 1)) + random.random() * 0.5
                wait_s = clamp(wait_s, 0.8, 20.0)
                print(f"[http] status={code} attempt={attempt}/{max_tries} wait={wait_s:.1f}s url={url}")
                await asyncio.sleep(wait_s)
                continue
            raise

        except (httpx.TimeoutException, httpx.TransportError) as e:
            if attempt < max_tries:
                wait_s = base_delay * (2 ** (attempt - 1)) + random.random() * 0.5
                wait_s = clamp(wait_s, 0.8, 20.0)
                print(f"[http] transport/timeout attempt={attempt}/{max_tries} wait={wait_s:.1f}s url={url} err={e}")
                await asyncio.sleep(wait_s)
                continue
            raise


async def refresh_universe() -> None:
    """
    Pull active & open markets from Gamma.
    We keep it simple and safe: paginate until we hit MAX_MARKETS or no more results.
    """
    global _market_universe, _last_universe_refresh

    markets: List[Dict[str, Any]] = []
    offset = 0

    while len(markets) < MAX_MARKETS:
        params = {
            "active": "true",
            "closed": "false",
            "limit": str(PAGE_LIMIT),
            "offset": str(offset),
            # order by start time so universe isn't random; API usually supports these
            "order": "startTime",
            "ascending": "true",
        }
        data = await http_get_json(f"{GAMMA}/markets", params=params)

        if not isinstance(data, list) or len(data) == 0:
            break

        markets.extend([m for m in data if isinstance(m, dict)])

        if len(data) < PAGE_LIMIT:
            break

        offset += PAGE_LIMIT

    _market_universe = markets[:MAX_MARKETS]
    _last_universe_refresh = now_ts()
    print(f"[universe] markets={len(_market_universe)} refresh={UNIVERSE_REFRESH_SEC:.1f}s")


def extract_binary_prices(market: Dict[str, Any]) -> Optional[Tuple[float, float]]:
    """
    Best-effort: Gamma often includes outcomePrices for binary markets.
    We'll treat outcomePrices[0] as YES and [1] as NO when present.
    If not present, we skip.
    """
    outcome_prices = market.get("outcomePrices")

    if isinstance(outcome_prices, list) and len(outcome_prices) == 2:
        p_yes = parse_float(outcome_prices[0])
        p_no = parse_float(outcome_prices[1])
        if p_yes is None or p_no is None:
            return None
        # sanity clamp
        if not (0.0 <= p_yes <= 1.0 and 0.0 <= p_no <= 1.0):
            return None
        return (p_yes, p_no)

    return None


def market_label(m: Dict[str, Any]) -> str:
    title = m.get("question") or m.get("title") or "Unknown market"
    return str(title).strip()


def market_id(m: Dict[str, Any]) -> str:
    # Gamma markets usually have "id"
    mid = m.get("id") or m.get("marketId") or m.get("_id") or "unknown"
    return str(mid)


def market_url(m: Dict[str, Any]) -> str:
    # Gamma sometimes has "slug"
    slug = m.get("slug")
    if slug:
        return f"https://polymarket.com/market/{slug}"
    return "https://polymarket.com"


async def scan_once() -> Tuple[int, int]:
    """
    Scan the universe for simple, safe signals:
    - Binary "arb edge" using prices available on Gamma
      (not perfect orderbook arb, but stable & zero extra APIs)
    """
    checked = 0
    arbs_found = 0

    for m in _market_universe:
        prices = extract_binary_prices(m)
        if prices is None:
            continue

        checked += 1
        p_yes, p_no = prices
        cost = p_yes + p_no
        edge = 1.0 - cost

        if edge >= MIN_ARB_EDGE:
            mid = market_id(m)
            if should_cooldown("arb", mid):
                continue

            arbs_found += 1
            mark_alert("arb", mid)

            msg = (
                "🚨 **Arb-ish Alert (Gamma prices)**\n"
                f"**Market:** {market_label(m)}\n"
                f"YES={p_yes:.3f}  NO={p_no:.3f}  (sum={cost:.3f})\n"
                f"**Edge:** +{edge:.3f} (~{edge*100:.1f}¢ per $1)\n"
                f"{market_url(m)}\n\n"
                "_Note: This uses Gamma outcomePrices (not full orderbook). Confirm fill prices before sizing._"
            )
            await send_discord(msg)

    return checked, arbs_found


async def heartbeat_loop() -> None:
    global _last_heartbeat_at
    while True:
        try:
            if (now_ts() - _last_heartbeat_at) >= HEARTBEAT_MIN * 60.0:
                _last_heartbeat_at = now_ts()
                await send_discord(
                    f"✅ PolyBrain alive. Universe={len(_market_universe)} | "
                    f"scan_every={SCAN_INTERVAL_SEC:.0f}s | refresh_every={UNIVERSE_REFRESH_SEC:.0f}s"
                )
        except Exception as e:
            print(f"[heartbeat] error: {e}")
        await asyncio.sleep(30)


async def universe_loop() -> None:
    while True:
        try:
            # refresh if never refreshed OR stale
            if (now_ts() - _last_universe_refresh) >= UNIVERSE_REFRESH_SEC:
                await refresh_universe()
        except Exception as e:
            print(f"[universe] failed: {e}")
            await report_error("universe_loop", e)
        await asyncio.sleep(1.0)


async def scan_loop() -> None:
    while True:
        t0 = now_ts()
        try:
            checked, arbs = await scan_once()
            print(f"[scan] checked={checked} arbs_found={arbs}")
        except Exception as e:
            print(f"[scan] failed: {e}")
            await report_error("scan_loop", e)

        # keep interval stable even if scan takes time
        dt = now_ts() - t0
        sleep_for = max(1.0, SCAN_INTERVAL_SEC - dt)
        await asyncio.sleep(sleep_for)


async def report_error(where: str, e: Exception) -> None:
    """
    Send 1 clean Discord error message with cooldown (so no spam).
    """
    global _last_error_at
    if (now_ts() - _last_error_at) < (ERROR_COOLDOWN_MIN * 60.0):
        return
    _last_error_at = now_ts()

    tb = traceback.format_exc()
    # Keep it Discord-friendly (not huge)
    tb_short = tb[-1500:] if len(tb) > 1500 else tb

    msg = (
        f"⚠️ **PolyBrain error** in `{where}`\n"
        f"**{type(e).__name__}:** {str(e)}\n"
        "```text\n"
        f"{tb_short}\n"
        "```"
    )
    try:
        await send_discord(msg)
    except Exception as send_err:
        print(f"[error-report] failed to send to Discord: {send_err}")


# =========================
# DISCORD EVENTS
# =========================
@client.event
async def on_ready():
    global _http
    print(f"Logged in as: {client.user}")

    # HTTP client (one session)
    _http = httpx.AsyncClient(
        timeout=httpx.Timeout(20.0),
        headers={"User-Agent": "polybrain-alerts/1.0"},
    )

    # Initial universe load + startup ping
    await refresh_universe()
    await send_discord("✅ PolyBrain deployed & running. (Safety Pack enabled)")

    # Start loops
    asyncio.create_task(universe_loop())
    asyncio.create_task(scan_loop())
    asyncio.create_task(heartbeat_loop())


def main():
    client.run(DISCORD_BOT_TOKEN)


if __name__ == "__main__":
    main()
