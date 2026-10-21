# ORB Retest Bot

> **Runs in the cloud on GitHub Actions** (free, public repo). Every weekday:
> - **~8:50 ET morning run:** waits for the open, takes entries until 11:35 ET, then exits. Alpaca keeps each trade's stop and target working on its own servers.
> - **~14:50 ET close-out run:** waits until 15:55 ET, records stop/target fills and closes anything still open at 15:55 ET.
> - After each run the trade journal (`data/journal.db`), log (`data/live_log.txt`) and dashboard (`reports/dashboard.html`) are committed back to the repo.
>
> Your laptop doesn't need to be on. Alpaca keys live in **Settings → Secrets and variables → Actions** as `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` (never in the code).
> To run a job by hand: **Actions → ORB paper bot → Run workflow**. To pause the bot: **Actions → ORB paper bot → ⋯ → Disable workflow**.
> GitHub's scheduler often starts runs 15–30 minutes late (the 10/21 close-out started 25 min late), so both runs are scheduled early and wait.

Paper-trades your opening range breakout + retest strategy on SPY, QQQ, NVDA, TSLA and AAPL
(the five tickers with the most options volume). It backtests on real history, paper trades
live on an Alpaca paper account, and keeps every trade and every day's setup in its own
journal with a stats dashboard.

**No real money. Ever.** The bot refuses to run if `paper = false`.

---

## How it works (the 30-second version)

```
Alpaca 5-min candles ──▶ strategy.py (the rules) ──▶ journal.db ──▶ dashboard.html
                              ▲                          ▲
        backtest.py (history) ┘    live.py (today) ──────┘──▶ bracket orders on Alpaca paper
```

The key design choice: **the backtest and live bot share one rules engine** (`orb/strategy.py`).
It eats one candle at a time and doesn't know whether that candle is from last March or from
five seconds ago. So if live results drift from backtest results, the cause is the market
(or fills), not two different versions of your rules.

### The rules (all adjustable in `config.toml`)
1. **Range**: high/low of 9:30–9:45 ET.
2. **Breakout**: a 5-min candle *closes* outside the range. Wicks don't count.
3. **Retest**: price comes back to the range edge (within 10% of the range width).
4. **Confirm**: a candle closes back in the breakout direction *and* is the right color
   (green for longs, red for shorts). Entry = that close.
5. **Stop**: back inside the range. Midpoint by default. Set `stop_mode = "opposite"` for the far side.
6. **Target**: nearest key level at least 1R away. Candidates are prior day high/low and
   premarket high/low. If none qualifies, the target is 2R.
7. **Exit**: stop, target, or flat at 15:55. One trade per ticker per day, and no entries after 11:30.

If the stop and target both get hit in the same candle, the backtest assumes the **stop hit first**.
That makes results slightly worse than reality on purpose. Backtests lie optimistically, so this
one leans the other way.

### "Compliance" table
Every day, every ticker gets an outcome: `traded`, `no_retest` (broke out and ran without you),
`invalidated` (fake breakout), or `no_breakout`. That table answers your real question:
*which of these five actually respect the pattern, and how often?*

---

## Setup on your Windows laptop (about 15 minutes)

**1. Install Python 3.12+** from python.org. On the first installer screen, check
**"Add python.exe to PATH"**. Then, in a terminal inside this folder:
```
pip install -r requirements.txt
```

**2. Get free Alpaca paper keys.** Sign up at alpaca.markets, switch to **Paper Trading**,
and generate API keys. Save them as environment variables (not in the code):
```
setx ALPACA_API_KEY "your key"
setx ALPACA_SECRET_KEY "your secret"
```
Close and reopen the terminal afterward. Never paste these into chat or commit them to GitHub.

**3. Smoke test with fake data** (no keys needed):
```
python -m orb demo
python -m unittest discover -s tests -t .
```
The demo should show roughly zero or negative results, because it's a random walk.
If it ever shows a big edge on fake data, something is broken.

**4. Backtest on real history:**
```
python -m orb backtest              # last 365 days, all 5 tickers
python -m orb backtest --days 90 --symbols SPY QQQ
```

**5. Paper trade live.** Start it before 9:45 ET:
```
python -m orb live                  # sends bracket orders to Alpaca paper
python -m orb live --no-broker      # just watches and simulates, no orders
```
Leave the window open. It stops itself after the close. Each trade is a **bracket order**, so
Alpaca holds your stop and target server-side. If your laptop sleeps, the trade is still protected.
The bot won't take a stale signal it missed while offline.

**6. Run it automatically every weekday:** Task Scheduler → Create Basic Task → Daily, 9:20 AM →
Start a program → `run_live.bat` in this folder. Then edit the task's trigger and select weekdays only.
Set your laptop to stay awake while plugged in.

**7. Look at results anytime:**
```
python -m orb report                # opens reports/dashboard.html
python -m orb export                # reports/trade_log_paper.csv, your written trade log
```

---

## Honest limits

- **IEX data (free live feed) is about 2–3% of total volume.** The candle shapes are close to
  the real tape but not identical, so a live setup can occasionally differ from what TradingView
  shows. Backtests use full SIP data.
- **Paper fills are optimistic.** Real fills have more slippage, especially on TSLA and NVDA.
- **This is stocks, not MES.** These tickers track the same market you'd trade with MES (SPY ≈ /ES),
  but treat the results as research on the pattern, not proof that MES will pay.
- **Under 30 trades means nothing.** The dashboard warns you. Under 100 is still noisy.
- **Don't curve-fit.** If you tweak settings until the backtest looks amazing, you've fit the past,
  not found an edge. Change one thing at a time, then confirm it on paper going forward.

## Files

| File | What it does |
|---|---|
| `orb/strategy.py` | The rules. Read this one first. |
| `orb/backtest.py` | Replays history through the rules |
| `orb/live.py` | Live loop + Alpaca bracket orders |
| `orb/journal.py` | SQLite journal (`data/journal.db`) |
| `orb/stats.py` | Win rate, expectancy, profit factor, drawdown, compliance |
| `orb/report.py` | Builds the dashboard |
| `config.toml` | Every knob |
| `tests/` | 26 tests; run them after any change |
