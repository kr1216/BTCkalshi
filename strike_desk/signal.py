"""Turn live market data into a call: take UP, take DOWN, or wait."""
from __future__ import annotations

from dataclasses import dataclass, field

from .feeds import Quote
from .model import CYCLE_S, PROFILES, SETTLE_S, blend, ev, fair_up, tradable, vol_per_sec

KALSHI_STALE_S = 10   # a Kalshi quote older than this is not used
SPOT_STALE_S = 15     # a spot print older than this fails the freshness check


@dataclass
class Settings:
    min_edge: float    # dollars per contract, e.g. 0.03
    max_price: float   # dollars, e.g. 0.92
    vol_mult: float

    @classmethod
    def default(cls, asset: str) -> "Settings":
        pr = PROFILES[asset]
        return cls(pr["edge"] / 100, 0.92, pr["vol"])


@dataclass
class Overrides:
    strike: float | None = None
    yes: float | None = None   # dollars
    no: float | None = None


@dataclass
class Call:
    asset: str
    now: float
    close: float
    s_left: float
    action: str                      # "UP", "DOWN", "LEAN UP", "LEAN DOWN", "WAIT", "NO DATA"
    why: str
    side: str | None = None          # "YES" / "NO" when action is UP / DOWN
    price: float | None = None       # ask to pay for the call
    edge: float | None = None        # best EV after fee, dollars per contract
    p_up: float | None = None        # blended fair chance of Up
    p_model: float | None = None     # model alone
    sd: float | None = None
    vol: float | None = None
    spot: float | None = None
    spot_age: float | None = None
    strike: float | None = None
    strike_src: str = "missing"
    p_yes: float | None = None
    p_no: float | None = None
    ev_yes: float | None = None
    ev_no: float | None = None
    ticker: str | None = None
    quote_age: float | None = None
    checks: list[tuple[str, bool | None, str]] = field(default_factory=list)

    @property
    def is_signal(self) -> bool:
        return self.action in ("UP", "DOWN")


def _c(p: float) -> str:
    return f"{p * 100:.1f}¢"


def _signed(x: float) -> str:
    return ("+" if x >= 0 else "−") + f"{abs(x) * 100:.1f}¢"


