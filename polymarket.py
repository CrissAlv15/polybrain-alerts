import httpx

POLY_BASE = "https://gamma-api.polymarket.com"

async def list_active_markets(client: httpx.AsyncClient, limit: int = 200, offset: int = 0):
    # Gamma list markets endpoint (public)
    # docs: Polymarket "List markets"
    r = await client.get(
        f"{POLY_BASE}/markets",
        params={
            "active": "true",
            "closed": "false",
            "limit": limit,
            "offset": offset,
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json()

def extract_sports_moneyline_markets(markets_json):
    """
    We only want sports-y head-to-head markets for MVP:
    - 2 outcomes
    - has prices we can treat like win probabilities
    """
    out = []
    for m in markets_json:
        outcomes = m.get("outcomes") or []
        if len(outcomes) != 2:
            continue

        # Try to keep sports-only-ish (Gamma tags/categories vary)
        # If you want ALL markets, delete this filter.
        category = (m.get("category") or "").lower()
        if "sports" not in category:
            continue

        # Prices (best effort: sometimes "outcomePrices" exists)
        prices = m.get("outcomePrices") or m.get("outcome_prices") or None
        if not prices or len(prices) != 2:
            continue

        try:
            p0 = float(prices[0])
            p1 = float(prices[1])
        except:
            continue

        # basic sanity
        if p0 <= 0 or p1 <= 0 or p0 >= 1.01 or p1 >= 1.01:
            continue

        out.append({
            "id": m.get("id"),
            "question": m.get("question"),
            "category": m.get("category"),
            "outcomes": outcomes,
            "prices": [p0, p1],
            "url": m.get("slug") and f"https://polymarket.com/market/{m['slug']}",
        })
    return out
