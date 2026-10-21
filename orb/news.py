"""High-impact news days (orb/news_days.csv). The bot sits these out."""
from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

NEWS_FILE = Path(__file__).resolve().parent / "news_days.csv"


def load(path: Path = NEWS_FILE) -> dict[date, list[tuple[str, str]]]:
    table: dict[date, list[tuple[str, str]]] = {}
    if not path.exists():
        return table
    with open(path, newline="", encoding="utf-8") as f:
        rows = (ln for ln in f if ln.strip() and not ln.lstrip().startswith("#"))
        for r in csv.DictReader(rows):
            d = date.fromisoformat(r["date"].strip())
            table.setdefault(d, []).append((r["symbol"].strip().upper(), r["event"].strip()))
    return table


def event_for(symbol: str, day: date, table: dict) -> str | None:
    """The news event that blocks trading this symbol today, or None."""
    for sym, event in table.get(day, []):
        if sym in ("ALL", symbol.upper()):
            return event
    return None
