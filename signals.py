def clamp(x, lo, hi):
    return max(lo, min(hi, x))

def fair_value_band(fair_p: float):
    tp1 = clamp(fair_p + 0.05, 0.02, 0.98)
    tp2 = clamp(fair_p + 0.10, 0.02, 0.98)
    return tp1, tp2

def detect_value(poly_p: float, fair_p: float, threshold: float = 0.07):
    edge = fair_p - poly_p
    if edge >= threshold:
        tp1, tp2 = fair_value_band(fair_p)
        return {"type": "VALUE", "edge": edge, "tp1": tp1, "tp2": tp2}
    return None

def detect_arb_candidate(poly_p: float, fair_p: float, threshold: float = 0.10):
    diff = abs(poly_p - fair_p)
    if diff >= threshold:
        return {"type": "ARB_CANDIDATE", "diff": diff}
    return None
