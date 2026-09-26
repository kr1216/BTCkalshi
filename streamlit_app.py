"""Strike Desk: live UP/DOWN calls for Kalshi 15-minute crypto markets."""
import io
import math
import struct
import time
import wave
from datetime import datetime
from zoneinfo import ZoneInfo

import streamlit as st

from strike_desk import feeds, journal
from strike_desk.model import CYCLE_S, PROFILES, SETTLE_S, fee
from strike_desk.signal import Overrides, Settings, evaluate

ET = ZoneInfo("America/New_York")
ASSETS = list(PROFILES)
VERDICT = {
    "edge": "Backtest on Kalshi's real prices: made money after fees on markets it had not seen.",
    "weak": "Backtest on Kalshi's real prices: slightly profitable, but it could be luck. Paper trade first.",
    "none": "Backtest on Kalshi's real prices: lost money at every setting. Watch only; do not trade.",
}

st.set_page_config(page_title="Strike Desk", page_icon="⏱", layout="wide")
st.markdown("""
<style>
:root{--up:#157A4E;--down:#BE3A34;--warn:#A5680A;--up-bg:#E1F2EA;--down-bg:#F8E3E1;--muted:#5C6B69;--tile:#F3F5F4;--rule:#D5DCDA}
@media (prefers-color-scheme: dark){:root{--up:#4CC98F;--down:#F07A70;--warn:#E6B45A;--up-bg:#12281F;--down-bg:#2E1715;--muted:#8C9C9F;--tile:#161D21;--rule:#26323A}}
.sd-board{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px;margin-bottom:.5rem}
.sd-tile{border:1px solid var(--rule);background:var(--tile);border-radius:12px;padding:10px 12px;display:flex;flex-direction:column;gap:2px}
.sd-tile.up{background:var(--up-bg);border-color:var(--up)} .sd-tile.down{background:var(--down-bg);border-color:var(--down)}
.sd-tile.sel{box-shadow:0 0 0 2px currentColor inset}
.sd-tile .a{font:600 .8rem ui-monospace,Menlo,monospace;letter-spacing:.04em;color:var(--muted);display:flex;justify-content:space-between}
.sd-tile .c{font-weight:800;font-size:1.55rem;line-height:1.05}
.sd-tile.up .c{color:var(--up)} .sd-tile.down .c{color:var(--down)}
.sd-tile .s{font-size:.78rem;color:var(--muted)}
.sd-tile .tag{font-size:.68rem;color:var(--warn)}
.sd-verdict{font-weight:800;font-size:2.6rem;line-height:1;letter-spacing:-.02em;margin:0}
.sd-why{color:var(--muted);margin:.35rem 0 0}
.sd-gauge{display:flex;height:30px;border-radius:8px;overflow:hidden;position:relative;font:600 12px ui-monospace,Menlo,monospace;margin:.6rem 0 .2rem}
.sd-gauge .d{background:var(--down-bg);color:var(--down);display:flex;align-items:center;padding-left:8px}
.sd-gauge .u{background:var(--up-bg);color:var(--up);display:flex;align-items:center;justify-content:flex-end;padding-right:8px;flex:1}
.sd-gauge .mk{position:absolute;top:0;bottom:0;width:2px;background:currentColor}
.sd-small{font-size:.8rem;color:var(--muted)}
.sd-checks{list-style:none;padding:0;margin:0;font-size:.92rem}
.sd-checks li{display:flex;justify-content:space-between;gap:1rem;padding:3px 0}
.sd-checks .v{font-family:ui-monospace,Menlo,monospace;font-size:.8rem;color:var(--muted)}
.ok{color:var(--up)} .no{color:var(--down)} .na{color:var(--muted)}
.pos{color:var(--up)} .neg{color:var(--down)}
</style>""", unsafe_allow_html=True)


# ---------- live data: one background poller shared by every viewer ----------
@st.cache_resource
def poller():
    return feeds.Poller(ASSETS, every=1.0)


@st.cache_data(ttl=30, show_spinner=False)
def get_settled(asset):
    return feeds.kalshi_settled(asset)


@st.cache_data
def beep():
    """A short two-tone chime as WAV bytes."""
    rate, buf = 22050, io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        frames = b"".join(struct.pack("<h", int(9000 * math.sin(2 * math.pi * (880 if i < rate * .12 else 1320) * i / rate)
                                                * min(1, (rate * .3 - i) / 800)))
                          for i in range(int(rate * .3)))
        w.writeframes(frames)
    return buf.getvalue()


