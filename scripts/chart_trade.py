"""Chart one paper trade on 1-min candles: python scripts/chart_trade.py TSLA 2026-10-29 [out.png]
Needs data/charts/<SYM>_<DAY>.csv (scripts/fetch_bars.py, run through GitHub: run-now 'bars SYM DAY')."""
import csv, sqlite3, sys
from datetime import date, datetime, time
from pathlib import Path
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

ROOT = Path(__file__).resolve().parents[1]
sym, day = sys.argv[1].upper(), sys.argv[2]
out = Path(sys.argv[3]) if len(sys.argv) > 3 else ROOT / "reports" / f"chart_{sym}_{day}.png"
rows = [r for r in csv.DictReader(open(ROOT / "data" / "charts" / f"{sym}_{day}.csv"))]
B = [(datetime.fromisoformat(r["ts"]), *(float(r[k]) for k in ("open", "high", "low", "close"))) for r in rows]
B = [b for b in B if time(9, 30) <= b[0].time() < time(16, 0)]
db = sqlite3.connect(ROOT / "data" / "journal.db"); db.row_factory = sqlite3.Row
t = dict(db.execute("SELECT * FROM trades WHERE mode='paper' AND symbol=? AND session_date=?", (sym, day)).fetchone())
idx = {b[0].strftime("%H:%M"): i for i, b in enumerate(B)}
ORH, ORL = t["or_high"], t["or_low"]
E, S, T = t["entry"], t["stop"], t["target"]
long = t["direction"] == "long"
ib = idx[datetime.fromisoformat(t["breakout_time"]).strftime("%H:%M")]
ie = idx.get(datetime.fromisoformat(t["entry_time"]).strftime("%H:%M"), len(B) - 1)
ix = idx.get(datetime.fromisoformat(t["exit_time"]).strftime("%H:%M"), len(B) - 1) if t["exit_time"] else len(B) - 1
log = (ROOT / "data" / "live_log.txt").read_text().splitlines()
fvg = None
for ln in log:
    if f"{sym}: " in ln and "ORB bot:" in ln and "FVG" in ln:
        try:
            fvg = tuple(float(x) for x in ln.split("FVG ")[1].split(" ")[0].split("-"))
        except Exception:
            pass
after = B[ie:ix + 1]
best = max(b[2] for b in after) if long else min(b[3] for b in after)
worst = min(b[3] for b in after) if long else max(b[2] for b in after)
risk = abs(E - S)
last = B[ix]
open_now = t["exit"] is None
cur = last[4] if open_now else t["exit"]
r_now = ((cur - E) if long else (E - cur)) / risk
lab = lambda i: B[i][0].strftime("%-H:%M")

BG, FG, MUT, GRID, UP, DN = "#16181d", "#e6e6e6", "#8a8f98", "#262a31", "#3fb68b", "#e5534b"
fig, ax = plt.subplots(figsize=(14, 7.6), dpi=150)
fig.patch.set_facecolor(BG); ax.set_facecolor(BG)
ax.add_patch(Rectangle((-0.5, ORL), 15, ORH - ORL, color="#5b8def", alpha=0.18, lw=0))
ax.text(7, ORH, f"Opening range\n{ORL:.2f}–{ORH:.2f}", color="#8fb0f5", ha="center", va="bottom", fontsize=9)
if fvg:
    ax.add_patch(Rectangle((ib - 1.5, fvg[0]), ie - ib + 3, fvg[1] - fvg[0], color="#c58af9", alpha=0.3, lw=0))
    ax.text(ib - 2, (fvg[0] + fvg[1]) / 2, "FVG ", color="#c58af9", fontsize=9, va="center", ha="right")
ax.axvspan(ie - 0.5, ix + 0.5, color="#ffffff", alpha=0.035, lw=0)
for i, (ts, o, h, l, c) in enumerate(B):
    col = UP if c >= o else DN
    ax.vlines(i, l, h, color=col, lw=0.8)
    lo, hi = sorted((o, c))
    ax.add_patch(Rectangle((i - 0.35, lo), 0.7, max(hi - lo, 0.01), color=col, lw=0))
