"""Fair-odds model for Kalshi 15-minute crypto markets.

Ported from the Strike Desk artifact. Kalshi settles on the average of 60
one-second CF Benchmarks prints in the final minute; price is modelled as a
driftless random walk with volatility from recent 1-minute candles.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

CYCLE_S = 900
SETTLE_S = 60

# Per-asset defaults from the Kalshi backtest, 2026-09-15 to 09-25 (see the Strike Desk artifact).
# vol: volatility multiplier; edge: minimum edge in cents; tier: how far to trust the Crypto.com feed;
# verdict: "edge" made money on held-out markets, "weak" did too but within noise, "none" did not.
# blend: (a, b, c) in logit p = a*logit(model) + b*logit(Kalshi mid) + c.
PROFILES = {
    "SOL":  dict(vol=0.9,  edge=3, tier="core", verdict="edge", blend=(0.649, 0.375, 0.012)),
    "NEAR": dict(vol=1.0,  edge=3, tier="thin", verdict="weak", blend=(0.614, 0.437, 0.109)),
    "DOGE": dict(vol=0.9,  edge=3, tier="thin", verdict="none", blend=(0.464, 0.605, 0.057)),
    "XRP":  dict(vol=0.9,  edge=3, tier="core", verdict="none", blend=(0.473, 0.598, 0.067)),
    "ETH":  dict(vol=0.9,  edge=3, tier="core", verdict="none", blend=(0.189, 0.866, 0.002)),
    "BTC":  dict(vol=1.15, edge=3, tier="core", verdict="none", blend=(0.125, 0.935, -0.025)),
}


def ncdf(z: float) -> float:
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


@dataclass
class Fair:
    p: float      # chance the settlement average finishes at or above the target
    sd: float     # 1-sigma of the settlement average, in price units
    mean: float


def fair_up(spot: float, strike: float, s_left: float, vol_sec: float, obs_avg: float | None = None) -> Fair:
    t = max(s_left, 0.0)
    if t > SETTLE_S:
        mean, var = spot, vol_sec ** 2 * ((t - SETTLE_S) + SETTLE_S / 3)
    else:
        r, n = t, SETTLE_S - t
        oa = spot if obs_avg is None else obs_avg
        mean = (n * oa + r * spot) / SETTLE_S
        var = (r / SETTLE_S) ** 2 * vol_sec ** 2 * r / 3
    sd = math.sqrt(var)
    if sd == 0:
        return Fair(1.0 if mean >= strike else 0.0, 0.0, mean)
    return Fair(ncdf((mean - strike) / sd), sd, mean)


def fee(p: float) -> float:
    """Kalshi taker fee per contract: 7% x p x (1-p), rounded up to the cent."""
    return math.ceil(round(0.07 * p * (1 - p) * 1e6) / 1e4) / 100


def ev(p_win: float, price: float) -> float:
    return p_win - price - fee(price)


def logit(p: float) -> float:
    p = min(max(p, 1e-4), 1 - 1e-4)
    return math.log(p / (1 - p))


def blend(p_model: float, p_kalshi: float, coef: tuple[float, float, float]) -> float:
    a, b, c = coef
    return 1 / (1 + math.exp(-(a * logit(p_model) + b * logit(p_kalshi) + c)))


def vol_per_sec(closes: list[float]) -> float | None:
    """Per-second volatility from up to the last 31 closed 1-minute candle closes."""
    c = closes[-31:]
    if len(c) < 11:
        return None
    sq = sum((c[i] - c[i - 1]) ** 2 for i in range(1, len(c)))
    return math.sqrt(sq / (len(c) - 1) / 60)


def tradable(p: float | None) -> bool:
    return p is not None and 0 < p < 1
