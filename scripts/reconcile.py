"""Health check: did every bot run, and does the journal match what really happened?

    python scripts/reconcile.py      writes data/health.json (read-only on Alpaca: never places an order)

Three checks, shown on the Trades page in Jarvis:
  1. ORB trades vs Alpaca: every order has a journal row, every journal row has an order, fills match.
     (RSI(2), NR7 and Vol Breakout send no orders by design, so they must have no broker order id.)
  2. Coverage: every market day since each bot started has a logged decision for every ticker.
  3. Candles: every recorded exit price was really traded in that minute's candle.
"""
import csv, json, sqlite3, sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
ET = ZoneInfo("America/New_York")
OUT = ROOT / "data" / "health.json"
CHARTS = ROOT / "data" / "charts"
PRICE_TOL = 0.005       # half a cent
CANDLE_TOL = 0.001      # 0.1%: paper fills and SIP candles can differ a little, not by dollars
BOTS = {"paper": ("ORB", date(2026, 10, 21)), "paper_rsi2": ("RSI(2)", None),     # ORB moved to GitHub on 10/21
        "paper_nr7": ("NR7", date(2026, 11, 3)), "paper_vol_breakout": ("Vol Breakout", date(2026, 11, 3))}
REPLAY_NEXT_DAY = ("paper_nr7", "paper_vol_breakout")    # these journal a session the morning after it


def _ts(v) -> datetime | None:
    if not v:
        return None
    return v if isinstance(v, datetime) else datetime.fromisoformat(str(v).replace("Z", "+00:00"))


def _same(a, b) -> bool:
    return abs(float(a) - float(b)) <= PRICE_TOL


def compare_orders(trades: list[dict], orders: list[dict], positions: list[str]) -> dict:
    """trades: the ORB journal rows. orders: Alpaca's top-level orders (legs nested). positions: open symbols."""
    by_id, issues, fills = {o["id"]: o for o in orders}, [], {}
    closers = [o for o in orders if not o.get("legs") and o.get("filled_avg_price")
               and o.get("order_class") in (None, "", "simple")]
    used = set()
    for t in sorted(trades, key=lambda r: r["entry_time"]):
        label = f"{t['symbol']} {t['session_date']}"
        p = by_id.get(t.get("broker_order_id"))
        if not p:
            issues.append({"kind": "no_order", "id": t["id"], "text": f"{label}: journal trade has no matching Alpaca order"})
            continue
        f = {"entry_fill": None, "entry_at": None, "exit_fill": None, "exit_at": None}
        if p.get("filled_avg_price"):
            f["entry_fill"], f["entry_at"] = float(p["filled_avg_price"]), str(p.get("filled_at"))
            if not _same(f["entry_fill"], t["entry"]):
                issues.append({"kind": "entry_mismatch", "id": t["id"], "journal": t["entry"], "alpaca": f["entry_fill"],
                               "text": f"{label}: entry {t['entry']:.2f} in the journal, {f['entry_fill']:.2f} on Alpaca"})
        else:
            issues.append({"kind": "entry_not_filled", "id": t["id"], "text": f"{label}: journal has a trade but Alpaca never filled the order ({p.get('status')})"})
        leg = next((l for l in p.get("legs") or [] if l.get("status") == "filled"), None)
        if leg:
            f["exit_fill"], f["exit_at"] = float(leg["filled_avg_price"]), str(leg.get("filled_at"))
        elif t.get("exit_time"):        # closed by a separate market order
            opp = "sell" if t["direction"] == "long" else "buy"
            when = _ts(t["exit_time"])
            near = [o for o in closers if o["id"] not in used and o["symbol"] == t["symbol"] and o.get("side") == opp
                    and float(o.get("qty") or 0) == t["qty"] and abs((_ts(o["filled_at"]) - when).total_seconds()) < 600]
            if near:
                c = min(near, key=lambda o: abs((_ts(o["filled_at"]) - when).total_seconds()))
                used.add(c["id"])
                f["exit_fill"], f["exit_at"] = float(c["filled_avg_price"]), str(c["filled_at"])
        if t.get("exit") is not None:
            if f["exit_fill"] is None:
                issues.append({"kind": "no_exit_order", "id": t["id"], "text": f"{label}: journal shows an exit but no closing fill on Alpaca"})
            elif not _same(f["exit_fill"], t["exit"]):
                issues.append({"kind": "exit_mismatch", "id": t["id"], "journal": t["exit"], "alpaca": f["exit_fill"],
                               "text": f"{label}: exit {t['exit']:.2f} in the journal, {f['exit_fill']:.2f} on Alpaca"})
        elif f["exit_fill"] is not None:
            issues.append({"kind": "unrecorded_exit", "id": t["id"], "text": f"{label}: Alpaca closed it at {f['exit_fill']:.2f} but the journal still shows it open"})
        fills[t["id"]] = f
    known = {t.get("broker_order_id") for t in trades}
    for o in orders:
        if o["id"] in known or not o.get("filled_avg_price") or o["id"] in used:
            continue
        issues.append({"kind": "not_in_journal", "text": f"{o['symbol']} {str(o.get('filled_at'))[:10]}: {o.get('side')} {o.get('qty')} filled at "
                       f"{float(o['filled_avg_price']):.2f} on Alpaca, not in the journal"})
    open_rows = {t["symbol"] for t in trades if t.get("exit") is None}
    for s in sorted(set(positions) - open_rows):
        issues.append({"kind": "position_not_in_journal", "text": f"{s}: open position on Alpaca, nothing open in the journal"})
    for s in sorted(open_rows - set(positions)):
        issues.append({"kind": "journal_open_no_position", "text": f"{s}: open in the journal, no position on Alpaca"})
    return {"orders_checked": len(orders), "trades_checked": len(trades), "issues": issues, "fills": fills}


