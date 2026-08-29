"""
strategy_core/portfolio.py

The point-in-time-safe walk-forward backtest engine (spec section 3).

The engine walks forward one trading day at a time across a chosen date
window. On each day, `strategy.py`'s functions are given price history
*only up to and including that day* -- the `PointInTimePrices` wrapper
below is the thing that enforces this: it slices before handing data to
the strategy, so the strategy cannot reach past the current day even by
a bug, not merely by convention.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

import pandas as pd

from . import strategy as strat


# --------------------------------------------------------------------------- #
# Point-in-time data access
# --------------------------------------------------------------------------- #


class PointInTimePrices:
    """Wraps one ticker's full price history but only ever exposes rows up
    to and including a given date. This is the structural no-lookahead
    guarantee: callers get a slice, never the full frame."""

    def __init__(self, full_history: pd.DataFrame):
        if not full_history.index.is_monotonic_increasing:
            full_history = full_history.sort_index()
        self._full = full_history

    def as_of(self, as_of_date: date) -> pd.DataFrame:
        """Everything up to and including `as_of_date`. Never anything after."""
        return self._full.loc[self._full.index <= as_of_date]

    def close_on(self, d: date) -> Optional[float]:
        if d in self._full.index:
            return float(self._full.loc[d, "Close"])
        return None

    def last_known_close_on_or_before(self, d: date) -> Optional[float]:
        window = self.as_of(d)
        if window.empty:
            return None
        return float(window["Close"].iloc[-1])

    def trading_day_before(self, d: date) -> Optional[date]:
        """The last trading day strictly before `d` in this ticker's own
        calendar -- used to find the entry day (close of the day
        immediately before the catalyst)."""
        prior = self._full.index[self._full.index < d]
        if len(prior) == 0:
            return None
        return prior[-1]

    def trading_day_after(self, d: date) -> Optional[date]:
        """The first trading day strictly after `d` in this ticker's own
        calendar -- used to find C+1 (the day after the catalyst) for
        Mechanism O's add-on trigger check."""
        after = self._full.index[self._full.index > d]
        if len(after) == 0:
            return None
        return after[0]

    @property
    def dates(self):
        return self._full.index

    @property
    def full_history(self) -> pd.DataFrame:
        """The complete OHLCV frame, unsliced -- for POST-HOC use only
        (e.g. drawing a price chart after a run has finished). The
        backtest engine itself must never call this; every point-in-time
        decision goes through as_of()/close_on()/etc. above, which is
        what actually enforces no-lookahead."""
        return self._full


# --------------------------------------------------------------------------- #
# Inputs / outputs
# --------------------------------------------------------------------------- #


@dataclass
class TickerData:
    ticker: str
    prices: PointInTimePrices
    catalyst_dates: list  # list[date] -- known historical earnings dates
    shares_outstanding: float
    sector: Optional[str] = None


@dataclass
class Trade:
    ticker: str
    entry_date: date
    entry_price: float
    shares: float
    catalyst_date: date
    exit_date: Optional[date]
    exit_price: Optional[float]
    exit_reason: Optional[str]  # see strategy.ExitReason for the full set
    fees_paid: float
    pnl: Optional[float] = None
    pnl_pct: Optional[float] = None
    unrealized: bool = False
    lot_type: str = "base"  # 'base' | 'o1' | 'o2' -- which rules govern this lot
    merged_into_base: bool = False  # True for an O1 lot folded into its base position (not a real sale)


@dataclass
class SweepEvent:
    """One idle-cash-sweep trade: cash moved into (or back out of) the
    sweep ticker. Not a `Trade` -- it's cash management, not a strategy
    position -- but tracked the same way so the report can show it."""

    date: date
    action: str  # 'buy' | 'sell'
    ticker: str
    shares: float
    price: float
    amount: float  # gross dollar amount of the trade, before fees
    fees: float


