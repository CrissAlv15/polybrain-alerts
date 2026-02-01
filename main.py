import os
import time
import asyncio
import re
from typing import List, Dict, Any, Tuple, Optional

import httpx

from discord_alerts import client, send
from polymarket import list_active_markets, extract_sports_h2h_markets
from odds import get_h2h_odds_events, consensus_probs_from_event
from signals import detect_value, detect_arb_candidate

# =========================
# Config (Railway Variables)
# =========================
SCAN_SECONDS = int(os.getenv("SCAN_SECONDS", "30"))
COOLDOWN_MINUTES = int(os.getenv("COOLDOWN_MINUTES", "60"))
VALUE_THRESHOLD = float(os.getenv("VALUE_THRESHOLD", "0.07"))

# Odds sports keys (both NHL + NBA by default)
SPORT_KEYS = os.getenv("SPORT_KEYS", "icehockey_nhl,basketball_nba").split(",")

# Limit how many Polymarket markets we page through (keep it safe for free tiers)
POLY_PAGES = int(os.getenv("POLY_PAGES", "3"))  # each page = 200 markets

# Matching strictness
MATCH_SCORE_MIN = float(os.getenv("MATCH_SCORE_MIN", "0.20"))

# =========================
# State
# =========================
last_alert_at: Dict[str, float] = {}  # key -> epoch

def can_alert(key: str) -> bool:
    now = time.time()
    last = last_alert_at.get(key, 0.0)
    return (now - last) >= (COOLDOWN_MINUTES * 60)

def mark_alert(key: str) -> None:
    last_alert_at[key] = time.time()

# =========================
# Matching helpers
# =========================
def norm(s: str) -> str:
    s = (s or "").lower()
    s = s.replace("&", " and ")
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def token_set(s: str) -> set:
    return set(norm(s).split())

def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)

# Practical alias starter pack (you can expand over time)
ALIASES = {
    # NHL
    "new jersey devils": ["nj devils", "devils", "new jersey"],
    "new york rangers": ["ny rangers", "rangers"],
    "new york islanders": ["ny islanders", "islanders"],
    "tampa bay lightning": ["lightning", "tampa bay"],
    "vegas golden knights": ["vegas", "golden knights", "knights"],
    "washington capitals": ["capitals", "caps", "washington"],
    "toronto maple leafs": ["maple leafs", "leafs", "toronto"],
    "boston bruins": ["bruins", "boston"],
    "montreal canadiens": ["canadiens", "habs", "montreal"],
    "carolina hurricanes": ["hurricanes", "canes", "carolina"],
    "florida panthers": ["panthers", "florida"],
    "edmonton oilers": ["oilers", "edmonton"],
    "calgary flames": ["flames", "calgary"],

    # NBA
    "los angeles lakers": ["la lakers", "lakers"],
    "los angeles clippers": ["la clippers", "clippers"],
    "golden state warriors": ["warriors", "gsw", "golden state"],
    "new york knicks": ["knicks"],
    "brooklyn nets": ["nets"],
    "boston celtics": ["celtics", "boston"],
    "miami heat": ["heat", "miami"],
    "milwaukee bucks": ["bucks"],
    "denver nuggets": ["nuggets"],
    "phoenix suns": ["suns"],
    "dallas mavericks": ["mavs", "mavericks", "dallas"],
    "minnesota timberwolves": ["wolves", "timberwolves", "minnesota"],
}

def name_variants(team_name: str) -> List[str]:
    t = norm(team_name)
    out = {t}
    for canon, alts in ALIASES.items():
        if norm(canon) == t:
            out.update(norm(x) for x in alts)
    return sorted(out)

def best_event_match(polymarket_text: str, events: List[Dict[str, Any]]) -> Tuple[Optional[Dict[str, Any]], float]:
    q_tokens = token_set(polymarket_text)
    best = None
    best_score = 0.0

    for ev in events:
        home = ev.get("home_team") or ""
        away = ev.get("away_team") or ""
        home_vars = name_variants(home)
        away_vars = name_variants(away)

        home_best = max((jaccard(q_tokens, token_set(v)) for v in home_vars), default=0.0)
        away_best = max((jaccard(q_tokens, token_set(v)) for v in away_vars), default=0.0)

        # Need both teams to appear somewhat
        score = (home_best + away_best) / 2.0

        if score > best_score:
            best_score = score
            best = ev

    return best, best_score

def outcome_mentions(team: str, outcome_text: str) -> bool:
    o = norm(outcome_text)
    for v in name_variants(team):
        if v and v in o:
            return True
    return False

# =========================
# Main scanning logic
# =========================
async def load_polymarket_sports_markets(http: httpx.AsyncClient) -> List[Dict[str, Any]]:
    poly_all = []
    for i in range(POLY_PAGES):
        raw = await list_active_markets(http, limit=200, offset=i * 200)
        poly_all.extend(raw)
    return extract_sports_h2h_markets(poly_all)

