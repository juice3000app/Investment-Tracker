# Model 2 -- catalyst-trading toolkit

A backtesting and live-tracking tool for the "Model 2" strategy: buy a
Canadian small/micro-cap stock the day before its earnings catalyst, manage
the position with a trailing stop-loss and a stagnation exit, backtest it
without lookahead bias, and check what to do next from anywhere.

See `model3-clean-architecture-brief.md` (the original handoff brief) for
the full strategy rules and product spec this was built from.

## Layout

```
strategy_core/     pure decision logic + data fetching, no UI/web code
  strategy.py         entry/exit rules, screening (as pure functions)
  portfolio.py         point-in-time-safe walk-forward backtest engine
  data_sources.py       price / earnings / shares-outstanding fetching (yfinance)
  dataset.py             save/list/load named datasets; the fetch pipeline

backtest_app/       Tkinter desktop app, run on your own machine
  gui.py               sliders, dataset picker, Fetch vs Run actions
  report.py             auto-generated browser report on every run

live_app/           hosted, always-on, checked from anywhere
  server.py             dashboard: open positions + upcoming catalysts
  daily_job.py            post-close: fetch -> decide -> update -> digest
  state.py                 SQLite-persisted positions + decision log
  notify.py                 SMTP email digest

tests/              pytest suite for strategy_core + live_app (95 tests, no network)
```

Both apps import `strategy_core` -- a rule tuned during backtesting is the
exact same code making live decisions, never a hand-kept-in-sync copy.

## Quick start -- backtesting locally

```bash
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m pytest tests/ -v     # confirms strategy_core is working, no network needed
python -m backtest_app.gui     # opens the desktop app
```

In the app: **Dataset** tab -> pick a universe mode -> set the fetch date
range -> "Fetch new dataset" (hits the network once) -> select it in the
list (auto-selected right after a fetch) -> on the **Backtest Window**
tab, the run's own date range is prefilled from the dataset's full range
but can be narrowed to test a sub-period without re-fetching -> "Run
backtest against selected" (no network). Every run opens a report in your
browser automatically.

Fetching and running have independent date ranges on purpose (problem
5a): fetch a wide range once, then run backtests over as many narrower
windows within it as you like, instantly, with no repeat network calls.
The backtest window must fall inside the dataset's own fetched range --
picking dates outside it produces a clear error telling you the valid
range, rather than a confusing partial result.

`strategy_core/data/README.md` explains the two data-source limitations
worth knowing about before your first real run (auto-sweep universe
coverage, TSX small-cap earnings-date coverage on the free data source).

## Deploying the live app

See `DEPLOYMENT.md` -- Render (free hosting) + cron-job.org (free daily
trigger) + your own email account (SMTP digest). Roughly 20 minutes,
three free signups.

## Open decisions, as confirmed

| Decision | Choice |
|---|---|
| Graphical report display | Auto-opened browser report |
| Live app hosting | Render, free tier |
| Daily digest delivery | Your own email via SMTP |
| Daily job run time | 5:00pm Atlantic time (after TSX close) |
| Order placement | Advisory only -- never places real orders |
| Second strategy (buy-the-dip reversal) | Out of scope for this build |
| Mechanism O add-on trades (O1/O2) | Off by default, toggleable per-run/per-deployment |
| Mechanism O in the live app | Advisory recommendations only, except the O1 merge (pure bookkeeping, auto-performed) |

## Running tests

```bash
source .venv/bin/activate
python -m pytest tests/ -v
```

