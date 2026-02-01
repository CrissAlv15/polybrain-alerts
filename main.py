import os
import asyncio
import time
import httpx

from discord_alerts import client, send
from polymarket import list_active_markets, extract_sports_moneyline_markets
from odds import get_h2h_probs, consensus_probs_from_event
from signals import detect_value, detect_simple_arb

# ===== Config =====
SCAN_SECONDS = int(os.getenv("SCAN_SECONDS", "30"))

# Stay cheap on free tier: only check a few sports keys.
# Add more later when you upgrade.
SPORT_KEYS = os.getenv("SPORT_KEYS", "icehockey_nhl,basketball_nba").split(",")

# Avoid repeat spam
COOLDOWN_MINUTES = int(os.getenv("COOLDOWN_MINUTES", "60"))

# value threshold (0.07 = 7% misprice)
VALUE_THRESHOLD = float(os.getenv("VALUE_THRESHOLD", "0.07"))

# ===== state =====
last_alert_at = {}  # key -> epoch seconds

def can_alert(key: str) -> bool:
    now = time.time()
    last = last_alert_at.get(key, 0)
    return (now - last) >= (COOLDOWN_MINUTES * 60)

def mark_alert(key: str):
    last_alert_at[key] = time.time()

def best_match_market_to_event(question: str, event: dict):
    """
    MVP matching: naive string contains checks.
    Later we can do better mapping.
    """
    q = (question or "").lower()
    home = (event["home_team"] or "").lower()
    away = (event["away_team"] or "").lower()
    return (home in q) and (away in q)

async def scan_loop():
    async with httpx.AsyncClient() as http:
        # 1) Pull Polymarket sports H2H markets (paged a bit)
        poly_all = []
        for off in (0, 200, 400):
            raw = await list_active_markets(http, limit=200, offset=off)
            poly_all.extend(raw)

        poly_markets = extract_sports_moneyline_markets(poly_all)

        # 2) Pull sportsbook consensus odds (limited sports)
        events = []
        for sk in SPORT_KEYS:
            try:
                data = await get_h2h_probs(http, sk.strip())
                for ev in data:
                    c = consensus_probs_from_event(ev)
                    if c["home_p"] and c["away_p"]:
                        events.append(c)
            except Exception as e:
                print(f"[odds] sport={sk} error={e}")

        checked = 0
        arbs = 0
        values = 0

        # 3) Match + generate signals
        for m in poly_markets:
            checked += 1
            q = m["question"]
            p_yes = m["prices"][0]  # outcome 0 price
            outcomes = m["outcomes"]

            # Only do a rough match
            match = None
            for ev in events:
                if best_match_market_to_event(q, ev):
                    match = ev
                    break
            if not match:
                continue

            # Map outcome0 -> home or away (MVP guess: if outcome name contains home team)
            home = match["home_team"]
            away = match["away_team"]
            out0 = (outcomes[0] or "").lower()

            if home.lower() in out0:
                fair_p = match["home_p"]
                side = home
                poly_p = p_yes
            elif away.lower() in out0:
                fair_p = match["away_p"]
                side = away
                poly_p = p_yes
            else:
                continue

            key = f"{m['id']}:{side}"

            # VALUE alert
            v = detect_value(poly_p=poly_p, fair_p=fair_p, threshold=VALUE_THRESHOLD)
            if v and can_alert(key):
                values += 1
                mark_alert(key)

                tp1 = int(v["tp1"] * 100)
                tp2 = int(v["tp2"] * 100)

                msg = (
                    f"📣 **VALUE** on **{side}**\n"
                    f"• Market: {q}\n"
                    f"• Polymarket price: **{int(poly_p*100)}¢**\n"
                    f"• Sportsbook fair: **{int(fair_p*100)}%**\n"
                    f"• Edge: **{v['edge']*100:.1f}%**\n\n"
                    f"**Sell plan**\n"
                    f"• Sell 50% at **{tp1}¢**\n"
                    f"• Sell rest at **{tp2}¢**\n"
                    + (f"\n{m['url']}" if m.get("url") else "")
                )
                await send(msg)

            # ARB candidate alert (only if no VALUE fired)
            if not v:
                a = detect_simple_arb(poly_p=poly_p, fair_p=fair_p, threshold=0.10)
                if a and can_alert(key + ":arb"):
                    arbs += 1
                    mark_alert(key + ":arb")
                    msg = (
                        f"⚡ **ARB CANDIDATE** ({a['diff']*100:.1f}% off fair)\n"
                        f"• {side}\n"
                        f"• Market: {q}\n"
                        f"• Polymarket: **{int(poly_p*100)}¢** vs fair **{int(fair_p*100)}%**\n"
                        f"• Check if your sportsbook lines still match before taking.\n"
                        + (f"\n{m['url']}" if m.get("url") else "")
                    )
                    await send(msg)

        print(f"[scan] checked={checked} arbs_found={arbs} value_found={values}")

@client.event
async def on_ready():
    print(f"Logged in as: {client.user}")
    await send("✅ PolyBrain is live on Railway. Scanning started.")
    while True:
        try:
            await scan_loop()
        except Exception as e:
            print(f"[fatal] {e}")
        await asyncio.sleep(SCAN_SECONDS)

if __name__ == "__main__":
    client.run(os.getenv("DISCORD_BOT_TOKEN"))
