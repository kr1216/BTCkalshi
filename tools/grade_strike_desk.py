"""Replay the Strike Desk fair-odds model against settled Kalshi KXBTC15M markets.

Usage: python tools/grade_strike_desk.py [N_MARKETS] [OUT_JSON]   (env BASIS=5.3 to shift spot)
"""
import json, math, os, sys, time, urllib.request
from datetime import datetime, timezone

K = "https://api.elections.kalshi.com/trade-api/v2"
C = "https://api.crypto.com/exchange/v1/public/get-candlestick"
N_MARKETS = int(sys.argv[1]) if len(sys.argv) > 1 else 300
BASIS = float(os.environ.get("BASIS", "0"))  # $ to subtract from Crypto.com spot to approximate CF BRTI

def get(url, tries=4):
    for i in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                return json.load(r)
        except Exception as e:
            if i == tries - 1: raise
            time.sleep(1.5 * (i + 1))

ts = lambda s: int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())

# ---- desk model, ported 1:1 from the artifact ----
SETTLE = 60
ncdf = lambda z: 0.5 * (1 + math.erf(z / math.sqrt(2)))
def fair_up(spot, strike, s_left, vol, obs_avg=None):
    t = max(s_left, 0)
    if t > SETTLE: mean, v = spot, vol * vol * ((t - SETTLE) + SETTLE / 3)
    else:
        r, n = t, SETTLE - t; oa = spot if obs_avg is None else obs_avg
        mean = (n * oa + r * spot) / SETTLE; v = (r / SETTLE) ** 2 * vol * vol * r / 3
    sd = math.sqrt(v)
    return (1.0 if mean >= strike else 0.0) if sd == 0 else ncdf((mean - strike) / sd)
def fee(p): return math.ceil(round(0.07 * p * (1 - p) * 1e6) / 1e4) / 100
def vol_per_sec(closes):
    c = closes[-31:]
    if len(c) < 11: return None
    return math.sqrt(sum((c[i] - c[i-1]) ** 2 for i in range(1, len(c))) / (len(c) - 1) / 60)

# ---- 1. settled markets ----
markets, cur = [], ""
while len(markets) < N_MARKETS:
    d = get(f"{K}/markets?series_ticker=KXBTC15M&status=settled&limit=200" + (f"&cursor={cur}" if cur else ""))
    markets += [m for m in d["markets"] if m.get("result") in ("yes", "no") and m.get("floor_strike")]
    cur = d.get("cursor")
    if not cur: break
markets = markets[:N_MARKETS]
print(f"markets: {len(markets)}  {markets[-1]['open_time']} → {markets[0]['close_time']}", file=sys.stderr)

# ---- 2. Crypto.com 1m candles covering the span (+35 min warmup) ----
lo = min(ts(m["open_time"]) for m in markets) - 40 * 60
hi = max(ts(m["close_time"]) for m in markets) + 60
cdc = {}
end = hi * 1000
while end > lo * 1000:
    d = get(f"{C}?instrument_name=BTC_USD&timeframe=1m&end_ts={end}&count=300")["result"]["data"]
    if not d: break
    for x in d: cdc[x["t"] // 1000] = (float(x["o"]), float(x["c"]))
    new_end = min(x["t"] for x in d)
    if new_end >= end: break
    end = new_end
print(f"cdc candles: {len(cdc)}", file=sys.stderr)

# ---- 3. replay ----
MIN_EDGE, MAX_P, VMULT = 0.03, 0.92, 1.15
rows, trades, basis, strike_err = [], [], [], []
for i, m in enumerate(markets):
    o, cl, tk = ts(m["open_time"]), ts(m["close_time"]), m["ticker"]
    y = 1 if m["result"] == "yes" else 0
    strike = float(m["floor_strike"])
    # price-source checks
    if (cl - 60) in cdc and m.get("expiration_value"):
        a, b = cdc[cl - 60]; basis.append((a + b) / 2 - float(m["expiration_value"]))
    if o in cdc: strike_err.append(cdc[o][0] - strike)
    try:
        ks = get(f"{K}/series/KXBTC15M/markets/{tk}/candlesticks?start_ts={o}&end_ts={cl}&period_interval=1")["candlesticks"]
    except Exception as e:
        continue
    taken = False
    for c in ks:
        t = c["end_period_ts"]; s_left = cl - t
        if not (20 <= s_left <= 780): continue
        ya, yb = c["yes_ask"].get("close_dollars"), c["yes_bid"].get("close_dollars")
        if ya is None or yb is None: continue
        ya, yb = float(ya), float(yb); na = round(1 - yb, 4)
        if not (0 < ya < 1 and 0 < na < 1): continue
        spot_c = cdc.get(t - 60)
        closes = [cdc[k][1] for k in range(t - 60 * 32, t, 60) if k in cdc]
        if not spot_c or len(closes) < 12: continue
        spot = spot_c[1] - BASIS
        v0 = vol_per_sec(closes[:-1])
        if v0 is None: continue
        p = fair_up(spot, strike, s_left, v0 * VMULT)
        mid = (ya + yb) / 2
        rows.append(dict(k=(t - o) // 60, s_left=s_left, p=p, mid=mid, y=y, dist=abs(spot - strike)))
        evy, evn = p - ya - fee(ya), (1 - p) - na - fee(na)
        best = max([("YES", evy, ya), ("NO", evn, na)], key=lambda x: x[1])
        if not taken and best[1] >= MIN_EDGE and best[2] <= MAX_P:
            win = y if best[0] == "YES" else 1 - y
            fair = p if best[0] == "YES" else 1 - p
            trades.append(dict(tk=tk, side=best[0], price=best[2], fair=fair, ev=best[1], win=win,
                               pnl=win - best[2] - fee(best[2]), s_left=s_left, dist=abs(spot - strike)))
            taken = True
    if i % 50 == 0: print(f"  {i}/{len(markets)}", file=sys.stderr)
    time.sleep(0.05)

json.dump(dict(rows=rows, trades=trades, basis=basis, strike_err=strike_err,
               span=[markets[-1]["open_time"], markets[0]["close_time"]], n=len(markets)),
          open(sys.argv[2] if len(sys.argv) > 2 else "grade.json", "w"))
print("done", len(rows), "snapshots", len(trades), "trades", file=sys.stderr)