All 95 tests run against synthetic data -- no network calls, no API keys
needed. They cover the screening filters, position sizing/caps, the
trailing-stop ratchet, the rolling stagnation check (including the
trend-aware reprieve), fee application, the point-in-time no-lookahead
guarantee, full synthetic backtests (entry timing, exit reasons,
unrealized mark-to-market, sector caps, no-rebuy-while-held), Mechanism
O's O1/O2 trigger and exit logic (both as pure functions and wired
through a full backtest -- merge accounting, the disabled-by-default
case, the max-concurrent-positions cap, equity-curve correctness), the
live-app manual-position flows (backdated entries backfilling the
trailing-stop peak from real history, manual close with a custom date/
reason, removing a mistaken entry, and the daily job correctly running
the exit rules against a backfilled peak), and Mechanism O's live-app
wiring (C+1 trigger detection logged as a recommendation without
auto-creating a position, the one-shot nature of that check, the O1
merge performed automatically in state, and O1/O2 exit recommendations
that don't auto-close a position), dataset fetching (each universe
mode raises a clear, mode-specific error if it resolves to zero tickers,
instead of silently saving an empty dataset -- see "Fetching a universe"
below), the results-page report (the run-settings recap covers every
GUI parameter group with the right fields shown or hidden depending on
what's toggled on, and the per-stock charts correctly group a base
position with its Mechanism O add-on into one campaign, keep two separate
campaigns on the same ticker apart, give a standalone O1/O2 trade its own
card when the base strategy is off, and handle both plain-date and
timestamp-indexed price history -- see "The results page" below), the
base-strategy toggle (Mechanism O still triggers off the right reference
price with no base position ever opening, and correctly falls back to a
real closed trade -- never the merge-into-base path -- since there's no
base position to merge into), and the idle cash sweep (sweeps in after
the configured number of idle days and marks the holding to market,
liquidates in full to cover a new position that needs more cash than is
sitting free, and raises a clear error rather than silently doing nothing
if the sweep ticker doesn't match what the dataset actually fetched --
see "Idle cash sweep" below), and auto-sweep universe resolution (a
populated `tsx_tickers.csv` where every ticker fails to fetch, or where
none land in the market-cap band, each get their own distinct error
message rather than looking identical to the file being empty -- see
"Fetching a universe" below).

## Fetching a universe, and the Dataset/Backtest Window split

Fetching (network) and running (no network) have their own, independent
date ranges -- fetch a wide range once on the **Dataset** tab, then run
backtests over as many narrower windows within it as you like on the
**Backtest Window** tab, instantly. Selecting a dataset (or finishing a
fetch) prefills the backtest window with that dataset's full range; narrow
it from there. A backtest window that falls outside the selected
dataset's own fetched range is rejected with a clear error naming the
valid range, rather than silently running over less data than you asked
for.

If a universe mode resolves to zero tickers -- most commonly **S&P/TSX
Composite constituents** when something blocks the live Wikipedia lookup
(a corporate firewall is the usual culprit) -- fetching now fails loudly
with a mode-specific message instead of quietly saving an empty, useless
dataset. The offline backup list for that mode
(`strategy_core/data/tsx_composite_snapshot.csv`) ships with a real
~220-ticker snapshot rather than an empty stub, so a blocked live lookup
still falls back to something usable.

**Auto-sweep** (screens `strategy_core/data/tsx_tickers.csv`) used to give
this same "zero tickers" message no matter *why* it came back empty --
including once you'd correctly populated the file, which made a real
problem look identical to the file simply being empty. It now tells you
exactly what happened: every ticker failed to fetch from Yahoo Finance
(almost always a network/firewall block, or a ticker missing its `.TO`
suffix), or every ticker fetched fine but none fell inside the market-cap
floor/ceiling on the Numeric Screen tab, or the file itself is missing a
`ticker`/`symbol` column or has no rows under it.

## The results page

Every backtest run auto-opens a self-contained HTML report with:

- **Run settings** -- a recap at the top of every parameter/slider value
  used for that run, grouped the same way as the GUI tabs (Backtest,
  Numeric screen, Entry, Exit, Mechanism O -- O1, Mechanism O -- O2, Fees
  & idle cash sweep). A group's optional detail rows (O1/O2 sub-params,
  the trend-aware reprieve window, the idle-cash sweep ticker) only show
  up when that feature was actually turned on for the run, so the recap
  doesn't clutter itself with settings that had no effect.
- **Equity curve** -- the overall portfolio performance chart, as before.
- **Per-stock charts** -- a collapsible card per stock traded, with its own
  price chart and color-coded vertical markers for the catalyst date, the
  entry date, any Mechanism O add-on buy, and the exit (or "still open" if
  the run ended before it closed). A base position and its linked add-on
  lot share one card; if the same ticker was traded twice independently,
  each trade gets its own card. With the base strategy off (see below), a
  standalone O1/O2 trade gets its own card too, labeled "O1/O2 only (base
  strategy off)" instead of being dropped. A ticker filter and
  expand/collapse-all controls sit above the cards for runs with a lot of
  tickers.
- **Idle cash sweep** -- if the sweep was enabled, a table of every sweep
  buy/sell (date, shares, price, amount, fees) with a one-line summary;
  says plainly when it was enabled but nothing ever sat idle long enough
  to trigger one. Omitted entirely when the sweep is off.
- **Trade log** -- the sortable/filterable table, as before.

## Backtesting Mechanism O on its own

