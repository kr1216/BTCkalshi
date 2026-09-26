"""Trade journal in a local SQLite file.

On Streamlit Community Cloud the file lives on the app's disk and is wiped
when the app is rebuilt or restarted; export the CSV to keep a copy.
"""
from __future__ import annotations

import contextlib
import os
import sqlite3
import time

PATH = os.environ.get("STRIKE_DESK_DB", os.path.join(os.path.dirname(__file__), "..", "data", "journal.sqlite3"))

SCHEMA = """
create table if not exists journal (
  id integer primary key autoincrement,
  at real not null, asset text not null, side text not null, price real not null,
  fair_win real not null, strike real, spot real, close real not null, ticker text,
  outcome integer, settled_by text, settle_value real
);
create table if not exists signals (
  id integer primary key autoincrement,
  at real not null, asset text not null, close real not null, side text not null,
  price real not null, fair_win real not null, edge real not null, s_left real not null, ticker text,
  outcome integer, settle_value real,
  unique (asset, close, side)
)"""


@contextlib.contextmanager
def _conn():
    """A connection that commits on success and is always closed."""
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    c = sqlite3.connect(PATH)
    try:
        c.row_factory = sqlite3.Row
        c.executescript(SCHEMA)
        with c:
            yield c
    finally:
        c.close()


def add(asset: str, side: str, price: float, fair_win: float, strike: float, spot: float, close: float, ticker: str | None) -> None:
    with _conn() as c:
        c.execute("insert into journal (at, asset, side, price, fair_win, strike, spot, close, ticker) values (?,?,?,?,?,?,?,?,?)",
                  (time.time(), asset, side, price, fair_win, strike, spot, close, ticker))


def rows(limit: int = 200) -> list[dict]:
    with _conn() as c:
        return [dict(r) for r in c.execute("select * from journal order by at desc limit ?", (limit,))]


def open_rows() -> list[dict]:
    with _conn() as c:
        return [dict(r) for r in c.execute("select * from journal where outcome is null")]


def settle(row_id: int, outcome: int, by: str, value: float | None = None) -> None:
    with _conn() as c:
        c.execute("update journal set outcome=?, settled_by=?, settle_value=? where id=?", (outcome, by, value, row_id))


def delete(row_id: int) -> None:
    with _conn() as c:
        c.execute("delete from journal where id=?", (row_id,))


# ---------- signals: every UP/DOWN call the dashboard makes, first time per market and side ----------
def record_signal(asset: str, close: float, side: str, price: float, fair_win: float, edge: float,
                  s_left: float, ticker: str | None) -> bool:
    """Store a call; returns True if it is new for this market and side."""
    with _conn() as c:
        cur = c.execute("insert or ignore into signals (at, asset, close, side, price, fair_win, edge, s_left, ticker)"
                        " values (?,?,?,?,?,?,?,?,?)", (time.time(), asset, close, side, price, fair_win, edge, s_left, ticker))
        return cur.rowcount == 1


def signals(limit: int = 500) -> list[dict]:
    with _conn() as c:
        return [dict(r) for r in c.execute("select * from signals order by at desc limit ?", (limit,))]


def open_signals() -> list[dict]:
    with _conn() as c:
        return [dict(r) for r in c.execute("select * from signals where outcome is null")]


def settle_signal(row_id: int, outcome: int, value: float | None) -> None:
    with _conn() as c:
        c.execute("update signals set outcome=?, settle_value=? where id=?", (outcome, value, row_id))