def coverage(sessions: dict[str, set], market_days: list[date], today: date) -> dict:
    """sessions: mode -> {(symbol, day string)}. Lists market days a bot should have logged but didn't."""
    out = {}
    done = [d for d in market_days if d < today]
    for mode, (name, start) in BOTS.items():
        days = {d for _, d in sessions.get(mode, set())}
        first = start or (date.fromisoformat(min(days)) if days else None)
        if first is None:
            out[mode] = {"bot": name, "first_day": None, "days_logged": 0, "last_day": None, "missing": []}
            continue
        # a replay bot logs a session the next morning, so the newest finished day may not be in yet
        due = done[:-1] if mode in REPLAY_NEXT_DAY else done
        out[mode] = {"bot": name, "first_day": str(first), "days_logged": len(days), "last_day": max(days) if days else None,
                     "missing": [str(d) for d in due if d >= first and str(d) not in days]}
    return out


def candle_check(trades: list[dict]) -> list[dict]:
    """Exit price must be inside (or within a hair of) the candles around the exit minute."""
    issues = []
    for t in trades:
        if t.get("exit") is None or not t.get("exit_time"):
            continue
        when = _ts(t["exit_time"]).astimezone(ET)
        path = CHARTS / f"{t['symbol']}_{when.date()}.csv"
        if not path.exists():
            continue
        with open(path) as f:
            near = [r for r in csv.DictReader(f) if abs((_ts(r["ts"]) - when).total_seconds()) <= 120]
        if not near:
            continue
        lo, hi = min(float(r["low"]) for r in near), max(float(r["high"]) for r in near)
        tol = t["exit"] * CANDLE_TOL
        if not lo - tol <= t["exit"] <= hi + tol:
            issues.append({"kind": "exit_outside_candle", "id": t["id"], "bot": t["mode"],
                           "text": f"{t['symbol']} {t['session_date']}: exit {t['exit']:.2f} but price traded {lo:.2f}-{hi:.2f} at {when:%m/%d %H:%M}"})
    return issues


def _plain(o) -> dict:
    v = lambda x: getattr(x, "value", x)
    return {"id": str(o.id), "symbol": o.symbol, "side": v(o.side), "qty": str(o.qty), "status": v(o.status),
            "order_class": v(o.order_class), "filled_avg_price": o.filled_avg_price,
            "filled_at": o.filled_at.isoformat() if o.filled_at else None,
            "legs": [{"type": v(l.type), "status": v(l.status), "filled_avg_price": l.filled_avg_price,
                      "filled_at": l.filled_at.isoformat() if l.filled_at else None} for l in (o.legs or [])]}


def alpaca_state(cfg, since: date) -> tuple[list[dict], list[str], list[date]]:
    from alpaca.trading.client import TradingClient
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetCalendarRequest, GetOrdersRequest
    from alpaca.common.enums import Sort
    tc = TradingClient(cfg.api_key, cfg.secret_key, paper=True)
    orders, after, seen = [], datetime.combine(since - timedelta(days=1), datetime.min.time(), timezone.utc), set()
    while True:
        page = tc.get_orders(GetOrdersRequest(status=QueryOrderStatus.ALL, after=after, limit=500, nested=True, direction=Sort.ASC))
        fresh = [o for o in page if str(o.id) not in seen]
        if not fresh:
            break
        seen.update(str(o.id) for o in fresh)
        orders += [_plain(o) for o in fresh]
        after = max(o.submitted_at for o in fresh)
    positions = [p.symbol for p in tc.get_all_positions()]
    today = datetime.now(ET).date()
    cal = tc.get_calendar(GetCalendarRequest(start=since, end=today))
    return orders, positions, [c.date for c in cal]


def main() -> None:
    from orb import config
    c = sqlite3.connect(ROOT / "data" / "journal.db")
    c.row_factory = sqlite3.Row
    all_trades = [dict(r) for r in c.execute("SELECT * FROM trades WHERE mode LIKE 'paper%' ORDER BY entry_time")]
    sessions: dict[str, set] = {}
    for r in c.execute("SELECT mode, symbol, session_date FROM sessions WHERE mode LIKE 'paper%'"):
        sessions.setdefault(r["mode"], set()).add((r["symbol"], r["session_date"]))
    c.close()
    orb = [t for t in all_trades if t["mode"] == "paper"]
    health = {"generated": datetime.now(ET).isoformat(timespec="seconds"), "alpaca": None, "coverage": None,
              "candles": candle_check(all_trades), "journal_only_with_orders": [
                  t["id"] for t in all_trades if t["mode"] != "paper" and t["broker_order_id"]]}
    try:
        cfg = config.load()
        cfg.require_keys()
        orders, positions, market_days = alpaca_state(cfg, date(2026, 10, 20))
        health["alpaca"] = compare_orders(orb, orders, positions)
        health["coverage"] = coverage(sessions, market_days, datetime.now(ET).date())
    except (Exception, SystemExit) as e:      # still save the checks that don't need Alpaca
        health["alpaca_error"] = str(e)[:300]
    OUT.write_text(json.dumps(health, indent=1, default=str))
    a = health["alpaca"]
    print(f"health: {a['trades_checked'] if a else '?'} ORB trades vs {a['orders_checked'] if a else '?'} Alpaca orders, "
          f"{len(a['issues']) if a else '?'} issue(s); {len(health['candles'])} candle issue(s) -> {OUT.relative_to(ROOT)}")
    for i in (a["issues"] if a else []) + health["candles"]:
        print("  " + i["text"])


if __name__ == "__main__":
    main()
