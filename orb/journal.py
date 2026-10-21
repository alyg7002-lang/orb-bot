"""The bot's memory: every trade and every day's setup outcome, in one SQLite file.

File: data/journal.db  (open it with "DB Browser for SQLite" if you want to poke around)

mode is one of:
    backtest - simulated on historical data
    paper    - placed live on your Alpaca paper account
    demo     - fake random data, for testing only
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from .config import ROOT
from .strategy import Trade

DB_PATH = ROOT / "data" / "journal.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY, mode TEXT, started_at TEXT, symbols TEXT, settings TEXT);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT, mode TEXT, symbol TEXT, session_date TEXT, direction TEXT,
    or_high REAL, or_low REAL, breakout_time TEXT, entry_time TEXT,
    entry REAL, stop REAL, target REAL, target_label TEXT, qty INTEGER,
    exit_time TEXT, exit REAL, exit_reason TEXT, pnl REAL, r_multiple REAL,
    broker_order_id TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS sessions (
    run_id TEXT, mode TEXT, symbol TEXT, session_date TEXT, outcome TEXT,
    or_high REAL, or_low REAL, log TEXT,
    PRIMARY KEY (run_id, symbol, session_date));
"""


class Journal:
    def __init__(self, path: Path | str = DB_PATH):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    # ---- writes --------------------------------------------------------
    def start_run(self, run_id: str, mode: str, symbols: list[str], settings: dict) -> None:
        self.db.execute("INSERT OR IGNORE INTO runs VALUES (?,?,?,?,?)",
                        (run_id, mode, datetime.now().isoformat(timespec="seconds"),
                         ",".join(symbols), json.dumps(settings, default=str)))
        self.db.commit()

    def open_trade(self, run_id: str, mode: str, tr: Trade, qty: int,
                   broker_order_id: str | None = None) -> int:
        cur = self.db.execute(
            """INSERT INTO trades (run_id, mode, symbol, session_date, direction, or_high, or_low,
               breakout_time, entry_time, entry, stop, target, target_label, qty,
               broker_order_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (run_id, mode, tr.symbol, tr.session_date.isoformat(), tr.direction, tr.or_high,
             tr.or_low, tr.breakout_time.isoformat(), tr.entry_time.isoformat(), tr.entry,
             tr.initial_stop, tr.target, tr.target_label, qty, broker_order_id,
             datetime.now().isoformat(timespec="seconds")))
        self.db.commit()
        return cur.lastrowid

    def update_entry(self, trade_id: int, entry: float) -> None:
        """Live mode: replace the planned entry with the broker's actual fill."""
        self.db.execute("UPDATE trades SET entry=? WHERE id=?", (entry, trade_id))
        self.db.commit()

    def close_trade(self, trade_id: int, exit_time: datetime, exit_px: float, reason: str,
                    pnl: float) -> None:
        row = self.db.execute("SELECT direction, entry, stop FROM trades WHERE id=?",
                              (trade_id,)).fetchone()
        risk = abs(row["entry"] - row["stop"])
        sign = 1 if row["direction"] == "long" else -1
        r = (exit_px - row["entry"]) * sign / risk if risk else 0.0
        self.db.execute("UPDATE trades SET exit_time=?, exit=?, exit_reason=?, pnl=?, r_multiple=? "
                        "WHERE id=?", (exit_time.isoformat(), exit_px, reason, round(pnl, 2),
                                       round(r, 3), trade_id))
        self.db.commit()

    def save_session(self, run_id: str, mode: str, symbol: str, session_date, outcome: str,
                     or_high, or_low, log: list[str]) -> None:
        self.db.execute("INSERT OR REPLACE INTO sessions VALUES (?,?,?,?,?,?,?,?)",
                        (run_id, mode, symbol, str(session_date), outcome, or_high, or_low,
                         "\n".join(log)))
        self.db.commit()

    # ---- reads ---------------------------------------------------------
    def latest_run(self, mode: str) -> str | None:
        row = self.db.execute("SELECT run_id FROM runs WHERE mode=? ORDER BY started_at DESC, "
                              "rowid DESC LIMIT 1", (mode,)).fetchone()
        return row["run_id"] if row else None

    def trades(self, mode: str, run_id: str | None = None, closed_only: bool = True) -> list[dict]:
        q, args = "SELECT * FROM trades WHERE mode=?", [mode]
        if run_id:
            q += " AND run_id=?"
            args.append(run_id)
        if closed_only:
            q += " AND exit IS NOT NULL"
        q += " ORDER BY session_date, entry_time"
        return [dict(r) for r in self.db.execute(q, args)]

    def open_trades(self, mode: str) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM trades WHERE mode=? AND exit IS NULL", (mode,))]

    def sessions(self, mode: str, run_id: str | None = None) -> list[dict]:
        q, args = "SELECT * FROM sessions WHERE mode=?", [mode]
        if run_id:
            q += " AND run_id=?"
            args.append(run_id)
        return [dict(r) for r in self.db.execute(q + " ORDER BY session_date", args)]

    def run_info(self, run_id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return dict(row) if row else None
