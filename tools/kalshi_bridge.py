"""Snapshot live Kalshi 15-minute crypto markets for the Strike Desk artifact.

Writes one JSON document per asset (the currently active market) plus a
`settled` document with recent results into OUT_DIR. Each file is sent as-is
to the artifact's `kalshi` collection (doc id = file name without .json).

Usage: python tools/kalshi_bridge.py OUT_DIR
"""
import json, os, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

K = "https://api.elections.kalshi.com/trade-api/v2"
ASSETS = ["BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE", "ZEC", "NEAR"]
SETTLED_KEEP = 12  # per asset, ~3 hours

def get(url):
    for i in range(3):
        try:
            with urllib.request.urlopen(url, timeout=15) as r:
                return json.load(r)
        except Exception:
            if i == 2: raise
            time.sleep(1 + i)

ms = lambda s: int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000)
num = lambda v: None if v in (None, "") else float(v)

def live(asset):
    ms_ = get(f"{K}/markets?series_ticker=KX{asset}15M&status=open&limit=10")["markets"]
    act = sorted((m for m in ms_ if m.get("status") == "active"), key=lambda m: m["close_time"])
    if not act: return None
    m = act[0]
    return {
        "asset": asset, "ticker": m["ticker"], "open": ms(m["open_time"]), "close": ms(m["close_time"]),
        "target": num(m.get("floor_strike")),
        "yesBid": num(m.get("yes_bid_dollars")), "yesAsk": num(m.get("yes_ask_dollars")),
        "noBid": num(m.get("no_bid_dollars")), "noAsk": num(m.get("no_ask_dollars")),
        "fetchedAt": int(time.time() * 1000),
    }

def settled(asset):
    ms_ = get(f"{K}/markets?series_ticker=KX{asset}15M&status=settled&limit={SETTLED_KEEP}")["markets"]
    return {f"{asset}.{ms(m['close_time'])}": {
                "ticker": m["ticker"], "result": 1 if m["result"] == "yes" else 0,
                "target": num(m.get("floor_strike")), "value": num(m.get("expiration_value"))}
            for m in ms_ if m.get("result") in ("yes", "no")}

def main(out):
    os.makedirs(out, exist_ok=True)
    with ThreadPoolExecutor(len(ASSETS) * 2) as ex:
        lives = list(ex.map(live, ASSETS))
        sets = list(ex.map(settled, ASSETS))
    for a, d in zip(ASSETS, lives):
        json.dump(d or {"asset": a, "ticker": None, "fetchedAt": int(time.time() * 1000)},
                  open(os.path.join(out, f"{a}.json"), "w"))
    results = {}
    for s in sets: results.update(s)
    json.dump({"results": results, "fetchedAt": int(time.time() * 1000)},
              open(os.path.join(out, "settled.json"), "w"))
    print(" ".join(f"{d['asset']}:{d['yesAsk']}/{d['noAsk']}" for d in lives if d), f"| settled {len(results)}")

if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "kalshi_out")
