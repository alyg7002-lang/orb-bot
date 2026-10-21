"""Export paper trades to JSON for the ORB Bot Trades page in Claude."""
import json, sqlite3, sys
from pathlib import Path
root = Path(__file__).resolve().parents[1]
out = Path(sys.argv[1]) if len(sys.argv) > 1 else root / "reports" / "paper_trades.json"
c = sqlite3.connect(root / "data" / "journal.db"); c.row_factory = sqlite3.Row
cols = "session_date symbol direction entry_time entry stop target exit_time exit exit_reason qty pnl r_multiple".split()
rows = []
for i, r in enumerate(c.execute("select * from trades where mode='paper' order by entry_time")):
    d = {k: r[k] for k in cols}
    d["trade"] = i + 1
    d["entry_time"] = (d["entry_time"] or "")[11:16]
    d["exit_time"] = (d["exit_time"] or "")[11:16]
    d["status"] = "closed" if r["exit"] is not None else "open"
    for k in ("entry", "stop", "target", "exit"):
        d[k] = round(d[k], 2) if d[k] is not None else None
    rows.append(d)
out.write_text(json.dumps(rows, indent=1))
print(f"{len(rows)} trades -> {out}")