async def load_odds_events(http: httpx.AsyncClient) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    for sk in [x.strip() for x in SPORT_KEYS if x.strip()]:
        try:
            data = await get_h2h_odds_events(http, sk)
            for ev in data:
                c = consensus_probs_from_event(ev)
                if c.get("home_p") and c.get("away_p"):
                    events.append(c)
        except Exception as e:
            print(f"[odds] sport={sk} error={repr(e)}")
    return events

def map_polymarket_side_to_fair(market: Dict[str, Any], ev: Dict[str, Any]) -> Optional[Tuple[str, float, float]]:
    """
    Returns (side_name, poly_price, fair_prob) for outcome[0] only.
    (We use outcome[0] as the "YES" side in Polymarket-style pricing.)
    """
    q = market.get("question") or ""
    outcomes = market.get("outcomes") or []
    prices = market.get("prices") or []
    if len(outcomes) != 2 or len(prices) != 2:
        return None

    home = ev.get("home_team") or ""
    away = ev.get("away_team") or ""
    home_p = ev.get("home_p")
    away_p = ev.get("away_p")
    if home_p is None or away_p is None:
        return None

    out0 = outcomes[0] or ""
    poly_p = float(prices[0])

    # Determine which team outcome[0] refers to
    if outcome_mentions(home, out0) or outcome_mentions(home, q):
        return (home, poly_p, float(home_p))
    if outcome_mentions(away, out0) or outcome_mentions(away, q):
        return (away, poly_p, float(away_p))

    return None

async def scan_once() -> None:
    async with httpx.AsyncClient() as http:
        poly_markets = await load_polymarket_sports_markets(http)
        events = await load_odds_events(http)

        checked = 0
        matched = 0
        values = 0
        arbs = 0

        for m in poly_markets:
            checked += 1
            q = m.get("question") or ""
            match, score = best_event_match(q, events)

            if not match or score < MATCH_SCORE_MIN:
                continue

            mapped = map_polymarket_side_to_fair(m, match)
            if not mapped:
                continue

            matched += 1
            side, poly_p, fair_p = mapped
            market_id = m.get("id")
            key_base = f"{market_id}:{norm(side)}"

            # VALUE
            v = detect_value(poly_p=poly_p, fair_p=fair_p, threshold=VALUE_THRESHOLD)
            if v and can_alert(key_base):
                values += 1
                mark_alert(key_base)

                tp1 = int(v["tp1"] * 100)
                tp2 = int(v["tp2"] * 100)

                msg = (
                    f"📣 **VALUE ALERT** — **{side}**\n"
                    f"• Market: {q}\n"
                    f"• Polymarket: **{int(poly_p*100)}¢** (implied {int(poly_p*100)}%)\n"
                    f"• Sportsbook fair: **{int(fair_p*100)}%**\n"
                    f"• Edge: **+{v['edge']*100:.1f}%**\n"
                    f"• Match score: **{score:.2f}**\n\n"
                    f"**Plan**\n"
                    f"• Entry: buy ≤ **{int(poly_p*100)}¢** (or better)\n"
                    f"• Sell 50%: **{tp1}¢** (or when edge < +{int(VALUE_THRESHOLD*100)}%)\n"
                    f"• Sell rest: **{tp2}¢** (or near game-time risk)\n"
                    f"• Risk note: if major lineup/injury news flips odds, exit.\n"
                    + (f"\n{m['url']}" if m.get("url") else "")
                )
                await send(msg)

            # ARB candidate (only if no value)
            if not v:
                a = detect_arb_candidate(poly_p=poly_p, fair_p=fair_p, threshold=0.10)
                if a and can_alert(key_base + ":arb"):
                    arbs += 1
                    mark_alert(key_base + ":arb")
                    msg = (
                        f"⚡ **ARB CANDIDATE** — {side}\n"
                        f"• Market: {q}\n"
                        f"• Polymarket: **{int(poly_p*100)}¢** vs fair **{int(fair_p*100)}%**\n"
                        f"• Off by: **{a['diff']*100:.1f}%**\n"
                        f"• Action: verify your sportsbook line still exists before taking.\n"
                        f"• Match score: **{score:.2f}**\n"
                        + (f"\n{m['url']}" if m.get("url") else "")
                    )
                    await send(msg)

        print(f"[scan] checked={checked} matched={matched} value_found={values} arbs_found={arbs}")

@client.event
async def on_ready():
    print(f"Logged in as: {client.user}")
    await send("✅ PolyBrain is live. Scanning NHL+NBA (odds-based value + arb candidates).")
    while True:
        try:
            await scan_once()
        except Exception as e:
            print(f"[fatal] {repr(e)}")
        await asyncio.sleep(SCAN_SECONDS)

if __name__ == "__main__":
    client.run(os.getenv("DISCORD_BOT_TOKEN"))
