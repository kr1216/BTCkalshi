# Strike Desk

Live UP/DOWN calls for Kalshi's 15-minute crypto markets (BTC, ETH, SOL, XRP, DOGE, NEAR).

A background poller pulls Kalshi's target price and Yes/No asks and Crypto.com spot
for every market once a second. Each market is priced with a random-walk model of
the final-minute settlement average, blended with Kalshi's own odds, and the board
shows **TAKE UP**, **TAKE DOWN** or **WAIT** for all six at once, with a pop-up and
optional chime when a new call appears.

Every call is recorded and scored against Kalshi's result in the **Call record**, so
you can see whether the calls actually make money before trusting them. Trades you
log yourself settle automatically too.

It is a research tool. Backtests against Kalshi's real prices found no reliable edge
for most markets; see the notes in the app.

## Run it

Install [`uv`](https://docs.astral.sh/uv/), then:

```
uv sync
uv run streamlit run streamlit_app.py
```

No API keys are needed; both Kalshi's and Crypto.com's market data endpoints are public.

## Journal storage

The journal is a SQLite file at `data/journal.sqlite3` (override with `STRIKE_DESK_DB`).
On Streamlit Community Cloud that file is wiped whenever the app restarts or redeploys,
so use **Download CSV** to keep a copy.

## Tools

- `tools/grade_strike_desk.py` replays the model against settled Kalshi markets.
- `tools/kalshi_bridge.py` snapshots live Kalshi quotes as JSON (used by the earlier artifact relay).

## Tests

```
uv run python -m unittest discover -s tests -t .
```
