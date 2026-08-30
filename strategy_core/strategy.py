"""
strategy_core/strategy.py

Model 2's entry/exit rules and screening logic, as pure functions over
price history + per-position state.

Nothing in this file performs I/O (no network calls, no file reads). Every
function is handed the data it needs and returns a decision. That is what
makes the backtest engine's no-lookahead guarantee possible: as long as
callers only ever pass this module a price history sliced up to "today",
these functions are structurally unable to see anything past that point.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal, Optional

import numpy as np
import pandas as pd

ExitReason = Literal[
    "stop_loss", "stagnation", "open_at_window_end",
    "o1_timed_exit", "o1_timed_exit_no_base_to_merge", "o2_first_down_day", "merged_into_base",
]
LotType = Literal["base", "o1", "o2"]
AddonTrigger = Literal["o1", "o2"]

TRADING_DAYS_PER_YEAR = 252


# --------------------------------------------------------------------------- #
# Parameters
# --------------------------------------------------------------------------- #


@dataclass
class StrategyParams:
    """Every one of these corresponds to a slider in the desktop app."""

    # -- Numeric screen (candidate must pass ALL of these) -- #
    min_volatility: float = 0.40  # 60-day annualized vol of daily returns
    market_cap_floor: float = 10_000_000.0  # CAD
    market_cap_ceiling: float = 3_000_000_000.0  # CAD
    min_avg_dollar_volume: float = 1_000_000.0  # 20-day avg $ volume/day
    catalyst_window_days: int = 45  # forward-scan only; ignored by the backtest

    # -- Entry -- #
    enable_base_strategy: bool = True  # off = backtest Mechanism O (O1/O2) in isolation, no base position ever opens
    position_size_pct: float = 0.05  # fraction of total capital per name
    max_concurrent_positions: int = 20
    max_positions_per_sector: int = 2

    # -- Exit -- #
    stop_loss_pct: float = 0.35
    stagnation_start_days: int = 42
    stagnation_min_gain_pct: float = 0.08
    trend_aware: bool = False
    trend_window_days: int = 5

    # -- Costs -- #
    flat_fee: float = 0.0
    spread_pct: float = 0.001

    # -- Idle cash sweep (optional, secondary) -- #
    idle_cash_sweep_enabled: bool = False
    idle_cash_sweep_ticker: str = "VFV.TO"
    idle_cash_min_holding_days: int = 30  # how many consecutive days cash must sit unused before it's swept into the ticker above

    # -- Universe / entry caps -- #
    max_positions: int = 20  # alias kept in sync with max_concurrent_positions by callers

    # -- Mechanism O: C+1 overreaction add-on purchases (both optional, independently toggled) -- #
    # Theory: catalyst-day moves occasionally overreact and regress toward
    # mean growth. O1 tries to capture extra lift from a negative
    # overreaction regressing upward; O2 tries to ride a few more days of a
    # positive overreaction before it regresses back down. Both are
    # measured relative to the base position's own entry price (close of
    # C-1) at the close of C+1, and both are a SEPARATE lot -- the base
    # position's own stop-loss/stagnation are untouched by either.
    enable_o1_dip_buy: bool = False
    o1_decline_threshold_pct: float = 0.02  # trigger: C+1 close <= entry_price * (1 - this)
    o1_position_size_pct: float = 0.05  # fraction of total capital, same style as the base entry
    o1_exit_duration_days: int = 15  # timed exit horizon AND the uptrend lookback window at that checkpoint

    enable_o2_momentum_buy: bool = False
    o2_increase_threshold_pct: float = 0.02  # trigger: C+1 close >= entry_price * (1 + this)
    o2_position_size_pct: float = 0.05


# --------------------------------------------------------------------------- #
# Per-position state carried day to day by the backtest engine / live tracker
# --------------------------------------------------------------------------- #


@dataclass
class PositionState:
    ticker: str
    entry_date: date
    entry_price: float
    shares: float
    catalyst_date: date
    sector: Optional[str] = None

    peak_price: float = field(default=0.0)  # ratchets up only, never resets down
    next_stagnation_check_date: Optional[date] = None  # None until stagnation window opens
    stagnation_deferred: bool = False  # True while in a trend-aware reprieve

    # -- Mechanism O bookkeeping -- #
    lot_type: LotType = "base"  # 'base' | 'o1' | 'o2' -- which rules govern this lot
    addon_evaluated: bool = False  # True once the C+1 O1/O2 trigger check has run for a 'base' lot
    # entry_date doubles as "the day this lot was bought" for o1/o2 lots
    # (i.e. C+1), so no separate field is needed to time their own clocks.

    def __post_init__(self):
        if self.peak_price <= 0:
            self.peak_price = self.entry_price


@dataclass
class ExitDecision:
    should_exit: bool
    reason: Optional[ExitReason] = None
    # Distance-to-trigger info, useful for the live tracker's "how close" reporting
    stop_loss_trigger_price: Optional[float] = None
    days_to_next_stagnation_check: Optional[int] = None


# --------------------------------------------------------------------------- #
# Screening
# --------------------------------------------------------------------------- #


def compute_historical_volatility(daily_closes: pd.Series, window: int = 60) -> Optional[float]:
    """60-day annualized volatility of daily returns, using only the last
    `window` closes in the series passed in (caller is responsible for
    having already sliced the series to "as of" the evaluation day)."""
    if len(daily_closes) < window + 1:
        return None
    recent = daily_closes.tail(window + 1)
    daily_returns = recent.pct_change().dropna()
    if daily_returns.empty:
        return None
    return float(daily_returns.std(ddof=1) * np.sqrt(TRADING_DAYS_PER_YEAR))


def compute_avg_dollar_volume(price_history: pd.DataFrame, window: int = 20) -> Optional[float]:
    """20-day average of Close * Volume, using only the tail of the frame
    passed in."""
    if len(price_history) < window:
        return None
    recent = price_history.tail(window)
    dollar_vol = recent["Close"] * recent["Volume"]
    return float(dollar_vol.mean())


def approximate_market_cap(close_price: float, shares_outstanding: float) -> float:
    """Market cap has to be *approximated* historically: today's
    shares-outstanding figure times the historical closing price on the
    date being evaluated. Known, accepted approximation -- historical
    share-count data isn't available from the free data source used."""
    return close_price * shares_outstanding