The **Entry** tab's "Enable base strategy" checkbox (on by default) turns
off the base "buy the day before the catalyst" position entirely -- no
base capital is ever deployed, and the base position's stop-loss/
stagnation rules never run. Mechanism O still triggers at C+1 off the same
C-1 reference price a base entry would have used, so you can backtest O1
or O2 completely on their own. One behavioral difference worth knowing:
without a base position, O1 can never take its "merge into base" exit --
whenever it would have merged (the stock's on an uptrend at the exit
checkpoint), it instead becomes a real, closed, P&L-bearing trade tagged
`o1_timed_exit_no_base_to_merge` so it's clearly distinguishable in the
trade log from an ordinary timed exit.

## Idle cash sweep

Off by default (**Fees & Idle Cash Sweep** tab). When enabled, cash that's
sat unused for "Days idle before sweeping" days in a row gets bought into
the sweep ticker (fetched alongside your dataset, same mechanism as the
benchmark-return comparison); it's sold back in full the moment a new
position needs more cash than is currently sitting free. The swept
holding is marked to market in the equity curve and counts as real
capital when sizing the next position, so capital parked in the sweep
ticker isn't invisible to the strategy's own position sizing. Changing the
sweep ticker only takes effect on your next fetch (Dataset tab) -- it
needs its own price history, fetched the same way the benchmark ticker
always has been. If the sweep ticker configured for a run doesn't match
what the selected dataset actually fetched (e.g. you changed it after
fetching), the run fails with a clear error naming both tickers rather
than silently sweeping nothing.

## Mechanism O: add-on purchases on catalyst overreactions

Off by default. Two independently toggleable add-on mechanisms, each
with its own parameters (backtest: the "Mechanism O" GUI tab; live app:
the `PARAM_ENABLE_O1_DIP_BUY` / `PARAM_ENABLE_O2_MOMENTUM_BUY` and
related env vars in `DEPLOYMENT.md`):

- **O1 (dip-buy):** at the close of C+1 (the day after the catalyst), if
  the stock has declined `o1_decline_threshold_pct` or more from the
  base position's entry price, buy a separate `o1_position_size_pct` lot.
  Exit it `o1_exit_duration_days` later -- unless, at that exact
  checkpoint, the stock is on an `o1_exit_duration_days`-day uptrend, in
  which case the add-on lot is folded into the base position (blended
  cost basis) and from then on follows the base position's own
  stagnation/stop-loss rules. If the base position already closed before
  the checkpoint arrives, there's nothing to merge into, so it falls back
  to a timed exit regardless of trend.
- **O2 (momentum-buy):** at the close of C+1, if the stock has instead
  risen `o2_increase_threshold_pct` or more, buy a separate
  `o2_position_size_pct` lot and exit it on the first down day.

Both mechanisms respect the strategy's existing `max_concurrent_positions`
cap and only ever fire once per base position (the C+1 check is one-shot,
win or lose). In the live app both are advisory: a C+1 trigger and an
add-on exit are digest recommendations you act on and then record through
the dashboard, exactly like a new base candidate or a base position's
exit. The one exception is an O1 merge -- since it moves no cash and only
continues tracking lots you already confirmed you own, the daily job
performs that bookkeeping automatically.

**If you deployed an earlier version of the live app:** its state DB used
`ticker` as the primary key and can't hold an add-on lot alongside a base
position for the same ticker. Delete the old state DB file before your
first run of this version -- there's no in-place migration (see the note
at the top of `live_app/state.py`).

## Live app: previous purchases and manual exits

The dashboard's "Add a position" form works for both a brand-new entry and
a previous purchase you're only now recording -- just set the entry date
to when you actually bought it. The daily job runs the exact same exit
rules (trailing stop, stagnation) on every open position regardless of how
it was added. One detail that matters for backdated entries: the trailing-
stop peak is backfilled from the real price history since your entry date,
not reset to your entry price -- otherwise a position that already ran up
(or already dropped) before you got around to recording it would trail
from the wrong starting point.

To exit a position for a reason the strategy itself didn't catch (a
catalyst you found some other way, or any other reason), use "Mark closed"
on the dashboard -- it takes an exit date, exit price, and a free-text
reason, and the position moves to the "Position history" section with
that reason recorded. "Remove" is separate and deletes a position
entirely -- use it only to undo a mistaken entry, not to record a real
exit (which "Mark closed" handles and keeps in history).

If you acted on a Mechanism O "add-on buy opportunity" from the digest,
record it with "Record a Mechanism O add-on lot" -- pick the base
position it belongs to (ticker and catalyst date are inherited
automatically) and it appears indented under that base position in "Open
positions." A recent history of Mechanism O recommendations and the
merges the job performed automatically shows in "Mechanism O activity."
