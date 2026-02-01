import os
import httpx

ODDS_API_KEY = os.getenv("ODDS_API_KEY")
if not ODDS_API_KEY:
    raise SystemExit("Missing ODDS_API_KEY")

ODDS_BASE = "https://api.the-odds-api.com/v4"

def american_to_prob(american: float) -> float:
    if american < 0:
        return (-american) / ((-american) + 100.0)
    return 100.0 / (american + 100.0)

async def get_h2h_odds_events(client: httpx.AsyncClient, sport_key: str):
    r = await client.get(
        f"{ODDS_BASE}/sports/{sport_key}/odds",
        params={
            "apiKey": ODDS_API_KEY,
            "regions": "us",
            "markets": "h2h",
            "oddsFormat": "american",
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json()

def median(xs):
    xs = sorted(xs)
    n = len(xs)
    if n == 0:
        return None
    mid = n // 2
    return xs[mid] if n % 2 == 1 else (xs[mid - 1] + xs[mid]) / 2

def consensus_probs_from_event(event):
    """
    Return median implied probs across books (home and away).
    """
    home = event.get("home_team")
    away = event.get("away_team")
    books = event.get("bookmakers") or []

    home_probs, away_probs = [], []
    for b in books:
        markets = b.get("markets") or []
        for m in markets:
            if m.get("key") != "h2h":
                continue
            for o in (m.get("outcomes") or []):
                name = o.get("name")
                price = o.get("price")
                if price is None:
                    continue
                p = american_to_prob(float(price))
                if name == home:
                    home_probs.append(p)
                elif name == away:
                    away_probs.append(p)

    return {
        "home_team": home,
        "away_team": away,
        "home_p": median(home_probs),
        "away_p": median(away_probs),
        "commence_time": event.get("commence_time"),
    }