@dataclass
class BacktestResult:
    equity_curve: pd.DataFrame  # columns: date, cash, positions_value, total_value
    trades: list[Trade]
    position_daily_log: pd.DataFrame  # columns: ticker, date, price, shares, market_value
    candidates_screened: int
    candidates_passed: int
    starting_cash: float
    ending_value: float
    benchmark_return_pct: Optional[float] = None
    sweep_events: list[SweepEvent] = field(default_factory=list)

    @property
    def total_return_pct(self) -> float:
        if self.starting_cash == 0:
            return 0.0
        return (self.ending_value - self.starting_cash) / self.starting_cash * 100.0

    def to_trade_log_df(self) -> pd.DataFrame:
        rows = []
        for t in self.trades:
            rows.append(
                {
                    "ticker": t.ticker,
                    "entry_date": t.entry_date,
                    "entry_price": t.entry_price,
                    "shares": t.shares,
                    "catalyst_date": t.catalyst_date,
                    "exit_date": t.exit_date,
                    "exit_price": t.exit_price,
                    "exit_reason": t.exit_reason,
                    "fees_paid": t.fees_paid,
                    "pnl": t.pnl,
                    "pnl_pct": t.pnl_pct,
                    "unrealized": t.unrealized,
                    "lot_type": t.lot_type,
                    "merged_into_base": t.merged_into_base,
                }
            )
        return pd.DataFrame(rows)


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #


def _build_entry_schedule(tickers: list[TickerData], start_date: date, end_date: date):
    """Map entry_date -> list of (ticker_data, catalyst_date) candidates.
    Entry day = the trading day immediately before the catalyst, found in
    that ticker's own price calendar (never assumed to be calendar_day - 1)."""
    schedule: dict[date, list] = {}
    for td in tickers:
        for cdate in td.catalyst_dates:
            if not (start_date <= cdate <= end_date):
                continue
            entry_day = td.prices.trading_day_before(cdate)
            if entry_day is None or entry_day < start_date:
                continue
            schedule.setdefault(entry_day, []).append((td, cdate))
    return schedule


def _build_trading_calendar(tickers: list[TickerData], start_date: date, end_date: date):
    all_dates = set()
    for td in tickers:
        idx = td.prices.dates
        all_dates.update(d for d in idx if start_date <= d <= end_date)
    return sorted(all_dates)


@dataclass
class _AddonReference:
    """Stands in for a base position's entry_price/catalyst_date/sector
    when `enable_base_strategy=False` -- Mechanism O still needs a C-1
    reference price to trigger against even though no base capital is
    ever deployed. Same C+1-trigger and exit logic runs off of this as
    would run off a real open base PositionState; `evaluate_o1_addon_exit`
    is simply always told `base_open=False`, which it already handles
    (falls back to a timed exit -- see `o1_timed_exit_no_base_to_merge`)."""

    entry_price: float
    catalyst_date: date
    sector: Optional[str]
    addon_evaluated: bool = False


def _sweep_mark_value(sweep_shares: float, sweep_ticker: Optional["TickerData"], day: date) -> float:
    """Mark-to-market value of the current idle-cash-sweep holding. Used
    everywhere "total capital" is computed for position sizing -- without
    this, capital swept into the ETF would vanish from the strategy's own
    view of how much it has to work with the moment it's swept."""
    if sweep_shares <= 0 or sweep_ticker is None:
        return 0.0
    price = sweep_ticker.prices.last_known_close_on_or_before(day)
    return sweep_shares * price if price else 0.0


def _liquidate_sweep_for_shortfall(
    cash: float,
    sweep_shares: float,
    sweep_ticker: Optional["TickerData"],
    day: date,
    params: strat.StrategyParams,
    sweep_events: list,
) -> tuple[float, float]:
    """Sells the ENTIRE idle-cash-sweep holding (never a partial sale) to
    raise cash for a trade that's short. Selling in full rather than
    exactly enough keeps the accounting simple and avoids fee-rounding
    edge cases; any cash left over just sits until it's idle long enough
    to be swept again."""
    if sweep_shares <= 0 or sweep_ticker is None:
        return cash, sweep_shares
    price = sweep_ticker.prices.last_known_close_on_or_before(day)
    if not price or price <= 0:
        return cash, sweep_shares
    gross = sweep_shares * price
    fees = strat.compute_trade_cost(gross, params)
    sweep_events.append(
        SweepEvent(date=day, action="sell", ticker=sweep_ticker.ticker, shares=sweep_shares, price=price, amount=gross, fees=fees)
    )
    cash += gross - fees
    return cash, 0.0


