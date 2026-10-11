from datetime import date


def xirr(flows: list[tuple[date, float]]) -> float | None:
    flows = sorted(flows)
    if len(flows) < 2 or not (any(a < 0 for _, a in flows) and any(a > 0 for _, a in flows)):
        return None
    t0 = flows[0][0]

    def npv(rate: float) -> float:
        return sum(a / (1 + rate) ** ((d - t0).days / 365) for d, a in flows)

    lo, hi = -0.9999, 10.0
    flo, fhi = npv(lo), npv(hi)
    if flo * fhi > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        fm = npv(mid)
        if flo * fm <= 0:
            hi = mid
        else:
            lo, flo = mid, fm
    return (lo + hi) / 2