@dataclass
class ScreenResult:
    passed: bool
    volatility: Optional[float] = None
    market_cap: Optional[float] = None
    avg_dollar_volume: Optional[float] = None
    failure_reasons: list = field(default_factory=list)


def evaluate_candidate_screen_from_stats(
    volatility: Optional[float],
    avg_dollar_volume: Optional[float],
    market_cap: Optional[float],
    params: StrategyParams,
) -> ScreenResult:
    """Same reasons-checking logic as evaluate_candidate_screen, but takes
    already-computed stats directly instead of a raw price history frame.
    Lets a caller reuse cached per-ticker stats (see live_app/daily_job.py's
    screen-inputs cache) without re-fetching and re-slicing live price
    history just to recompute the exact same numbers -- the numeric
    thresholds in `params` are applied here, at evaluation time, so a
    settings change is picked up on the next call without needing to
    invalidate whatever cached the stats themselves."""
    reasons = []

    if volatility is None:
        reasons.append("insufficient_history_for_volatility")
    elif volatility < params.min_volatility:
        reasons.append("volatility_below_minimum")

    if market_cap is None:
        reasons.append("no_shares_outstanding")
    else:
        if market_cap < params.market_cap_floor:
            reasons.append("market_cap_below_floor")
        if market_cap > params.market_cap_ceiling:
            reasons.append("market_cap_above_ceiling")

    if avg_dollar_volume is None:
        reasons.append("insufficient_history_for_dollar_volume")
    elif avg_dollar_volume < params.min_avg_dollar_volume:
        reasons.append("dollar_volume_below_minimum")

    return ScreenResult(
        passed=len(reasons) == 0,
        volatility=volatility,
        market_cap=market_cap,
        avg_dollar_volume=avg_dollar_volume,
        failure_reasons=reasons,
    )


def evaluate_candidate_screen(
    price_history: pd.DataFrame,
    shares_outstanding: float,
    params: StrategyParams,
) -> ScreenResult:
    """Run every numeric-screen filter from the strategy spec against a
    price history that the caller has ALREADY sliced to end on the
    evaluation date (i.e. the day before the catalyst). This function does
    not know or care what "today" is -- it just looks at the tail of
    whatever frame it's handed, which is what keeps the no-lookahead
    guarantee structural rather than a matter of trusting every caller.

    `price_history` must have columns: Close, Volume, indexed/sorted by
    date ascending, with the last row being the evaluation day.
    """
    if price_history is None or price_history.empty:
        return ScreenResult(passed=False, failure_reasons=["no_price_history"])

    last_close = float(price_history["Close"].iloc[-1])
    vol = compute_historical_volatility(price_history["Close"])
    adv = compute_avg_dollar_volume(price_history)
    mcap = approximate_market_cap(last_close, shares_outstanding) if shares_outstanding else None

    return evaluate_candidate_screen_from_stats(vol, adv, mcap, params)


# --------------------------------------------------------------------------- #
# Position sizing / portfolio caps
# --------------------------------------------------------------------------- #