def run_backtest(
    tickers: list[TickerData],
    params: strat.StrategyParams,
    start_date: date,
    end_date: date,
    starting_cash: float = 10_000.0,
    benchmark: Optional[TickerData] = None,
) -> BacktestResult:
    """Runs one full backtest. Nothing here reaches out to the network --
    all price/catalyst/shares data must already be loaded into `tickers`
    (see dataset.py / data_sources.py)."""

    if params.idle_cash_sweep_enabled:
        if benchmark is None:
            raise ValueError(
                f"Idle cash sweep is enabled (into '{params.idle_cash_sweep_ticker}'), but no price history for "
                "it was fetched with this dataset -- re-fetch the dataset, or disable idle cash sweep for this run."
            )
        if benchmark.ticker != params.idle_cash_sweep_ticker:
            raise ValueError(
                f"Idle cash sweep is set to '{params.idle_cash_sweep_ticker}', but this dataset's fetched "
                f"index/benchmark ticker is '{benchmark.ticker}' -- re-fetch the dataset after changing the sweep "
                f"ticker (Fees & Idle Cash Sweep tab), or set it back to '{benchmark.ticker}'."
            )

    cash = starting_cash
    open_positions: dict[str, strat.PositionState] = {}
    open_addon_positions: dict[str, strat.PositionState] = {}  # at most one per ticker (O1 xor O2)
    pending_addon_refs: dict[str, _AddonReference] = {}  # only used when enable_base_strategy=False
    by_ticker: dict[str, TickerData] = {td.ticker: td for td in tickers}
    trades: list[Trade] = []
    open_trade_by_ticker: dict[str, Trade] = {}
    addon_trade_by_ticker: dict[str, Trade] = {}

    sweep_shares = 0.0
    idle_days_counter = 0
    sweep_events: list[SweepEvent] = []

    entry_schedule = _build_entry_schedule(tickers, start_date, end_date)
    calendar = _build_trading_calendar(tickers, start_date, end_date)

    equity_rows = []
    position_daily_rows = []
    candidates_screened = 0
    candidates_passed = 0

    for day in calendar:
        # --- 1. Evaluate exits for every currently open BASE position --- #
        for ticker in list(open_positions.keys()):
            td = by_ticker[ticker]
            price = td.prices.close_on(day)
            if price is None:
                continue  # no trade that day for this ticker; carry position forward

            position = open_positions[ticker]
            history_slice = td.prices.as_of(day)
            decision = strat.evaluate_exit(position, day, price, history_slice, params)

            position_daily_rows.append(
                {
                    "ticker": ticker,
                    "date": day,
                    "price": price,
                    "shares": position.shares,
                    "market_value": position.shares * price,
                    "lot_type": position.lot_type,
                }
            )

            if decision.should_exit:
                trade_value = position.shares * price
                fees = strat.compute_trade_cost(trade_value, params)
                cash += trade_value - fees
                idle_days_counter = 0

                t = open_trade_by_ticker[ticker]
                t.exit_date = day
                t.exit_price = price
                t.exit_reason = decision.reason
                t.fees_paid += fees
                t.pnl = (price - t.entry_price) * t.shares - t.fees_paid
                t.pnl_pct = (price - t.entry_price) / t.entry_price * 100.0

                del open_positions[ticker]
                del open_trade_by_ticker[ticker]

        # --- 1b. Evaluate exits/merges for every open Mechanism O add-on lot --- #
        # Runs AFTER base exits so a same-day O1 merge check correctly sees
        # whether its base position is still open (base exits are resolved
        # for the day before any merge is attempted).
        for ticker in list(open_addon_positions.keys()):
            td = by_ticker[ticker]
            price = td.prices.close_on(day)
            if price is None:
                continue

            addon = open_addon_positions[ticker]
            history_slice = td.prices.as_of(day)
            addon_trade = addon_trade_by_ticker[ticker]

            position_daily_rows.append(
                {
                    "ticker": ticker, "date": day, "price": price,
                    "shares": addon.shares, "market_value": addon.shares * price,
                    "lot_type": addon.lot_type,
                }
            )

            if addon.lot_type == "o1":
                base_open = ticker in open_positions
                decision = strat.evaluate_o1_addon_exit(addon, day, price, history_slice, params, base_open)

                if decision.action == "hold":
                    continue

                if decision.action == "merge":
                    base_position = open_positions[ticker]
                    base_trade = open_trade_by_ticker[ticker]
                    total_shares = base_position.shares + addon.shares
                    # Blended cost basis -- the base trade's eventual real
                    # exit will use this combined shares/entry_price, so
                    # the merged position's P&L comes out correct even
                    # though this event itself moves no cash. peak_price
                    # needs no adjustment: it already ratcheted to today's
                    # price (if it's a new high) in step 1 above, on the
                    # SAME underlying ticker series the add-on shares this.
                    blended_entry_price = (
                        base_trade.entry_price * base_position.shares + addon_trade.entry_price * addon.shares
                    ) / total_shares
                    base_trade.entry_price = blended_entry_price
                    base_trade.shares = total_shares
                    base_position.entry_price = blended_entry_price
                    base_position.shares = total_shares

                    addon_trade.exit_date = day
                    addon_trade.exit_price = price
                    addon_trade.exit_reason = "merged_into_base"
                    addon_trade.merged_into_base = True
                    addon_trade.pnl = None
                    addon_trade.pnl_pct = None

                    del open_addon_positions[ticker]
                    del addon_trade_by_ticker[ticker]
                    continue

                # timed_exit (with or without a base to have merged into)
                trade_value = addon.shares * price
                fees = strat.compute_trade_cost(trade_value, params)
                cash += trade_value - fees
                idle_days_counter = 0
                addon_trade.exit_date = day
                addon_trade.exit_price = price
                addon_trade.exit_reason = decision.reason
                addon_trade.fees_paid += fees
                addon_trade.pnl = (price - addon_trade.entry_price) * addon_trade.shares - addon_trade.fees_paid
                addon_trade.pnl_pct = (price - addon_trade.entry_price) / addon_trade.entry_price * 100.0
                del open_addon_positions[ticker]
                del addon_trade_by_ticker[ticker]

            elif addon.lot_type == "o2":
                should_exit = strat.evaluate_o2_addon_exit(addon, day, price, history_slice)
                if not should_exit:
                    continue
                trade_value = addon.shares * price
                fees = strat.compute_trade_cost(trade_value, params)
                cash += trade_value - fees
                idle_days_counter = 0
                addon_trade.exit_date = day
                addon_trade.exit_price = price
                addon_trade.exit_reason = "o2_first_down_day"
                addon_trade.fees_paid += fees
                addon_trade.pnl = (price - addon_trade.entry_price) * addon_trade.shares - addon_trade.fees_paid
                addon_trade.pnl_pct = (price - addon_trade.entry_price) / addon_trade.entry_price * 100.0
                del open_addon_positions[ticker]
                del addon_trade_by_ticker[ticker]

        # --- 2. Evaluate entries scheduled for today --- #
        for td, cdate in entry_schedule.get(day, []):
            if td.ticker in open_positions or td.ticker in open_addon_positions or td.ticker in pending_addon_refs:
                continue  # already holding (or an add-on lot/reference is still pending) -- never re-buy

            candidates_screened += 1
            history_slice = td.prices.as_of(day)
            screen = strat.evaluate_candidate_screen(history_slice, td.shares_outstanding, params)
            if not screen.passed:
                continue

            allowed, _reason = strat.can_open_new_position(
                list(open_positions.values()), td.ticker, td.sector, params
            )
            if not allowed:
                continue

            entry_price = td.prices.close_on(day)
            if entry_price is None or entry_price <= 0:
                continue

            if not params.enable_base_strategy:
                # Base strategy is off -- no capital deployed and no Trade
                # opened, but Mechanism O still needs this catalyst's C-1
                # reference price to evaluate its own C+1 trigger against
                # (see _AddonReference).
                candidates_passed += 1
                pending_addon_refs[td.ticker] = _AddonReference(
                    entry_price=entry_price, catalyst_date=cdate, sector=td.sector,
                )
                continue

            total_capital = (
                cash + _positions_value(open_positions, by_ticker, day)
                + _positions_value(open_addon_positions, by_ticker, day)
                + _sweep_mark_value(sweep_shares, benchmark, day)
            )
            target_value = strat.compute_position_size(total_capital, params)
            fees = strat.compute_trade_cost(target_value, params)
            spend = target_value + fees
            if spend > cash and params.idle_cash_sweep_enabled:
                cash, sweep_shares = _liquidate_sweep_for_shortfall(cash, sweep_shares, benchmark, day, params, sweep_events)
                idle_days_counter = 0
            if spend > cash:
                # Not enough cash to fully fund + cover fees -- skip rather
                # than partially fill (keeps sizing exact and predictable).
                continue

            shares = target_value / entry_price
            cash -= spend
            idle_days_counter = 0

            candidates_passed += 1
            position = strat.PositionState(
                ticker=td.ticker,
                entry_date=day,
                entry_price=entry_price,
                shares=shares,
                catalyst_date=cdate,
                sector=td.sector,
                peak_price=entry_price,
            )
            open_positions[td.ticker] = position

            trade = Trade(
                ticker=td.ticker,
                entry_date=day,
                entry_price=entry_price,
                shares=shares,
                catalyst_date=cdate,
                exit_date=None,
                exit_price=None,
                exit_reason=None,
                fees_paid=fees,
            )
            trades.append(trade)
            open_trade_by_ticker[td.ticker] = trade

        # --- 2b. Mechanism O: evaluate the C+1 add-on trigger --- #
        # Normally off of open BASE positions that haven't been checked
        # yet and whose C+1 is today. When the base strategy itself is
        # off, runs the identical trigger/sizing/exit logic off of the
        # pending_addon_refs recorded in step 2 above instead -- letting
        # O1/O2 be backtested in isolation, with no base position ever
        # opening. The two loops are kept separate (rather than unified
        # over one generic "reference" type) so the well-exercised
        # base-enabled path is untouched by this addition.
        if params.enable_base_strategy:
            for ticker, position in list(open_positions.items()):
                if position.addon_evaluated:
                    continue
                td = by_ticker[ticker]
                c_plus_1 = td.prices.trading_day_after(position.catalyst_date)
                if c_plus_1 != day:
                    continue
                position.addon_evaluated = True  # only ever evaluated once, regardless of outcome

                c_plus_1_price = td.prices.close_on(day)
                if c_plus_1_price is None:
                    continue
                trigger = strat.evaluate_addon_trigger(position.entry_price, c_plus_1_price, params)
                if trigger is None:
                    continue
                if ticker in open_addon_positions:
                    continue  # defensive -- shouldn't happen given addon_evaluated gating

                total_lots = len(open_positions) + len(open_addon_positions)
                if total_lots >= params.max_concurrent_positions:
                    continue  # add-on capital still respects the concurrent-positions cap

                size_pct = params.o1_position_size_pct if trigger == "o1" else params.o2_position_size_pct
                total_capital = (
                    cash + _positions_value(open_positions, by_ticker, day)
                    + _positions_value(open_addon_positions, by_ticker, day)
                    + _sweep_mark_value(sweep_shares, benchmark, day)
                )
                target_value = total_capital * size_pct
                fees = strat.compute_trade_cost(target_value, params)
                spend = target_value + fees
                if spend > cash and params.idle_cash_sweep_enabled:
                    cash, sweep_shares = _liquidate_sweep_for_shortfall(cash, sweep_shares, benchmark, day, params, sweep_events)
                    idle_days_counter = 0
                if spend > cash:
                    continue

                addon_shares = target_value / c_plus_1_price
                cash -= spend
                idle_days_counter = 0

                addon_position = strat.PositionState(
                    ticker=ticker,
                    entry_date=day,
                    entry_price=c_plus_1_price,
                    shares=addon_shares,
                    catalyst_date=position.catalyst_date,
                    sector=td.sector,
                    peak_price=c_plus_1_price,
                    lot_type=trigger,
                )
                open_addon_positions[ticker] = addon_position

                addon_trade = Trade(
                    ticker=ticker,
                    entry_date=day,
                    entry_price=c_plus_1_price,
                    shares=addon_shares,
                    catalyst_date=position.catalyst_date,
                    exit_date=None,
                    exit_price=None,
                    exit_reason=None,
                    fees_paid=fees,
                    lot_type=trigger,
                )
                trades.append(addon_trade)
                addon_trade_by_ticker[ticker] = addon_trade
        else:
            for ticker, ref in list(pending_addon_refs.items()):
                if ref.addon_evaluated:
                    continue
                td = by_ticker[ticker]
                c_plus_1 = td.prices.trading_day_after(ref.catalyst_date)
                if c_plus_1 != day:
                    continue
                ref.addon_evaluated = True

                c_plus_1_price = td.prices.close_on(day)
                if c_plus_1_price is None:
                    continue
                trigger = strat.evaluate_addon_trigger(ref.entry_price, c_plus_1_price, params)
                if trigger is None:
                    continue
                if ticker in open_addon_positions:
                    continue

                total_lots = len(open_addon_positions)
                if total_lots >= params.max_concurrent_positions:
                    continue

                size_pct = params.o1_position_size_pct if trigger == "o1" else params.o2_position_size_pct
                total_capital = (
                    cash + _positions_value(open_addon_positions, by_ticker, day)
                    + _sweep_mark_value(sweep_shares, benchmark, day)
                )
                target_value = total_capital * size_pct
                fees = strat.compute_trade_cost(target_value, params)
                spend = target_value + fees
                if spend > cash and params.idle_cash_sweep_enabled:
                    cash, sweep_shares = _liquidate_sweep_for_shortfall(cash, sweep_shares, benchmark, day, params, sweep_events)
                    idle_days_counter = 0
                if spend > cash:
                    continue

                addon_shares = target_value / c_plus_1_price
                cash -= spend
                idle_days_counter = 0

                addon_position = strat.PositionState(
                    ticker=ticker,
                    entry_date=day,
                    entry_price=c_plus_1_price,
                    shares=addon_shares,
                    catalyst_date=ref.catalyst_date,
                    sector=ref.sector,
                    peak_price=c_plus_1_price,
                    lot_type=trigger,
                )
                open_addon_positions[ticker] = addon_position

                addon_trade = Trade(
                    ticker=ticker,
                    entry_date=day,
                    entry_price=c_plus_1_price,
                    shares=addon_shares,
                    catalyst_date=ref.catalyst_date,
                    exit_date=None,
                    exit_price=None,
                    exit_reason=None,
                    fees_paid=fees,
                    lot_type=trigger,
                )
                trades.append(addon_trade)
                addon_trade_by_ticker[ticker] = addon_trade

        # --- 2c. Sweep any cash that's been sitting unused long enough --- #
        # into the idle-cash-sweep ticker. Runs after all of today's
        # exits/entries so it sees the day's true leftover cash.
        if params.idle_cash_sweep_enabled and benchmark is not None:
            if cash > 0.01:
                idle_days_counter += 1
                if idle_days_counter >= max(1, params.idle_cash_min_holding_days):
                    sweep_price = benchmark.prices.last_known_close_on_or_before(day)
                    if sweep_price and sweep_price > 0:
                        fees = strat.compute_trade_cost(cash, params)
                        invest = cash - fees
                        if invest > 0:
                            bought_shares = invest / sweep_price
                            sweep_events.append(
                                SweepEvent(date=day, action="buy", ticker=benchmark.ticker, shares=bought_shares, price=sweep_price, amount=invest, fees=fees)
                            )
                            sweep_shares += bought_shares
                            cash = 0.0
                    idle_days_counter = 0
            else:
                idle_days_counter = 0

        # --- 3. Record equity curve for today --- #
        sweep_value = _sweep_mark_value(sweep_shares, benchmark, day)
        positions_value = (
            _positions_value(open_positions, by_ticker, day)
            + _positions_value(open_addon_positions, by_ticker, day)
            + sweep_value
        )
        equity_rows.append(
            {
                "date": day,
                "cash": cash,
                "positions_value": positions_value,
                "total_value": cash + positions_value,
            }
        )

    # --- 4. Mark any still-open positions (base AND add-on) to market, --- #
    # report as unrealized -- never silently dropped.
    for ticker, position in open_positions.items():
        td = by_ticker[ticker]
        last_price = td.prices.last_known_close_on_or_before(end_date) or position.entry_price
        t = open_trade_by_ticker[ticker]
        t.exit_date = end_date
        t.exit_price = last_price
        t.exit_reason = "open_at_window_end"
        t.unrealized = True
        t.pnl = (last_price - t.entry_price) * t.shares - t.fees_paid
        t.pnl_pct = (last_price - t.entry_price) / t.entry_price * 100.0

    for ticker, addon in open_addon_positions.items():
        td = by_ticker[ticker]
        last_price = td.prices.last_known_close_on_or_before(end_date) or addon.entry_price
        t = addon_trade_by_ticker[ticker]
        t.exit_date = end_date
        t.exit_price = last_price
        t.exit_reason = "open_at_window_end"
        t.unrealized = True
        t.pnl = (last_price - t.entry_price) * t.shares - t.fees_paid
        t.pnl_pct = (last_price - t.entry_price) / t.entry_price * 100.0

    equity_curve = pd.DataFrame(equity_rows)
    ending_value = float(equity_curve["total_value"].iloc[-1]) if not equity_curve.empty else starting_cash

    benchmark_return_pct = None
    if benchmark is not None:
        benchmark_return_pct = compute_benchmark_return(benchmark, start_date, end_date)

    return BacktestResult(
        equity_curve=equity_curve,
        trades=trades,
        position_daily_log=pd.DataFrame(position_daily_rows),
        candidates_screened=candidates_screened,
        candidates_passed=candidates_passed,
        starting_cash=starting_cash,
        ending_value=ending_value,
        benchmark_return_pct=benchmark_return_pct,
        sweep_events=sweep_events,
    )


def _positions_value(open_positions: dict, by_ticker: dict, day: date) -> float:
    total = 0.0
    for ticker, position in open_positions.items():
        price = by_ticker[ticker].prices.last_known_close_on_or_before(day)
        if price is None:
            price = position.entry_price
        total += position.shares * price
    return total


def compute_benchmark_return(benchmark: TickerData, start_date: date, end_date: date) -> Optional[float]:
    """Simple buy-and-hold return of a benchmark index/ETF over the same
    window, for the results panel's side-by-side comparison."""
    window = benchmark.prices.as_of(end_date)
    window = window[window.index >= start_date]
    if len(window) < 2:
        return None
    start_price = float(window["Close"].iloc[0])
    end_price = float(window["Close"].iloc[-1])
    if start_price == 0:
        return None
    return (end_price - start_price) / start_price * 100.0
