def clamp(x, lo, hi):
    return max(lo, min(hi, x))

def fair_value_band(fair_p: float):
    """
    Converts fair probability into a suggested sell ladder.
    Example:
      if fair is 0.55, then:
        sell 50% when market hits ~0.60
        sell rest when market hits ~0.65
    """
    tp1 = clamp(fair_p + 0.05, 0.02, 0.98)
    tp2 = clamp(fair_p + 0.10, 0.02, 0.98)
    return tp1, tp2

def detect_value(poly_p: float, fair_p: float, threshold: float = 0.07):
    """
    If Polymarket is under fair by threshold => value buy signal.
    """
    edge = fair_p - poly_p
    if edge >= threshold:
        tp1, tp2 = fair_value_band(fair_p)
        return {
            "type": "VALUE",
            "edge": edge,
            "tp1": tp1,
            "tp2": tp2,
        }
    return None

def detect_simple_arb(poly_p: float, fair_p: float, threshold: float = 0.03):
    """
    Simplified arb heuristic:
    If Polymarket is WAY off vs a strong fair prob, you can sometimes hedge elsewhere.
    True arb depends on the exact books/lines you can access—so we alert it as “arb candidate”.
    """
    diff = abs(poly_p - fair_p)
    if diff >= threshold:
        return {
            "type": "ARB_CANDIDATE",
            "diff": diff,
        }
    return None
