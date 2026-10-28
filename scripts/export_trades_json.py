"""Export every paper trade (ORB bot + RSI(2) bot) to JSON for the 'ORB Bot Trades' page in Claude."""
import json, sqlite3, sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
out = Path(sys.argv[1]) if len(sys.argv) > 1 else root / "reports" / "paper_trades.json"
c = sqlite3.connect(root / "data" / "journal.db")
c.row_factory = sqlite3.Row
BOTS = [("paper", "orb", "ORB"), ("paper_rsi2", "rsi2", "RSI(2)")]


def stamp(s):
    return f"{s[5:7]}/{s[8:10]} {s[11:16]}" if s else ""


rows = []
for mode, key, label in BOTS:
    for i, r in enumerate(c.execute("SELECT * FROM trades WHERE mode=? ORDER BY entry_time", (mode,))):
        rows.append({
            "trade": f"{label} #{i + 1}", "strategy": key, "bot": label,
            "session_date": r["session_date"], "symbol": r["symbol"], "direction": r["direction"],
            "entry_time": stamp(r["entry_time"]), "exit_time": stamp(r["exit_time"]),
            "sort_time": r["exit_time"] or r["entry_time"],
            "entry": round(r["entry"], 2), "stop": round(r["stop"], 2) if r["stop"] is not None else None,
            "target": round(r["target"], 2) if r["target"] is not None else None,
            "exit": round(r["exit"], 2) if r["exit"] is not None else None,
            "exit_reason": r["exit_reason"], "qty": r["qty"], "pnl": r["pnl"], "r_multiple": r["r_multiple"],
            "status": "closed" if r["exit"] is not None else "open",
        })
out.write_text(json.dumps(rows, indent=1))
print(f"{len(rows)} trades -> {out}")