def safe(fn, *a):
    try:
        return fn(*a), None
    except Exception as e:  # network errors surface in the UI, not as a crash
        return None, f"{type(e).__name__}: {e}"


def num(s):
    try:
        return float(s.replace(",", "").replace("$", "").replace("¢", "")) if s and s.strip() else None
    except ValueError:
        return None


cents = lambda p: f"{p * 100:.1f}¢"
signed = lambda x: ("+" if x >= 0 else "−") + f"{abs(x) * 100:.1f}¢"
mmss = lambda s: f"{int(max(s, 0)) // 60:02d}:{int(max(s, 0)) % 60:02d}"


def usd(x, ref=None):
    if x is None:
        return "—"
    r = abs(ref if ref is not None else x)
    return f"${x:,.{2 if r >= 100 else 4 if r >= 1 else 6}f}"


def label(c):
    return {"UP": "TAKE UP", "DOWN": "TAKE DOWN"}.get(c.action, c.action)


# ---------- per-market settings kept for the whole session ----------
cfg = st.session_state.setdefault("cfg", {a: Settings.default(a) for a in ASSETS})
ovr = st.session_state.setdefault("ovr", {a: Overrides() for a in ASSETS})

with st.sidebar:
    st.header("Strike Desk")
    asset = st.radio("Market in detail", ASSETS, horizontal=True, key="asset",
                     format_func=lambda a: a + (" ·" if PROFILES[a]["tier"] == "thin" else ""))
    pr, s = PROFILES[asset], cfg[asset]
    (st.success if pr["verdict"] == "edge" else st.warning if pr["verdict"] == "weak" else st.error)(VERDICT[pr["verdict"]])
    s.min_edge = st.number_input(f"{asset} min edge, ¢", 0.0, 50.0, s.min_edge * 100, 0.5, key=f"edge.{asset}") / 100
    s.max_price = st.number_input(f"{asset} max price, ¢", 1.0, 99.0, s.max_price * 100, 1.0, key=f"maxp.{asset}") / 100
    s.vol_mult = st.number_input(f"{asset} volatility multiplier", 0.3, 3.0, s.vol_mult, 0.05, key=f"vol.{asset}")
    if st.button(f"Reset {asset} to defaults"):
        cfg[asset] = Settings.default(asset)
        for k in (f"edge.{asset}", f"maxp.{asset}", f"vol.{asset}"):
            st.session_state.pop(k, None)
        st.rerun()
    with st.expander(f"Override {asset} Kalshi values"):
        st.caption("Leave blank to use Kalshi's live values.")
        o = ovr[asset]
        o.strike = num(st.text_input("Target price", key=f"o.strike.{asset}"))
        y, n = num(st.text_input("Yes ask, ¢", key=f"o.yes.{asset}")), num(st.text_input("No ask, ¢", key=f"o.no.{asset}"))
        o.yes, o.no = (y / 100 if y is not None else None), (n / 100 if n is not None else None)
    st.divider()
    refresh = st.select_slider("Refresh every", [1, 2, 5, 10], value=2, format_func=lambda x: f"{x}s", key="refresh")
    only_tested = st.toggle("Only alert on markets that tested profitable", key="only_tested",
                            help="SOL and NEAR made money in the backtest; the others did not.")
    sound = st.toggle("Chime on a new call", value=True, key="sound",
                      help="Your browser may block sound until you have clicked somewhere on the page.")


def settle_all():
    """Score open journal entries and signals against Kalshi's published results."""
    now = time.time()
    waiting_j = [r for r in journal.open_rows() if r["close"] <= now]
    waiting_s = [r for r in journal.open_signals() if r["close"] <= now]
    for a in {r["asset"] for r in waiting_j + waiting_s}:
        settled, _ = safe(get_settled, a)
        for r in waiting_j + waiting_s:
            res = (settled or {}).get(r["close"]) if r["asset"] == a else None
            if not res:
                continue
            won = res["result"] if r["side"] == "YES" else 1 - res["result"]
            if "settled_by" in r:
                journal.settle(r["id"], won, "kalshi", res["value"])
            else:
                journal.settle_signal(r["id"], won, res["value"])