def compute_position_size(total_capital: float, params: StrategyParams) -> float:
    """Fixed percentage of total capital (cash + mark-to-market value of
    every open position) at the moment of entry."""
    return total_capital * params.position_size_pct


def can_open_new_position(
    open_positions: list[PositionState],
    candidate_ticker: str,
    candidate_sector: Optional[str],
    params: StrategyParams,
) -> tuple[bool, Optional[str]]:
    """Checks the two concentration caps plus the "never buy a ticker
    that's already held" rule. Returns (allowed, reason_if_blocked)."""
    if any(p.ticker == candidate_ticker for p in open_positions):
        return False, "already_holding_ticker"

    if len(open_positions) >= params.max_concurrent_positions:
        return False, "max_concurrent_positions_reached"

    if candidate_sector is not None:
        sector_count = sum(1 for p in open_positions if p.sector == candidate_sector)
        if sector_count >= params.max_positions_per_sector:
            return False, "max_positions_per_sector_reached"

    return True, None


# --------------------------------------------------------------------------- #
# Fees
# --------------------------------------------------------------------------- #


def compute_trade_cost(trade_value: float, params: StrategyParams) -> float:
    """Flat dollar fee plus a spread cost modeled as a percentage of trade
    value. Applies to both sides of every trade (buy and sell)."""
    return params.flat_fee + abs(trade_value) * params.spread_pct


# --------------------------------------------------------------------------- #
# Exit evaluation -- called once per open position per day
# --------------------------------------------------------------------------- #


def evaluate_exit(
    position: PositionState,
    current_date: date,
    current_price: float,
    price_history: pd.DataFrame,
    params: StrategyParams,
) -> ExitDecision:
    """Evaluate both exit rules for one position on one day, using only
    `current_price` and a `price_history` the caller has sliced to end on
    (and include) `current_date`.

    Mutates `position.peak_price` and the stagnation-check bookkeeping
    fields in place (the ratchet and the "next check date" are position
    state, not derived fresh each call) -- callers should persist the
    returned/mutated `position` between days.
    """
    # 1. Update the trailing peak. It only ever ratchets upward.
    if current_price > position.peak_price:
        position.peak_price = current_price

    stop_trigger_price = position.peak_price * (1 - params.stop_loss_pct)

    # 2. Trailing stop-loss check -- takes priority if both would fire the
    #    same day, since it's the capital-protection rule.
    if current_price <= stop_trigger_price:
        return ExitDecision(
            should_exit=True,
            reason="stop_loss",
            stop_loss_trigger_price=stop_trigger_price,
        )

    # 3. Stagnation check.
    days_since_catalyst = (current_date - position.catalyst_date).days

    if days_since_catalyst < params.stagnation_start_days:
        # Stagnation window hasn't opened yet.
        return ExitDecision(
            should_exit=False,
            stop_loss_trigger_price=stop_trigger_price,
            days_to_next_stagnation_check=params.stagnation_start_days - days_since_catalyst,
        )

    if position.stagnation_deferred and position.next_stagnation_check_date is not None:
        if current_date < position.next_stagnation_check_date:
            # Still inside a trend-aware reprieve -- no check today.
            days_left = (position.next_stagnation_check_date - current_date).days
            return ExitDecision(
                should_exit=False,
                stop_loss_trigger_price=stop_trigger_price,
                days_to_next_stagnation_check=days_left,
            )
        # Reprieve window elapsed -- check resumes fresh today.
        position.stagnation_deferred = False

    # Rolling comparison: current price vs. price exactly
    # `stagnation_start_days` calendar-days-of-data (i.e. `stagnation_start_days`
    # rows) earlier in the price history.
    lookback = params.stagnation_start_days
    if len(price_history) <= lookback:
        # Not enough history to run the check yet -- treat as pass for today.
        return ExitDecision(
            should_exit=False,
            stop_loss_trigger_price=stop_trigger_price,
            days_to_next_stagnation_check=0,
        )

    price_then = float(price_history["Close"].iloc[-(lookback + 1)])
    required_price = price_then * (1 + params.stagnation_min_gain_pct)
    stagnation_ok = current_price >= required_price

    if stagnation_ok:
        return ExitDecision(
            should_exit=False,
            stop_loss_trigger_price=stop_trigger_price,
            days_to_next_stagnation_check=0,  # rolling: checked again tomorrow
        )

    # Stagnation check failed.
    if not params.trend_aware:
        return ExitDecision(should_exit=True, reason="stagnation")

    # Trend-aware reprieve: has price risen at all over the shorter trend window?
    trend_window = params.trend_window_days
    if len(price_history) > trend_window:
        price_trend_ago = float(price_history["Close"].iloc[-(trend_window + 1)])
        trending_up = current_price > price_trend_ago
    else:
        trending_up = False

    if trending_up:
        position.stagnation_deferred = True
        position.next_stagnation_check_date = _add_calendar_days(current_date, trend_window)
        return ExitDecision(
            should_exit=False,
            stop_loss_trigger_price=stop_trigger_price,
            days_to_next_stagnation_check=trend_window,
        )

    return ExitDecision(should_exit=True, reason="stagnation")