def evaluate(asset: str, now: float, quote: Quote | None, spot: tuple[float, float] | None,
             candles: list[tuple[float, float, float]] | None, final_prints: list[float],
             settings: Settings, over: Overrides | None = None) -> Call:
    """Work out the call for one market at time `now` (unix seconds).

    `candles` are 1-minute (start, open, close); `final_prints` are spot prints seen
    since the start of the final minute (ignored before it).
    """
    over = over or Overrides()
    close = quote.close if quote else (now // CYCLE_S + 1) * CYCLE_S
    s_left = close - now
    c = Call(asset, now, close, s_left, "NO DATA", "", ticker=quote.ticker if quote else None)

    sp, sp_ts = spot if spot else (None, None)
    c.spot = sp
    c.spot_age = max(now - sp_ts, 0.0) if sp_ts else None
    spot_fresh = c.spot_age is not None and c.spot_age <= SPOT_STALE_S

    candles = candles or []
    closed = [x for x in candles if x[0] + 60 <= now]
    vol0 = vol_per_sec([x[2] for x in closed])
    c.vol = vol0 * settings.vol_mult if vol0 is not None else None

    c.quote_age = max(now - quote.fetched, 0.0) if quote else None
    q_fresh = c.quote_age is not None and c.quote_age <= KALSHI_STALE_S
    est = next((x[1] for x in candles if x[0] == close - CYCLE_S), None)
    if over.strike is not None:
        c.strike, c.strike_src = over.strike, "override"
    elif quote and quote.target is not None:
        c.strike, c.strike_src = quote.target, "Kalshi"
    elif est is not None:
        c.strike, c.strike_src = est, "estimate"

    k_yes = quote.yes_ask if quote and q_fresh and tradable(quote.yes_ask) else None
    k_no = quote.no_ask if quote and q_fresh and tradable(quote.no_ask) else None
    c.p_yes = over.yes if over.yes is not None else k_yes
    c.p_no = over.no if over.no is not None else k_no
    both = tradable(c.p_yes) and tradable(c.p_no)

    obs_avg = None
    if s_left <= SETTLE_S:
        if len(final_prints) >= 5:
            obs_avg = sum(final_prints) / len(final_prints)
        else:
            last = next((x for x in candles if x[0] == close - 60), None)
            obs_avg = (last[1] + last[2]) / 2 if last else None

    ok = sp is not None and c.strike is not None and c.vol is not None
    if ok:
        fm = fair_up(sp, c.strike, s_left, c.vol, obs_avg)
        c.p_model, c.sd = fm.p, fm.sd
        c.p_up = blend(fm.p, (c.p_yes + 1 - c.p_no) / 2, PROFILES[asset]["blend"]) if both else fm.p
        c.ev_yes = ev(c.p_up, c.p_yes) if tradable(c.p_yes) else None
        c.ev_no = ev(1 - c.p_up, c.p_no) if tradable(c.p_no) else None
    cands = [(s, e, p) for s, e, p in (("YES", c.ev_yes, c.p_yes), ("NO", c.ev_no, c.p_no))
             if e is not None and p <= settings.max_price]
    best = max(cands, key=lambda x: x[1]) if cands else None
    evs = [e for e in (c.ev_yes, c.ev_no) if e is not None]
    c.edge = best[1] if best else (max(evs) if evs else None)

    c.checks = [
        ("Spot price fresh", None if sp is None else spot_fresh, f"{c.spot_age:.0f}s old" if c.spot_age is not None else "—"),
        ("Volatility measured", None if not candles else vol0 is not None, f"{len(closed)} candles" if vol0 is not None else "needs 11+"),
        ("Target set", c.strike is not None, c.strike_src),
        ("Both Kalshi prices", both, f"Y {_c(c.p_yes)} / N {_c(c.p_no)}" if both else "quote too old" if quote and not q_fresh else "none"),
        ("Inside trading window", 20 <= s_left <= 780, "first 2 min" if s_left > 780 else "last 20 s" if s_left < 20 else "open"),
        ("Price at or below max", True if best else (None if not evs else False), f"≤ {_c(settings.max_price)}"),
        ("Edge clears minimum", None if not best else best[1] >= settings.min_edge, _signed(best[1]) if best else "—"),
    ]

    if not ok:
        c.action = "NO DATA"
        c.why = ("No spot price" if sp is None else "Measuring volatility" if c.vol is None else "No target price")
    elif not 20 <= s_left <= 780:
        c.action = "WAIT"
        c.why = "Too early: first 2 min" if s_left > 780 else "Too late: last 20 s"
    elif not both and quote and q_fresh and max(quote.yes_ask or 0, quote.no_ask or 0) >= 0.99:
        c.action = "WAIT"
        c.why = "Market is all but decided"
    elif not both:
        c.action = "LEAN UP" if c.p_up >= 0.5 else "LEAN DOWN"
        c.why = "Waiting for both Kalshi prices"
    elif all(v is True for _, v, _ in c.checks):
        c.side, c.edge, c.price = best
        c.action = "UP" if c.side == "YES" else "DOWN"
        fair = c.p_up if c.side == "YES" else 1 - c.p_up
        c.why = f"Fair {fair * 100:.1f}% vs {_c(c.price)} ask"
    else:
        fail = next(t for t, v, _ in c.checks if v is not True)
        c.action = "WAIT"
        c.why = {"Edge clears minimum": "The price already reflects the odds",
                 "Price at or below max": "Too expensive: the upside is too small",
                 "Inside trading window": "Too early" if s_left > 780 else "Too late"}.get(fail, fail + " ✕")
    return c
