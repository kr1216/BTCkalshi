"""Strike Desk: live fair odds for Kalshi 15-minute crypto markets."""
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import streamlit as st

from strike_desk import feeds, journal
from strike_desk.model import (CYCLE_S, PROFILES, SETTLE_S, blend, ev, fair_up, fee, tradable,
                               vol_per_sec)

ET = ZoneInfo("America/New_York")
KALSHI_STALE_S = 10   # a Kalshi quote older than this is not used
SPOT_STALE_S = 15     # a Crypto.com print older than this fails the freshness check

st.set_page_config(page_title="Strike Desk", page_icon="⏱", layout="centered")
st.markdown("""
<style>
:root{--up:#157A4E;--down:#BE3A34;--warn:#A5680A;--accent:#2743C4;--up-bg:#E1F2EA;--down-bg:#F8E3E1;--muted:#5C6B69}
@media (prefers-color-scheme: dark){:root{--up:#4CC98F;--down:#F07A70;--warn:#E6B45A;--accent:#7D95FF;--up-bg:#12281F;--down-bg:#2E1715;--muted:#8C9C9F}}
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


# ---------- cached fetchers (shared by every viewer) ----------
@st.cache_data(ttl=1.5, show_spinner=False)
def get_quote(asset):
    return feeds.kalshi_quote(asset)

@st.cache_data(ttl=1.0, show_spinner=False)
def get_spot(asset):
    return feeds.spot(asset)

@st.cache_data(ttl=15, show_spinner=False)
def get_candles(asset):
    return feeds.candles(asset)

@st.cache_data(ttl=30, show_spinner=False)
def get_settled(asset):
    return feeds.kalshi_settled(asset)


def safe(fn, *a):
    try:
        return fn(*a), None
    except Exception as e:  # network errors surface in the UI, not as a crash
        return None, f"{type(e).__name__}: {e}"


cents = lambda p: f"{p * 100:.1f}¢"
signed = lambda x: ("+" if x >= 0 else "−") + f"{abs(x) * 100:.1f}¢"
def num(s):
    try:
        return float(s.replace(",", "").replace("$", "").replace("¢", "")) if s and s.strip() else None
    except ValueError:
        return None

mmss = lambda s: f"{int(max(s, 0)) // 60:02d}:{int(max(s, 0)) % 60:02d}"


def usd(x, spot_hint=None):
    if x is None:
        return "—"
    ref = abs(spot_hint if spot_hint is not None else x)
    dp = 2 if ref >= 100 else 4 if ref >= 1 else 6
    return f"${x:,.{dp}f}"


# ---------- sidebar ----------
with st.sidebar:
    st.header("Strike Desk")
    asset = st.radio("Market", list(PROFILES), horizontal=True, key="asset",
                     format_func=lambda a: a + (" ·" if PROFILES[a]["tier"] == "thin" else ""))
    pr = PROFILES[asset]
    verdict_note = {
        "edge": "Backtest on Kalshi's real prices: made money after fees on markets it had not seen.",
        "weak": "Backtest on Kalshi's real prices: slightly profitable, but it could be luck. Paper trade first.",
        "none": "Backtest on Kalshi's real prices: lost money at every setting. Watch only; do not trade.",
    }[pr["verdict"]]
    (st.success if pr["verdict"] == "edge" else st.warning if pr["verdict"] == "weak" else st.error)(verdict_note)
    min_edge = st.number_input("Min edge, ¢", 0.0, 50.0, float(pr["edge"]), 0.5, key=f"edge.{asset}") / 100
    max_price = st.number_input("Max price, ¢", 1.0, 99.0, 92.0, 1.0, key=f"maxp.{asset}") / 100
    vol_mult = st.number_input("Volatility multiplier", 0.3, 3.0, float(pr["vol"]), 0.05, key=f"vol.{asset}")
    refresh = st.select_slider("Refresh every", [1, 2, 5, 10], value=2, format_func=lambda s: f"{s}s", key="refresh")
    with st.expander("Override Kalshi values"):
        st.caption("Leave blank to use Kalshi's live values.")
        o_strike = st.text_input("Target price", key=f"o.strike.{asset}")
        o_yes = st.text_input("Yes ask, ¢", key=f"o.yes.{asset}")
        o_no = st.text_input("No ask, ¢", key=f"o.no.{asset}")


def autosettle():
    """Mark open calls from Kalshi's published results, for every market that has one waiting."""
    now = time.time()
    waiting = [r for r in journal.open_rows() if r["close"] <= now]
    for a in {r["asset"] for r in waiting}:
        settled, _ = safe(get_settled, a)
        for r in (x for x in waiting if x["asset"] == a):
            s = (settled or {}).get(r["close"])
            if s:
                journal.settle(r["id"], s["result"] if r["side"] == "YES" else 1 - s["result"], "kalshi", s["value"])


@st.fragment(run_every=refresh)
def live(asset):
    now = time.time()
    q, q_err = safe(get_quote, asset)
    sp, sp_err = safe(get_spot, asset)
    cs, cs_err = safe(get_candles, asset)

    close = q.close if q else (now // CYCLE_S + 1) * CYCLE_S
    open_ = close - CYCLE_S
    s_left = close - now
    in_window = 20 <= s_left <= 780

    spot, spot_ts = sp if sp else (None, None)
    if spot is not None:
        feeds.record_print(asset, spot_ts, spot)
    spot_age = max(now - spot_ts, 0.0) if spot_ts else None
    spot_fresh = spot_age is not None and spot_age <= SPOT_STALE_S

    closed = [c for c in (cs or []) if c[0] + 60 <= now]
    vol0 = vol_per_sec([c[2] for c in closed])
    vol = vol0 * vol_mult if vol0 is not None else None

    q_age = max(now - q.fetched, 0.0) if q else None
    q_fresh = q_age is not None and q_age <= KALSHI_STALE_S
    est = next((c[1] for c in (cs or []) if c[0] == open_), None)
    k_target = q.target if q else None
    strike = num(o_strike) if num(o_strike) is not None else k_target if k_target is not None else est

    k_yes = q.yes_ask if q and q_fresh and tradable(q.yes_ask) else None
    k_no = q.no_ask if q and q_fresh and tradable(q.no_ask) else None
    p_yes = num(o_yes) / 100 if num(o_yes) is not None else k_yes
    p_no = num(o_no) / 100 if num(o_no) is not None else k_no
    both = tradable(p_yes) and tradable(p_no)

    # Final minute: average the prints this server has seen; fall back to the minute's candle.
    obs_avg = None
    if s_left <= SETTLE_S:
        seen = feeds.prints_between(asset, close - SETTLE_S, now)
        if len(seen) >= 5:
            obs_avg = sum(seen) / len(seen)
        else:
            last = next((c for c in (cs or []) if c[0] == close - 60), None)
            obs_avg = (last[1] + last[2]) / 2 if last else None

    ok = spot is not None and strike is not None and vol is not None
    fm = fair_up(spot, strike, s_left, vol, obs_avg) if ok else None
    p_up = blend(fm.p, (p_yes + 1 - p_no) / 2, pr["blend"]) if fm and both else (fm.p if fm else None)
    ev_y = ev(p_up, p_yes) if p_up is not None and tradable(p_yes) else None
    ev_n = ev(1 - p_up, p_no) if p_up is not None and tradable(p_no) else None
    cands = [(s, e, p) for s, e, p in (("YES", ev_y, p_yes), ("NO", ev_n, p_no)) if e is not None and p <= max_price]
    best = max(cands, key=lambda x: x[1]) if cands else None

    # ---------- header ----------
    c1, c2 = st.columns([3, 2])
    with c1:
        st.caption(f"{asset} · Kalshi 15-min market closes in")
        st.markdown(f"<p style='font:600 2.2rem ui-monospace,Menlo,monospace;margin:0'>{mmss(s_left)}</p>", unsafe_allow_html=True)
    with c2:
        st.caption("Closes " + datetime.fromtimestamp(close, ET).strftime("%-I:%M %p ET"))
        st.caption("Too early: first 2 min" if s_left > 780 else "Too late: last 20 s" if s_left < 20 else "In the trading window")
    st.progress(min(max((CYCLE_S - s_left) / CYCLE_S, 0.0), 1.0))

    # ---------- verdict ----------
    checks = [
        ("Spot price fresh", None if spot is None else spot_fresh, f"{spot_age:.0f}s old" if spot_age is not None else (sp_err or "—")),
        ("Volatility measured", None if not cs else vol0 is not None, f"{len(closed)} candles" if vol0 is not None else "needs 11+"),
        ("Target set", strike is not None, "override" if num(o_strike) is not None else "Kalshi" if k_target is not None else "estimate" if est is not None else "missing"),
        ("Both Kalshi prices", both, (f"Y {cents(p_yes)} / N {cents(p_no)}" if both else "quote too old" if q and not q_fresh else "none")),
        ("Inside trading window", in_window, f"{mmss(s_left)} left"),
        ("Price at or below max", True if best else (None if ev_y is None and ev_n is None else False), f"≤ {cents(max_price)}"),
        ("Edge clears minimum", None if not best else best[1] >= min_edge, signed(best[1]) if best else "—"),
    ]
    if not ok:
        action, why, color = "—", (sp_err and "Can't reach Crypto.com.") or (cs_err and "Can't load candles.") or ("Measuring volatility…" if vol is None else "No target price yet."), "inherit"
    elif not both:
        action, why = ("Leans UP" if p_up >= 0.5 else "Leans DOWN"), "Waiting for both Kalshi prices."
        color = "var(--up)" if p_up >= 0.5 else "var(--down)"
    elif all(c[1] is True for c in checks):
        action = "BUY YES" if best[0] == "YES" else "BUY NO"
        why = f"Fair {(p_up if best[0] == 'YES' else 1 - p_up) * 100:.1f}% vs {cents(best[2])} price"
        color = "var(--up)" if best[0] == "YES" else "var(--down)"
    else:
        fail = next(c for c in checks if c[1] is not True)[0]
        action, color = "SKIP", "inherit"
        why = {"Edge clears minimum": "The price already reflects the odds",
               "Price at or below max": "Too expensive: the upside is too small"}.get(fail, fail + " ✕")
    edge_best = max([e for e in (ev_y, ev_n) if e is not None], default=None)

    with st.container(border=True):
        v1, v2 = st.columns([3, 1])
        v1.markdown(f"<div class='sd-verdict' style='color:{color}'>{action}</div><div class='sd-why'>{why}</div>", unsafe_allow_html=True)
        if edge_best is not None:
            v2.markdown(f"<p class='sd-small'>Edge / contract</p><p style='font:600 1.5rem ui-monospace,Menlo,monospace;margin:0' class='{'pos' if edge_best >= 0 else 'neg'}'>{signed(edge_best)}</p>", unsafe_allow_html=True)
        if p_up is not None:
            up = p_up * 100
            mk = f"<span class='mk' style='left:calc({(1 - p_yes) * 100:.1f}% - 1px)'></span>" if tradable(p_yes) else ""
            st.markdown(f"<div class='sd-gauge'><span class='d' style='flex:0 0 {100 - up:.1f}%'>{'DOWN %.0f%%' % (100 - up) if 100 - up >= 14 else ''}</span>"
                        f"<span class='u'>{'UP %.0f%%' % up if up >= 14 else ''}</span>{mk}</div>"
                        f"<p class='sd-small'>Bar = fair odds{f' · line = Kalshi Yes {cents(p_yes)}' if tradable(p_yes) else ''}</p>", unsafe_allow_html=True)

    # ---------- numbers ----------
    m = st.columns(3)
    m[0].metric("Spot (Crypto.com)", usd(spot), help=f"{asset}_USD last trade")
    m[1].metric("Target", usd(strike, spot), help="The arrow shows spot minus target.", delta=(f"{spot - strike:+,.2f}" if spot is not None and strike is not None and spot >= 100
                                                     else f"{spot - strike:+.5f}" if spot is not None and strike is not None else None))
    m[2].metric("Fair Up / Down", f"{p_up * 100:.0f}% / {(1 - p_up) * 100:.0f}%" if p_up is not None else "—",
                help=f"Model alone: {fm.p * 100:.1f}%" if fm else None)
    m = st.columns(3)
    m[0].metric("Yes ask", cents(p_yes) if p_yes is not None else "—", help=f"bid {cents(q.yes_bid)}" if q and q.yes_bid is not None else None)
    m[1].metric("No ask", cents(p_no) if p_no is not None else "—", help=f"bid {cents(q.no_bid)}" if q and q.no_bid is not None else None)
    m[2].metric("Expected move left (1σ)", "±" + usd(fm.sd, spot)[1:] if fm else "—")
    ev_txt = lambda e: f"<span class='{'pos' if e >= 0 else 'neg'}'>{signed(e)}</span>" if e is not None else "—"
    be = " · ".join(f"{s} {cents(p + fee(p))}" for s, p in (("Yes", p_yes), ("No", p_no)) if tradable(p))
    st.markdown(f"<p class='sd-small'>Buy Yes EV after fee {ev_txt(ev_y)} · Buy No {ev_txt(ev_n)} · Break-even {be or '—'}"
                f"{f' · vol {usd(vol * 60 ** 0.5, spot)}/min' if vol else ''}</p>", unsafe_allow_html=True)

    # ---------- checks ----------
    with st.expander("Checks", expanded=action == "SKIP"):
        st.markdown("<ul class='sd-checks'>" + "".join(
            f"<li><span class='{'ok' if v is True else 'no' if v is False else 'na'}'>{'●' if v is not None else '○'} {t}</span><span class='v'>{d}</span></li>"
            for t, v, d in checks) + "</ul>", unsafe_allow_html=True)

    # ---------- sources ----------
    k_state = (f"Kalshi {q.ticker} · {q_age:.0f}s ago" if q else f"Kalshi: {q_err}" if q_err else f"Kalshi has no open {asset} market")
    st.caption(f"{k_state} · Crypto.com {asset}_USD {f'{spot_age:.0f}s ago' if spot_age is not None else sp_err or '—'} · refreshes every {refresh}s")

    # ---------- log ----------
    b1, b2 = st.columns(2)
    for col, side, price in ((b1, "YES", p_yes), (b2, "NO", p_no)):
        if col.button(f"Log {side.title()} bought", key=f"log.{side}", disabled=not (p_up is not None and tradable(price)),
                      type="primary" if best and best[0] == side and action.startswith("BUY") else "secondary", use_container_width=True):
            journal.add(asset, side, price, p_up if side == "YES" else 1 - p_up, strike, spot, close, q.ticker if q else None)
            st.toast(f"Logged {asset} {side} at {cents(price)}")


@st.fragment(run_every=15)
def journal_view():
    autosettle()
    rows = journal.rows()
    done = [r for r in rows if r["outcome"] in (0, 1)]
    st.subheader("Journal")
    st.caption("Calls settle from Kalshi's results automatically. The file resets when the app is redeployed, so download the CSV to keep it.")
    wins = sum(r["outcome"] for r in done)
    pnl = sum(r["outcome"] - r["price"] - fee(r["price"]) for r in done)
    brier = sum((r["fair_win"] - r["outcome"]) ** 2 for r in done) / len(done) if done else None
    s = st.columns(4)
    s[0].metric("Settled", len(done))
    s[1].metric("Won", f"{wins} ({wins / len(done):.0%})" if done else "—")
    s[2].metric("P&L / contract", f"{'+' if pnl >= 0 else '−'}${abs(pnl):.2f}" if done else "—")
    s[3].metric("Brier", f"{brier:.3f}" if brier is not None else "—")
    if not rows:
        st.caption("No calls logged yet.")
        return
    now = time.time()
    table = [{"Close (ET)": datetime.fromtimestamp(r["close"], ET).strftime("%b %d %-I:%M %p"), "Market": r["asset"], "Side": r["side"],
              "Price": round(r["price"] * 100, 1), "Fair %": round(r["fair_win"] * 100), "Target": r["strike"],
              "Result": "WON" if r["outcome"] == 1 else "LOST" if r["outcome"] == 0 else "OPEN" if r["close"] > now else "SETTLING",
              "Settled at": r["settle_value"]} for r in rows]
    st.dataframe(table, hide_index=True, use_container_width=True)
    csv = "\n".join([",".join(table[0])] + [",".join("" if v is None else str(v) for v in t.values()) for t in table])
    st.download_button("Download CSV", csv, "strike-desk-journal.csv", "text/csv")


live(asset)
journal_view()

with st.expander("How the fair odds are worked out"):
    st.markdown(
        "Kalshi settles on the average of 60 one-second CF Benchmarks prints in the final minute. The model treats price as a "
        "random walk with volatility from the last 30 one-minute candles and works out the chance the final-minute average finishes "
        "at or above the target. Inside the last minute, it averages the spot prints this server has already seen.\n\n"
        "That model is blended with Kalshi's own odds (the midpoint of the Yes and No asks) using per-market weights from a backtest "
        "on ten days of Kalshi history, so both prices are needed before it calls a trade.\n\n"
        "Edge = fair chance − ask − Kalshi's fee (7% × p × (1−p), rounded up to the cent).\n\n"
        "Crypto.com spot stands in for the CF Benchmarks index Kalshi settles on. It has run about $5 above it for BTC, so treat a call "
        "within ±$20 of the target as a coin flip. The backtest found the model's edge only held on prices a few seconds old; this page "
        "refreshes every few seconds but is still a research tool, not a trading signal.")