x0, x1 = ie - 0.5, len(B) - 0.5
for y, c, txt, ls in [(T, UP, f"Target {T:.2f} (+1.5R)", "--"), (E, "#f0c674", f"Entry {E:.2f}  {t['direction']} {t['qty']} sh", "-"),
                      (S, DN, f"Stop {S:.2f} (−1R)", "--")]:
    ax.hlines(y, x0, x1, color=c, lw=1.5, ls=ls)
    ax.text(x1 + 1, y, txt, color=c, va="center", fontsize=9.5, fontweight="bold")
ax.annotate(f"Breakout {lab(ib)}", (ib, B[ib][4]), (ib - 22, B[ib][4] + (1 if long else -1) * risk * 0.9), color=FG, fontsize=9,
            arrowprops=dict(arrowstyle="->", color=MUT))
ax.scatter([ie], [E], marker="^" if long else "v", s=110, color="#f0c674", zorder=5)
ax.annotate(f"ENTRY {lab(ie)}", (ie, E), (ie + 4, E - (1 if long else -1) * risk * 0.45), color="#f0c674", fontsize=9.5, fontweight="bold",
            arrowprops=dict(arrowstyle="->", color="#f0c674"))
ib_best = max(range(ie, ix + 1), key=lambda i: B[i][2] if long else -B[i][3])
bR = ((best - E) if long else (E - best)) / risk
ax.annotate(f"Best so far {best:.2f} at {lab(ib_best)}\n{bR:+.2f}R", (ib_best, best), (ib_best + 6, best + (1 if long else -1) * risk * 0.35),
            color=UP, fontsize=9.5, arrowprops=dict(arrowstyle="->", color=UP))
status = (f"NOW {cur:.2f} ({lab(ix)})  {r_now:+.2f}R  ≈ ${r_now * risk * t['qty']:+,.0f}" if open_now
          else f"EXIT {t['exit_reason']} {cur:.2f}  {t['r_multiple']:+.2f}R  ${t['pnl']:+,.2f}")
ax.scatter([ix], [cur], s=60, color=FG, zorder=5)
ax.annotate(status, (ix, cur), (ix - 30, cur + (1 if long else -1) * risk * 0.6), color=FG, fontsize=10, fontweight="bold",
            arrowprops=dict(arrowstyle="->", color=MUT))
lo_y = min(min(b[3] for b in B), S, T) - risk * 0.15
hi_y = max(max(b[2] for b in B), S, T) + risk * 0.15
ax.set_ylim(lo_y, hi_y); ax.set_xlim(-1, len(B) + 22)
ticks = [i for i, b in enumerate(B) if b[0].minute in (0, 30)]
ax.set_xticks(ticks); ax.set_xticklabels([lab(i) for i in ticks], color=MUT)
ax.tick_params(colors=MUT); ax.yaxis.tick_right()
ax.grid(color=GRID, lw=0.6); [s_.set_visible(False) for s_ in ax.spines.values()]
title = f"{sym} {t['direction']} · {datetime.fromisoformat(day):%a %m/%d/%Y} · 1-minute · " + ("still open" if open_now else "closed")
ax.set_title(title, color=FG, fontsize=13, loc="left", pad=12)
fig.text(0.01, 0.01, "Candles: Alpaca SIP 1-min (to 16 min ago). Entry = Alpaca fill; levels from the bot's journal.", color=MUT, fontsize=8)
plt.tight_layout(rect=(0, 0.02, 1, 1))
out.parent.mkdir(exist_ok=True)
plt.savefig(out, facecolor=BG)
print(out, f"best {best:.2f} ({bR:+.2f}R) worst {worst:.2f} cur {cur:.2f} ({r_now:+.2f}R) at {lab(ix)}")
