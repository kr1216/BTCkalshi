"""Live data: Kalshi 15-minute markets and Crypto.com spot/candles (public, no auth)."""
from __future__ import annotations

import json
import threading
import time
import urllib.parse
import urllib.request
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
CDC = "https://api.crypto.com/exchange/v1/public"
UA = {"User-Agent": "strike-desk/1.0"}


def _get(url: str, params: dict | None = None, timeout: float = 6) -> dict:
    if params:
        url += "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return json.load(r)


def _ts(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def _num(v) -> float | None:
    return None if v in (None, "") else float(v)


@dataclass
class Quote:
    ticker: str
    open: float           # unix seconds
    close: float
    target: float | None
    yes_bid: float | None
    yes_ask: float | None
    no_bid: float | None
    no_ask: float | None
    fetched: float


def kalshi_quote(asset: str) -> Quote | None:
    """The currently active KX<ASSET>15M market, or None between markets."""
    ms = _get(f"{KALSHI}/markets", {"series_ticker": f"KX{asset}15M", "status": "open", "limit": 10})["markets"]
    act = sorted((m for m in ms if m.get("status") == "active"), key=lambda m: m["close_time"])
    if not act:
        return None
    m = act[0]
    return Quote(m["ticker"], _ts(m["open_time"]), _ts(m["close_time"]), _num(m.get("floor_strike")),
                 _num(m.get("yes_bid_dollars")), _num(m.get("yes_ask_dollars")),
                 _num(m.get("no_bid_dollars")), _num(m.get("no_ask_dollars")), time.time())


def kalshi_settled(asset: str, limit: int = 40) -> dict[float, dict]:
    """Recent results keyed by close time (unix seconds): {'result': 1|0, 'value': settle price}."""
    ms = _get(f"{KALSHI}/markets", {"series_ticker": f"KX{asset}15M", "status": "settled", "limit": limit})["markets"]
    return {_ts(m["close_time"]): {"ticker": m["ticker"], "result": 1 if m["result"] == "yes" else 0,
                                   "value": _num(m.get("expiration_value"))}
            for m in ms if m.get("result") in ("yes", "no")}


def spot(asset: str) -> tuple[float, float]:
    """(last trade price, exchange timestamp in unix seconds)."""
    d = _get(f"{CDC}/get-tickers", {"instrument_name": f"{asset}_USD"})["result"]["data"][0]
    return float(d["a"]), d["t"] / 1000


def candles(asset: str, count: int = 40) -> list[tuple[float, float, float]]:
    """1-minute candles as (start unix seconds, open, close), oldest first."""
    d = _get(f"{CDC}/get-candlestick", {"instrument_name": f"{asset}_USD", "timeframe": "1m", "count": count})
    return sorted((c["t"] / 1000, float(c["o"]), float(c["c"])) for c in d["result"]["data"])


# Spot prints seen by this server, shared by every session, so the final minute's
# average can be built from real samples rather than guessed from one candle.
_prints: dict[str, deque] = defaultdict(lambda: deque(maxlen=600))
_lock = threading.Lock()


def record_print(asset: str, t: float, price: float) -> None:
    with _lock:
        q = _prints[asset]
        if not q or t > q[-1][0]:
            q.append((t, price))


def prints_between(asset: str, start: float, end: float) -> list[float]:
    with _lock:
        return [p for t, p in _prints[asset] if start <= t < end]


class Poller:
    """Polls every market in the background so pages read a ready snapshot instead of waiting on the network.

    Kalshi quotes and spot are fetched every `every` seconds, candles every `candle_every`,
    all markets in parallel. One poller serves every viewer of the app.
    """

    def __init__(self, assets: list[str], every: float = 1.0, candle_every: float = 15.0):
        from concurrent.futures import ThreadPoolExecutor
        self.assets, self.every, self.candle_every = assets, every, candle_every
        self.quotes: dict[str, Quote | None] = {}
        self.spots: dict[str, tuple[float, float]] = {}
        self.candles: dict[str, list] = {}
        self.errors: dict[str, str | None] = {a: None for a in assets}
        self.updated = 0.0
        self._pool = ThreadPoolExecutor(len(assets) * 3, thread_name_prefix="poll")
        self._last_candles = 0.0
        threading.Thread(target=self._run, daemon=True, name="strike-desk-poller").start()

    def _one(self, asset: str, with_candles: bool) -> None:
        errs = []
        for name, fn in (("Kalshi", lambda: kalshi_quote(asset)), ("spot", lambda: spot(asset)),
                         *((("candles", lambda: candles(asset)),) if with_candles else ())):
            try:
                v = fn()
            except Exception as e:  # keep the last good value; report the error
                errs.append(f"{name}: {type(e).__name__}")
                continue
            if name == "Kalshi":
                self.quotes[asset] = v
            elif name == "spot":
                self.spots[asset] = v
                record_print(asset, v[1], v[0])
            else:
                self.candles[asset] = v
        self.errors[asset] = ", ".join(errs) or None

    def _run(self) -> None:
        while True:
            t0 = time.time()
            with_candles = t0 - self._last_candles >= self.candle_every
            list(self._pool.map(lambda a: self._one(a, with_candles), self.assets))
            if with_candles:
                self._last_candles = t0
            self.updated = time.time()
            time.sleep(max(0.0, self.every - (time.time() - t0)))