def _add_calendar_days(d: date, n: int) -> date:
    from datetime import timedelta

    return d + timedelta(days=n)


# --------------------------------------------------------------------------- #
# Mechanism O: C+1 overreaction add-on purchases
# --------------------------------------------------------------------------- #


def evaluate_addon_trigger(
    entry_price: float,
    c_plus_1_price: float,
    params: StrategyParams,
) -> Optional[AddonTrigger]:
    """Called once, at the close of C+1, for a base position that hasn't
    been evaluated yet (see PositionState.addon_evaluated). Compares
    C+1's close to the base position's own entry price (close of C-1).

    O1 (decline -> dip-buy) and O2 (incline -> momentum-buy) are mutually
    exclusive by construction (the price move can't be both a decline and
    an incline) and each independently toggleable -- if the triggering
    move happens but that mechanism is switched off, nothing fires.
    """
    if entry_price <= 0:
        return None
    pct_change = (c_plus_1_price - entry_price) / entry_price

    if params.enable_o1_dip_buy and pct_change <= -params.o1_decline_threshold_pct:
        return "o1"
    if params.enable_o2_momentum_buy and pct_change >= params.o2_increase_threshold_pct:
        return "o2"
    return None


@dataclass
class AddonExitDecision:
    action: Literal["hold", "timed_exit", "merge"]
    reason: Optional[ExitReason] = None


def evaluate_o1_addon_exit(
    addon_position: PositionState,
    current_date: date,
    current_price: float,
    price_history: pd.DataFrame,
    params: StrategyParams,
    base_position_open: bool,
) -> AddonExitDecision:
    """O1's own lot: exit `o1_exit_duration_days` after its own entry,
    UNLESS at that exact checkpoint the stock is on an
    `o1_exit_duration_days`-day uptrend -- in which case it merges into
    the base position instead of being sold (spec: "this stock gets
    included with the stock purchased on day C-1 and has normal
    stagnation and stoploss rules apply as with the original purchase").

    If the base position has already closed by the time the checkpoint
    arrives (its own stop-loss or stagnation got there first), there's
    nothing left to merge into -- falls back to a real timed exit
    regardless of trend, tagged distinctly so it's visible in the trade
    log that the merge path wasn't available.
    """
    days_held = (current_date - addon_position.entry_date).days
    if days_held < params.o1_exit_duration_days:
        return AddonExitDecision(action="hold")

    lookback = params.o1_exit_duration_days
    on_uptrend = False
    if len(price_history) > lookback:
        price_then = float(price_history["Close"].iloc[-(lookback + 1)])
        on_uptrend = current_price > price_then
    # else: not enough history to confirm an uptrend -- conservative default (False)

    if on_uptrend and base_position_open:
        return AddonExitDecision(action="merge")
    if on_uptrend and not base_position_open:
        return AddonExitDecision(action="timed_exit", reason="o1_timed_exit_no_base_to_merge")
    return AddonExitDecision(action="timed_exit", reason="o1_timed_exit")


def evaluate_o2_addon_exit(
    addon_position: PositionState,
    current_date: date,
    current_price: float,
    price_history: pd.DataFrame,
) -> bool:
    """O2's own lot: exit on the first down day (today's close below the
    prior close). Never checked on the entry day itself -- the earliest
    possible down-day is the day after entry."""
    if current_date <= addon_position.entry_date:
        return False
    if len(price_history) < 2:
        return False
    prior_close = float(price_history["Close"].iloc[-2])
    return current_price < prior_close


# --------------------------------------------------------------------------- #
# Idle cash sweep (optional, secondary)
# --------------------------------------------------------------------------- #
#
# The actual sweep decision and bookkeeping live in portfolio.run_backtest,
# not here -- unlike every other rule in this file, it needs to hold state
# across days (how long has cash been sitting idle, how many sweep-ticker
# shares are currently held) and execute real buy/sell trades against the
# sweep ticker's own point-in-time price history, both of which are the
# engine's job. In short: once cash has sat unused for
# `idle_cash_min_holding_days` days in a row, it's swept into
# `idle_cash_sweep_ticker`; it's sold back (in full) the moment a new
# position needs more cash than is currently sitting uninvested.