def tile(c, selected):
    cls = "up" if c.action == "UP" else "down" if c.action == "DOWN" else ""
    if c.is_signal:
        sub = f"Buy {'Yes' if c.side == 'YES' else 'No'} at {cents(c.price)} · edge {signed(c.edge)}"
    elif c.action.startswith("LEAN"):
        sub = f"Fair Up {c.p_up * 100:.0f}% · {c.why.lower()}"
    else:
        sub = c.why + (f" · edge {signed(c.edge)}" if c.edge is not None else "")
    tag = "" if PROFILES[c.asset]["verdict"] != "none" else "<span class='tag'>lost money in backtest</span>"
    return (f"<div class='sd-tile {cls}{' sel' if selected else ''}'><span class='a'><span>{c.asset}</span><span>{mmss(c.s_left)}</span></span>"
            f"<span class='c'>{label(c)}</span><span class='s'>{sub}</span>{tag}</div>")


@st.fragment(run_every=refresh)
def desk(asset):
    now, P = time.time(), poller()
    calls, errs = {}, P.errors
    for a in ASSETS:
        q, sp, cs = P.quotes.get(a), P.spots.get(a), P.candles.get(a)
        close = q.close if q else (now // CYCLE_S + 1) * CYCLE_S
        prints = feeds.prints_between(a, close - SETTLE_S, now) if close - now <= SETTLE_S else []
        calls[a] = evaluate(a, now, q, sp, cs, prints, cfg[a], ovr[a])

    # New calls: record them, then alert.
    fresh = []
    for c in calls.values():
        if c.is_signal and journal.record_signal(c.asset, c.close, c.side, c.price,
                                                 c.p_up if c.side == "YES" else 1 - c.p_up, c.edge, c.s_left, c.ticker):
            if not (only_tested and PROFILES[c.asset]["verdict"] == "none"):
                fresh.append(c)
    for c in fresh:
        st.toast(f"**{c.asset}: {label(c)}** · buy {'Yes' if c.side == 'YES' else 'No'} at {cents(c.price)}, edge {signed(c.edge)}", icon="🔔")
    if fresh and sound:
        st.audio(beep(), format="audio/wav", autoplay=True)

    # ---------- board ----------
    live = [c for c in calls.values() if c.is_signal]
    st.markdown(f"### {'Calls now: ' + ', '.join(f'{c.asset} {c.action}' for c in live) if live else 'No calls right now'}")
    st.markdown("<div class='sd-board'>" + "".join(tile(calls[a], a == asset) for a in ASSETS) + "</div>", unsafe_allow_html=True)
    st.caption("A call appears when every check passes: fresh prices, both Kalshi asks, inside the trading window, "
               "price at or below your max and edge above your minimum. Click a market in the sidebar for the detail.")

    # ---------- detail for the selected market ----------
    c = calls[asset]
    st.divider()
    h1, h2 = st.columns([3, 2])
    h1.caption(f"{asset} · Kalshi 15-min market closes in")
    h1.markdown(f"<p style='font:600 2.2rem ui-monospace,Menlo,monospace;margin:0'>{mmss(c.s_left)}</p>", unsafe_allow_html=True)
    h2.caption("Closes " + datetime.fromtimestamp(c.close, ET).strftime("%-I:%M %p ET"))
    h2.caption("Too early: first 2 min" if c.s_left > 780 else "Too late: last 20 s" if c.s_left < 20 else "In the trading window")
    st.progress(min(max((CYCLE_S - c.s_left) / CYCLE_S, 0.0), 1.0))

    color = "var(--up)" if c.action in ("UP", "LEAN UP") else "var(--down)" if c.action in ("DOWN", "LEAN DOWN") else "inherit"
    with st.container(border=True):
        v1, v2 = st.columns([3, 1])
        how = f"Buy {'Yes' if c.side == 'YES' else 'No'} at {cents(c.price)} or better · " if c.is_signal else ""
        v1.markdown(f"<div class='sd-verdict' style='color:{color}'>{label(c)}</div><div class='sd-why'>{how}{c.why}</div>", unsafe_allow_html=True)
        if c.edge is not None:
            v2.markdown(f"<p class='sd-small'>Edge / contract</p><p style='font:600 1.5rem ui-monospace,Menlo,monospace;margin:0' "
                        f"class='{'pos' if c.edge >= 0 else 'neg'}'>{signed(c.edge)}</p>", unsafe_allow_html=True)
        if c.p_up is not None:
            up = c.p_up * 100
            mk = f"<span class='mk' style='left:calc({(1 - c.p_yes) * 100:.1f}% - 1px)'></span>" if c.p_yes else ""
            st.markdown(f"<div class='sd-gauge'><span class='d' style='flex:0 0 {100 - up:.1f}%'>{'DOWN %.0f%%' % (100 - up) if 100 - up >= 14 else ''}</span>"
                        f"<span class='u'>{'UP %.0f%%' % up if up >= 14 else ''}</span>{mk}</div>"
                        f"<p class='sd-small'>Bar = fair odds{f' · line = Kalshi Yes {cents(c.p_yes)}' if c.p_yes else ''}</p>", unsafe_allow_html=True)

    m = st.columns(3)
    m[0].metric("Spot (Crypto.com)", usd(c.spot))
    m[1].metric("Target", usd(c.strike, c.spot), help="The arrow shows spot minus target.",
                delta=(f"{c.spot - c.strike:+,.2f}" if c.spot >= 100 else f"{c.spot - c.strike:+.5f}") if c.spot is not None and c.strike is not None else None)
    m[2].metric("Fair Up / Down", f"{c.p_up * 100:.0f}% / {(1 - c.p_up) * 100:.0f}%" if c.p_up is not None else "—",
                help=f"Model alone: {c.p_model * 100:.1f}%" if c.p_model is not None else None)
    m = st.columns(3)
    m[0].metric("Yes (Up) ask", cents(c.p_yes) if c.p_yes else "—")
    m[1].metric("No (Down) ask", cents(c.p_no) if c.p_no else "—")
    m[2].metric("Expected move left (1σ)", "±" + usd(c.sd, c.spot)[1:] if c.sd is not None else "—")
    ev_txt = lambda e: f"<span class='{'pos' if e >= 0 else 'neg'}'>{signed(e)}</span>" if e is not None else "—"
    be = " · ".join(f"{k} {cents(p + fee(p))}" for k, p in (("Yes", c.p_yes), ("No", c.p_no)) if p)
    st.markdown(f"<p class='sd-small'>Buy Yes EV after fee {ev_txt(c.ev_yes)} · Buy No {ev_txt(c.ev_no)} · Break-even {be or '—'}"
                f"{f' · vol {usd(c.vol * 60 ** 0.5, c.spot)}/min' if c.vol else ''}</p>", unsafe_allow_html=True)

    with st.expander("Checks", expanded=c.action == "WAIT"):
        st.markdown("<ul class='sd-checks'>" + "".join(
            f"<li><span class='{'ok' if v is True else 'no' if v is False else 'na'}'>{'●' if v is not None else '○'} {t}</span><span class='v'>{d}</span></li>"
            for t, v, d in c.checks) + "</ul>", unsafe_allow_html=True)
    src = f"Kalshi {c.ticker} · {c.quote_age:.0f}s ago" if c.ticker else f"Kalshi has no open {asset} market"
    st.caption(f"{src} · Crypto.com {asset}_USD {f'{c.spot_age:.0f}s ago' if c.spot_age is not None else '—'} · "
               f"refreshes every {refresh}s" + (f" · {errs[asset]}" if errs[asset] else ""))

    b1, b2 = st.columns(2)
    for col, side, price in ((b1, "YES", c.p_yes), (b2, "NO", c.p_no)):
        if col.button(f"Log {'Up (Yes)' if side == 'YES' else 'Down (No)'} bought", key=f"log.{side}",
                      disabled=not (c.p_up is not None and price), use_container_width=True,
                      type="primary" if c.side == side else "secondary"):
            journal.add(asset, side, price, c.p_up if side == "YES" else 1 - c.p_up, c.strike, c.spot, c.close, c.ticker)
            st.toast(f"Logged {asset} {side} at {cents(price)}")


def pnl(r):
    return r["outcome"] - r["price"] - fee(r["price"])


@st.fragment(run_every=15)
def records():
    settle_all()
    now = time.time()

    st.subheader("Call record")
    st.caption("Every TAKE UP / TAKE DOWN the board has shown, scored at the ask it showed, after Kalshi's fee.")
    sig = journal.signals()
    done = [r for r in sig if r["outcome"] in (0, 1)]
    s = st.columns(4)
    s[0].metric("Calls settled", len(done), help=f"{len(sig) - len(done)} still open")
    s[1].metric("Right", f"{sum(r['outcome'] for r in done)} ({sum(r['outcome'] for r in done) / len(done):.0%})" if done else "—")
    s[2].metric("P&L / contract", f"{'+' if sum(map(pnl, done)) >= 0 else '−'}${abs(sum(map(pnl, done))):.2f}" if done else "—",
                help="Total over all settled calls, one contract each")
    s[3].metric("Avg edge promised", signed(sum(r["edge"] for r in done) / len(done)) if done else "—")
    if done:
        by = {}
        for r in done:
            b = by.setdefault(r["asset"], [0, 0, 0.0])
            b[0] += 1; b[1] += r["outcome"]; b[2] += pnl(r)
        st.dataframe([{"Market": a, "Calls": n, "Right": f"{w / n:.0%}", "P&L / contract": round(p, 2)} for a, (n, w, p) in sorted(by.items())],
                     hide_index=True, use_container_width=True)
    if sig:
        with st.expander("Recent calls"):
            st.dataframe([{"Close (ET)": datetime.fromtimestamp(r["close"], ET).strftime("%b %d %-I:%M %p"), "Market": r["asset"],
                           "Call": "UP" if r["side"] == "YES" else "DOWN", "Ask": round(r["price"] * 100, 1), "Edge ¢": round(r["edge"] * 100, 1),
                           "Min left": round(r["s_left"] / 60, 1),
                           "Result": "RIGHT" if r["outcome"] == 1 else "WRONG" if r["outcome"] == 0 else "OPEN" if r["close"] > now else "SETTLING"}
                          for r in sig[:100]], hide_index=True, use_container_width=True)

    st.subheader("Journal")
    st.caption("Trades you log. They settle from Kalshi's results automatically. The file resets when the app is redeployed, so download the CSV to keep it.")
    rows = journal.rows()
    jd = [r for r in rows if r["outcome"] in (0, 1)]
    s = st.columns(4)
    s[0].metric("Settled", len(jd))
    s[1].metric("Won", f"{sum(r['outcome'] for r in jd)} ({sum(r['outcome'] for r in jd) / len(jd):.0%})" if jd else "—")
    s[2].metric("P&L / contract", f"{'+' if sum(map(pnl, jd)) >= 0 else '−'}${abs(sum(map(pnl, jd))):.2f}" if jd else "—")
    s[3].metric("Brier", f"{sum((r['fair_win'] - r['outcome']) ** 2 for r in jd) / len(jd):.3f}" if jd else "—")
    if not rows:
        st.caption("No trades logged yet.")
        return
    table = [{"Close (ET)": datetime.fromtimestamp(r["close"], ET).strftime("%b %d %-I:%M %p"), "Market": r["asset"],
              "Side": "UP" if r["side"] == "YES" else "DOWN", "Price": round(r["price"] * 100, 1), "Fair %": round(r["fair_win"] * 100),
              "Target": r["strike"], "Result": "WON" if r["outcome"] == 1 else "LOST" if r["outcome"] == 0 else "OPEN" if r["close"] > now else "SETTLING",
              "Settled at": r["settle_value"]} for r in rows]
    st.dataframe(table, hide_index=True, use_container_width=True)
    csv = "\n".join([",".join(table[0])] + [",".join("" if v is None else str(v) for v in t.values()) for t in table])
    st.download_button("Download CSV", csv, "strike-desk-journal.csv", "text/csv")


desk(asset)
records()

with st.expander("How the calls are made"):
    st.markdown(
        "Kalshi settles on the average of 60 one-second CF Benchmarks prints in the final minute. The model treats price as a "
        "random walk with volatility from the last 30 one-minute candles and works out the chance the final-minute average finishes "
        "at or above the target. Inside the last minute, it averages the spot prints this server has already seen.\n\n"
        "That model is blended with Kalshi's own odds (the midpoint of the Yes and No asks) using per-market weights from a backtest "
        "on ten days of Kalshi history. **TAKE UP** means buying Yes has more than your minimum edge after Kalshi's fee "
        "(7% × p × (1−p), rounded up to the cent); **TAKE DOWN** means the same for No.\n\n"
        "Crypto.com spot stands in for the CF Benchmarks index Kalshi settles on. It has run about $5 above it for BTC, so treat a call "
        "within ±$20 of the target as a coin flip. The backtest found the model's edge only held on prices a few seconds old and only "
        "for some markets. Check the call record before trusting a call with money.")
