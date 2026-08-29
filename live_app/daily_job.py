"""
live_app/daily_job.py

Runs once per trading day, after market close: fetch -> decide via
strategy_core.strategy (the exact same module the backtester uses, per
the target architecture's core promise) -> update state -> send the
daily digest.

Advisory only (decision 7.5): this NEVER places a trade. It tells the
person what the rules say to do; they act on it themselves and record
what they actually did through the dashboard. Mechanism O's add-on
purchases follow the same pattern -- a C+1 trigger is a RECOMMENDATION in
the digest, not an automatically created position; the person adds it
through the dashboard if they act on it, exactly like a new base
candidate. The one exception is an O1 merge: since it moves no cash and
only continues tracking lots the person already confirmed they own, the
job performs that bookkeeping automatically (see state.merge_addon_into_base).
"""

from __future__ import annotations

import concurrent.futures
import os
import time
import traceback
from datetime import date, datetime, timedelta
from typing import Optional

from strategy_core import data_sources as ds
from strategy_core import strategy as strat

from . import notify, settings as live_settings, state

# How far back to pull price history per open position, so the stagnation
# rolling lookback and volatility screen both have enough data.
PRICE_HISTORY_LOOKBACK_DAYS = 400

# Per-ticker wall-clock budget for the candidate scan (see
# _scan_upcoming_catalysts). Yahoo Finance rate-limits requests from cloud
# hosts (HTTP 429 on the crumb/API call); when that happens, yfinance falls
# back to scraping Yahoo's web page directly, and that fallback has been
# observed to hang for 30+ seconds parsing a single ticker's HTML -- long
# enough to blow past gunicorn's worker timeout and kill the whole request
# before it records anything (not even a diagnostic). A single worker thread
# reused across the scan lets one slow ticker be abandoned after this many
# seconds without adding real network concurrency (which risks tripping
# Yahoo's rate limit harder).
_SCAN_FETCH_TIMEOUT_SECONDS = 10

# How long a cached earnings-date answer is trusted before re-asking Yahoo.
# This runs at most once/day in practice (daily job, or an occasional manual
# refresh) -- 20h means a normal daily cadence always gets one fresh check
# per ticker per day, while a same-day repeat (e.g. someone clicking
# "Refresh now" twice) reuses the earlier answer instead of hitting Yahoo
# again for all ~200+ tickers.
_EARNINGS_CACHE_MAX_AGE_HOURS = 20.0

# A small gap between tickers that actually need a live Yahoo call (cache
# misses only -- cache hits skip this entirely). Bursting ~200+ requests
# back-to-back is what triggers Yahoo's rate limiting in the first place;
# spacing them out costs a few minutes on a cold cache but should mean far
# fewer 429s, and after the first run most tickers come from cache anyway.
_SCAN_REQUEST_SPACING_SECONDS = 0.4

# Hard ceiling on the whole scan's wall-clock time, checked between tickers.
# Per-ticker timeouts bound a single slow ticket, but with a large enough
# universe even well-behaved per-ticker timeouts can add up past gunicorn's
# worker timeout (render.yaml sets that to 240s) -- so the scan stops
# itself early with a clear "stopped early" diagnostic rather than risk
# gunicorn killing the whole request and returning nothing at all. Kept
# safely below the worker timeout to leave room for everything else
# run_once() does before and after this call.
_SCAN_TOTAL_BUDGET_SECONDS = 180.0

# Universe used by the forward-looking scanner. Configurable via env var;
# defaults to the pre-vetted, smaller S&P/TSX Composite universe since
# that's the safer default for something running unattended once a day.
LIVE_UNIVERSE_MODE = os.environ.get("LIVE_UNIVERSE_MODE", "index")


def _default_params() -> strat.StrategyParams:
    """The live job's execution parameters -- now read from the settings
    the dashboard itself saves (live_app/settings.py), not env vars. A
    fresh deploy with nothing saved yet just uses StrategyParams' own
    dataclass defaults."""
    return live_settings.load_params()


def _fetch_history(ticker: str, today: date):
    fetch_start = today - timedelta(days=PRICE_HISTORY_LOOKBACK_DAYS)
    return ds.fetch_price_history(ticker, fetch_start, today)


def _trading_day_after(history, d: date) -> Optional[date]:
    """The first date in a fetched price-history frame strictly after `d`
    -- the live-data equivalent of portfolio.py's PointInTimePrices.trading_day_after,
    used to find C+1 (the real next trading day, not calendar_day + 1)."""
    later = history.index[history.index > d]
    if len(later) == 0:
        return None
    return later[0]


def _check_open_position(position: state.LivePosition, params: strat.StrategyParams, today: date):
    """Fetches fresh price history for one position and replays the exact
    same exit rules the backtester uses. Returns (decision, current_price,
    updated_position_state, history) or (None, None, None, None) if data
    couldn't be fetched."""
    history = _fetch_history(position.ticker, today)
    if history.empty:
        return None, None, None, None

    current_price = float(history["Close"].iloc[-1])

    pos_state = strat.PositionState(
        ticker=position.ticker,
        entry_date=position.entry_date,
        entry_price=position.entry_price,
        shares=position.shares,
        catalyst_date=position.catalyst_date,
        sector=position.sector,
        peak_price=position.peak_price,
        next_stagnation_check_date=position.next_stagnation_check_date,
        stagnation_deferred=position.stagnation_deferred,
    )
    decision = strat.evaluate_exit(pos_state, today, current_price, history, params)
    return decision, current_price, pos_state, history


def _scan_upcoming_catalysts(
    params: strat.StrategyParams, today: date, already_held: set[str]
) -> tuple[list[dict], dict]:
    """The forward-looking scanner from spec section 4, folded into the
    single daily job instead of being a separate tool (fix 5c).

    Returns (results, diagnostics). Every per-ticker network call is still
    best-effort (one bad ticker shouldn't sink the whole scan), but unlike
    the original version this now COUNTS failures instead of silently
    discarding them -- a scan that finds zero candidates because Yahoo
    Finance is blocking/rate-limiting the host looks identical to a scan
    that genuinely found nothing, unless something reports the difference.
    """
    results = []
    diagnostics = {
        "universe_size": 0,
        "universe_fetch_error": None,
        "already_held_skipped": 0,
        "earnings_cache_hits": 0,
        "earnings_fetch_errors": 0,
        "earnings_fetch_timeouts": 0,
        "no_upcoming_earnings": 0,
        "history_fetch_errors": 0,
        "history_fetch_timeouts": 0,
        "empty_history": 0,
        "screened_out": 0,
        "passed": 0,
        "stopped_early": False,
        "tickers_checked": 0,
    }
    try:
        if LIVE_UNIVERSE_MODE == "index":
            universe = ds.get_universe_index_constituents()
        elif LIVE_UNIVERSE_MODE == "auto_sweep":
            universe = ds.get_universe_auto_sweep(params.market_cap_floor, params.market_cap_ceiling)
        else:
            universe = []
    except Exception as e:
        universe = []
        diagnostics["universe_fetch_error"] = f"{type(e).__name__}: {e}"
    diagnostics["universe_size"] = len(universe)

    window_end = today + timedelta(days=params.catalyst_window_days)
    # A small pool, not a single worker: a `with ThreadPoolExecutor(...)`
    # block's default shutdown(wait=True) on exit would block this
    # function's *return* on any task we've already given up on via
    # .result(timeout=...) -- and with only one worker, a hung call also
    # occupies the only thread, so every ticker queued behind it never
    # gets a chance to run before its own timeout fires, defeating the
    # point of a per-ticker timeout entirely. A handful of workers plus an
    # explicit non-blocking shutdown means one stuck ticker only ever
    # costs its own timeout, never anyone else's turn.
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=4)
    try:

        def call_with_timeout(fn, *args, **kwargs):
            return pool.submit(fn, *args, **kwargs).result(timeout=_SCAN_FETCH_TIMEOUT_SECONDS)

        scan_started_at = time.monotonic()
        for u in universe:
            if time.monotonic() - scan_started_at > _SCAN_TOTAL_BUDGET_SECONDS:
                diagnostics["stopped_early"] = True
                break
            diagnostics["tickers_checked"] += 1

            if u.ticker in already_held:
                diagnostics["already_held_skipped"] += 1
                continue

            # Cached first: this runs at most once a day, and earnings dates
            # barely change day to day, so re-asking Yahoo for all ~200+
            # tickers on every single run is the main reason it gets
            # rate-limited in the first place. Cache the FULL unfiltered
            # list Yahoo has (not just today's window) so changing the
            # scan window later doesn't invalidate everything already cached.
            cached = state.get_cached_earnings_dates(u.ticker, max_age_hours=_EARNINGS_CACHE_MAX_AGE_HOURS)
            if cached is not None:
                diagnostics["earnings_cache_hits"] += 1
                all_catalysts = cached
            else:
                fetch_failed = False
                try:
                    all_catalysts = call_with_timeout(ds.fetch_earnings_dates, u.ticker)
                except concurrent.futures.TimeoutError:
                    diagnostics["earnings_fetch_timeouts"] += 1
                    fetch_failed = True
                except Exception:
                    diagnostics["earnings_fetch_errors"] += 1
                    fetch_failed = True
                finally:
                    # Applies to every real Yahoo hit regardless of outcome
                    # -- spacing these out is what actually reduces the
                    # rate-limiting risk; cache hits above skip it entirely.
                    time.sleep(_SCAN_REQUEST_SPACING_SECONDS)
                if fetch_failed:
                    continue
                # Only cache a real answer -- a transient failure above
                # already skipped this ticker, so it gets retried next run
                # instead of being stuck on a bad cache.
                state.save_cached_earnings_dates(u.ticker, all_catalysts)

            catalysts = [d for d in all_catalysts if today <= d <= window_end]
            if not catalysts:
                diagnostics["no_upcoming_earnings"] += 1
                continue
            next_catalyst = min(catalysts)

            try:
                history = call_with_timeout(_fetch_history, u.ticker, today)
            except concurrent.futures.TimeoutError:
                diagnostics["history_fetch_timeouts"] += 1
                continue
            except Exception:
                diagnostics["history_fetch_errors"] += 1
                continue
            if history.empty:
                diagnostics["empty_history"] += 1
                continue
            try:
                shares_out = call_with_timeout(ds.fetch_shares_outstanding, u.ticker) or 0.0
            except Exception:
                shares_out = 0.0
            screen = strat.evaluate_candidate_screen(history, shares_out, params)
            if screen.passed:
                diagnostics["passed"] += 1
                results.append(
                    {
                        "ticker": u.ticker,
                        "catalyst_date": next_catalyst.isoformat(),
                        "days_until": (next_catalyst - today).days,
                    }
                )
            else:
                diagnostics["screened_out"] += 1
    finally:
        # wait=False + cancel_futures=True: don't block returning results
        # (and the diagnostics explaining them) on whatever a hung ticker
        # is still doing -- we've already moved on and counted it as a
        # timeout above. The abandoned thread(s) die with the process/
        # gunicorn worker; nothing here waits on them.
        pool.shutdown(wait=False, cancel_futures=True)
    return results, diagnostics


def _check_addon_trigger(position: state.LivePosition, params: strat.StrategyParams, today: date, run_at: datetime) -> Optional[dict]:
    """For an open BASE position not yet evaluated: if today is C+1,
    checks the Mechanism O trigger and returns a recommendation dict (or
    None). Always marks addon_evaluated in state once checked, win or
    lose -- this is a one-shot measurement, same as the backtest engine."""
    if position.addon_evaluated:
        return None
    history = _fetch_history(position.ticker, today)
    if history.empty:
        return None
    c_plus_1 = _trading_day_after(history, position.catalyst_date)
    if c_plus_1 != today:
        return None

    position.addon_evaluated = True
    state.update_position(position)

    c_plus_1_price = float(history.loc[today, "Close"]) if today in history.index else None
    if c_plus_1_price is None:
        return None

    trigger = strat.evaluate_addon_trigger(position.entry_price, c_plus_1_price, params)
    if trigger is None:
        return None

    pct_change = (c_plus_1_price - position.entry_price) / position.entry_price * 100.0
    rec = {
        "ticker": position.ticker,
        "lot_type": trigger,
        "price": c_plus_1_price,
        "pct_change": pct_change,
        "suggested_size_pct": (params.o1_position_size_pct if trigger == "o1" else params.o2_position_size_pct) * 100.0,
        "base_position_id": position.id,
    }
    state.log_decision(run_at, position.ticker, "addon_trigger_recommended", rec)
    return rec


def _check_addon_position(addon: state.LivePosition, params: strat.StrategyParams, today: date, run_at: datetime) -> tuple[Optional[dict], Optional[dict], Optional[float]]:
    """Returns (exit_recommendation, merge_event, current_price) -- at most one of the first two is set."""
    history = _fetch_history(addon.ticker, today)
    if history.empty:
        state.log_decision(run_at, addon.ticker, "error", {"error": "no price data", "lot_type": addon.lot_type})
        return None, None, None
    current_price = float(history["Close"].iloc[-1])

    addon_state = strat.PositionState(
        ticker=addon.ticker, entry_date=addon.entry_date, entry_price=addon.entry_price,
        shares=addon.shares, catalyst_date=addon.catalyst_date, sector=addon.sector,
        peak_price=addon.peak_price, lot_type=addon.lot_type,
    )

    if addon.lot_type == "o1":
        base = state.get_position(addon.parent_id) if addon.parent_id else None
        base_open = base is not None and base.status == "open"
        decision = strat.evaluate_o1_addon_exit(addon_state, today, current_price, history, params, base_open)

        if decision.action == "hold":
            return None, None, current_price

        if decision.action == "merge":
            state.merge_addon_into_base(addon, base, today, current_price)
            merge_event = {
                "ticker": addon.ticker, "price": current_price,
                "combined_shares": base.shares + addon.shares,
            }
            state.log_decision(run_at, addon.ticker, "addon_merged", merge_event)
            return None, merge_event, current_price

        exit_rec = {"ticker": addon.ticker, "lot_type": "o1", "reason": decision.reason, "price": current_price}
        state.log_decision(run_at, addon.ticker, "addon_exit_recommended", exit_rec)
        return exit_rec, None, current_price

    elif addon.lot_type == "o2":
        should_exit = strat.evaluate_o2_addon_exit(addon_state, today, current_price, history)
        if not should_exit:
            return None, None, current_price
        exit_rec = {"ticker": addon.ticker, "lot_type": "o2", "reason": "o2_first_down_day", "price": current_price}
        state.log_decision(run_at, addon.ticker, "addon_exit_recommended", exit_rec)
        return exit_rec, None, current_price

    return None, None, current_price


def _check_idle_sweep(params: strat.StrategyParams, today: date, run_at: datetime) -> Optional[dict]:
    """Live-loop equivalent of the backtester's in-memory idle-cash-sweep
    (see portfolio.run_backtest): buys the FULL current cash balance into
    the sweep ticker once it's sat unused for `idle_cash_min_holding_days`
    consecutive daily-job runs in a row. Advisory-only in spirit still
    holds here too -- there's no automatic sell-to-cover-a-shortfall (the
    live app never spends money on the person's behalf; see
    sell_idle_sweep_now for the manual equivalent the dashboard exposes)."""
    if not params.idle_cash_sweep_enabled:
        return None
    ticker = params.idle_cash_sweep_ticker
    sweep_state = state.get_idle_sweep_state(ticker)
    cash_balance = state.compute_cash_balance()

    if cash_balance <= 0.01:
        state.save_idle_sweep_state(ticker, sweep_state["shares"], 0, today)
        return None

    idle_days = sweep_state["idle_days_counter"] + 1
    if idle_days < max(1, params.idle_cash_min_holding_days):
        state.save_idle_sweep_state(ticker, sweep_state["shares"], idle_days, today)
        return None

    try:
        price = ds.fetch_current_price(ticker)
    except Exception:
        price = None
    if not price or price <= 0:
        # Couldn't price it today -- keep the idle-days count so we don't
        # lose progress, try again on the next run.
        state.save_idle_sweep_state(ticker, sweep_state["shares"], idle_days, today)
        return None

    fees = strat.compute_trade_cost(cash_balance, params)
    invest = cash_balance - fees
    if invest <= 0:
        state.save_idle_sweep_state(ticker, sweep_state["shares"], idle_days, today)
        return None

    bought_shares = invest / price
    state.log_idle_sweep_event(today, "buy", ticker, bought_shares, price, invest, fees)
    state.log_decision(run_at, ticker, "idle_sweep_buy", {"shares": bought_shares, "price": price, "amount": invest})
    state.save_idle_sweep_state(ticker, sweep_state["shares"] + bought_shares, 0, today)
    return {"ticker": ticker, "shares": bought_shares, "price": price, "amount": invest}


def sell_idle_sweep_now() -> Optional[dict]:
    """Manually liquidates the entire idle-sweep holding right now (in
    full, same simplification the backtester uses) -- for when the person
    wants to free up cash themselves rather than waiting on the automatic
    buy-in logic. Used by the dashboard's 'Sell idle sweep' action."""
    params = _default_params()
    ticker = params.idle_cash_sweep_ticker
    sweep_state = state.get_idle_sweep_state(ticker)
    if sweep_state["shares"] <= 0:
        return None
    price = ds.fetch_current_price(ticker)
    if not price or price <= 0:
        raise RuntimeError(f"Could not fetch a current price for {ticker} -- try again shortly.")
    gross = sweep_state["shares"] * price
    fees = strat.compute_trade_cost(gross, params)
    today = date.today()
    state.log_idle_sweep_event(today, "sell", ticker, sweep_state["shares"], price, gross, fees)
    state.log_decision(datetime.now(), ticker, "idle_sweep_sell", {"shares": sweep_state["shares"], "price": price, "amount": gross})
    state.save_idle_sweep_state(ticker, 0.0, 0, today)
    return {"ticker": ticker, "shares": sweep_state["shares"], "price": price, "amount": gross}


def _record_snapshot(today: date, positions_value: float) -> None:
    """Appends today's portfolio-value snapshot (real performance-chart
    data, replacing the old mock series). Cash balance is always
    recomputed from the ledger; the sweep holding is marked to market with
    a fresh price fetch (falls back to its last logged trade price if
    today's fetch fails, rather than dropping the snapshot)."""
    cash_balance = state.compute_cash_balance()
    sweep_ui = _default_params()
    sweep_state = state.get_idle_sweep_state(sweep_ui.idle_cash_sweep_ticker)
    sweep_value = 0.0
    if sweep_state["shares"] > 0:
        try:
            price = ds.fetch_current_price(sweep_ui.idle_cash_sweep_ticker)
        except Exception:
            price = None
        if price:
            sweep_value = sweep_state["shares"] * price
    state.record_portfolio_snapshot(today, cash_balance, positions_value, sweep_value)


def run_once(send_email: bool = True) -> dict:
    """The whole daily job. Returns a summary dict (used by the dashboard's
    'run now' button and by tests) regardless of whether email sending is
    enabled/succeeds."""
    today = date.today()
    run_at = datetime.now()

    exited_today = []
    close_to_trigger = []
    errors = []
    addon_opportunities = []
    addon_exits_recommended = []
    addon_merges = []
    positions_value = 0.0

    open_base_positions = state.get_open_base_positions()
    params = _default_params()

    for position in open_base_positions:
        try:
            decision, current_price, pos_state, history = _check_open_position(position, params, today)
        except Exception as e:
            errors.append({"ticker": position.ticker, "error": str(e), "trace": traceback.format_exc()})
            state.log_decision(run_at, position.ticker, "error", {"error": str(e)})
            continue

        if decision is None:
            errors.append({"ticker": position.ticker, "error": "no price data"})
            state.log_decision(run_at, position.ticker, "error", {"error": "no price data"})
            continue

        if not decision.should_exit:
            positions_value += current_price * position.shares

        # Persist the rolling bookkeeping (peak price ratchet, stagnation
        # deferral) regardless of outcome -- next day's check depends on it.
        position.peak_price = pos_state.peak_price
        position.stagnation_deferred = pos_state.stagnation_deferred
        position.next_stagnation_check_date = pos_state.next_stagnation_check_date
        state.update_position(position)

        if decision.should_exit:
            exited_today.append(
                {
                    "ticker": position.ticker,
                    "reason": decision.reason,
                    "price": current_price,
                    "entry_date": position.entry_date.isoformat(),
                    "entry_price": position.entry_price,
                }
            )
            state.log_decision(
                run_at, position.ticker, "exit_recommended",
                {"reason": decision.reason, "price": current_price},
            )
        else:
            pct_to_stop = (
                (current_price - decision.stop_loss_trigger_price) / current_price * 100.0
                if decision.stop_loss_trigger_price else None
            )
            close_to_trigger.append(
                {
                    "ticker": position.ticker,
                    "pct_to_stop": pct_to_stop if pct_to_stop is not None else 0.0,
                    "days_to_stagnation_check": decision.days_to_next_stagnation_check,
                }
            )
            state.log_decision(
                run_at, position.ticker, "hold",
                {"price": current_price, "pct_to_stop": pct_to_stop},
            )

        # Mechanism O: has C+1 arrived for this position? (one-shot check)
        try:
            opportunity = _check_addon_trigger(position, params, today, run_at)
            if opportunity:
                addon_opportunities.append(opportunity)
        except Exception as e:
            errors.append({"ticker": position.ticker, "error": f"addon trigger check failed: {e}"})
            state.log_decision(run_at, position.ticker, "error", {"error": f"addon trigger check failed: {e}"})

    # Mechanism O: evaluate every currently open add-on lot (O1 timer/merge, O2 first-down-day)
    for addon in state.get_open_addon_positions():
        try:
            exit_rec, merge_event, current_price = _check_addon_position(addon, params, today, run_at)
            if exit_rec:
                addon_exits_recommended.append(exit_rec)
            elif merge_event:
                addon_merges.append(merge_event)
            elif current_price is not None:
                positions_value += current_price * addon.shares
        except Exception as e:
            errors.append({"ticker": addon.ticker, "error": f"addon exit check failed: {e}"})
            state.log_decision(run_at, addon.ticker, "error", {"error": f"addon exit check failed: {e}"})

    already_held = {p.ticker for p in open_base_positions}
    new_candidates, scan_diagnostics = _scan_upcoming_catalysts(params, today, already_held)
    for c in new_candidates:
        state.log_decision(run_at, c["ticker"], "new_candidate", c)

    if not new_candidates:
        # Zero candidates is a legitimate result (the screen is strict), but
        # it's indistinguishable from "every network call failed" unless we
        # say so explicitly -- surface it in both places a user would look.
        fetch_failures = (
            scan_diagnostics["earnings_fetch_errors"]
            + scan_diagnostics["earnings_fetch_timeouts"]
            + scan_diagnostics["history_fetch_errors"]
            + scan_diagnostics["history_fetch_timeouts"]
        )
        # How many tickers actually needed a live Yahoo call this run --
        # cache hits and already-held skips never touched the network, so
        # they shouldn't count toward "how much of the scan failed". Based
        # on tickers_checked, not universe_size, since a scan that stopped
        # early never got to the rest of the universe at all.
        attempted = (
            scan_diagnostics["tickers_checked"]
            - scan_diagnostics["already_held_skipped"]
            - scan_diagnostics["earnings_cache_hits"]
        )
        if scan_diagnostics["universe_fetch_error"]:
            note = f"Universe fetch failed: {scan_diagnostics['universe_fetch_error']}"
        elif scan_diagnostics["universe_size"] == 0:
            note = "Universe list came back empty (LIVE_UNIVERSE_MODE misconfigured, or the source list is empty)."
        else:
            # Coverage and failure-rate are independent questions -- a scan
            # can stop early AND still show every sign of Yahoo blocking it,
            # or stop early with a perfectly healthy (if partial) result.
            # Report both, instead of letting "stopped early" hide a real
            # rate-limiting problem underneath it (or vice versa).
            if scan_diagnostics["stopped_early"]:
                coverage = (
                    f"Scan stopped after {_SCAN_TOTAL_BUDGET_SECONDS:.0f}s to stay well under the "
                    f"server's request timeout -- only got through {scan_diagnostics['tickers_checked']} "
                    f"of {scan_diagnostics['universe_size']} tickers "
                    f"({scan_diagnostics['earnings_cache_hits']} from cache). The rest will get picked "
                    f"up on the next run; each one only needs a live check once a day, so coverage "
                    f"should improve as the cache fills in."
                )
            else:
                coverage = f"Scanned all {scan_diagnostics['tickers_checked']} tickers in the universe."

            if attempted > 0 and fetch_failures >= attempted * 0.5:
                diagnosis = (
                    f" Of the {attempted} tickers that needed a live check this run "
                    f"({scan_diagnostics['earnings_cache_hits']} others came from cache): "
                    f"{scan_diagnostics['earnings_fetch_errors']} earnings-date fetches failed and "
                    f"{scan_diagnostics['earnings_fetch_timeouts']} timed out "
                    f"(>{_SCAN_FETCH_TIMEOUT_SECONDS}s); {scan_diagnostics['history_fetch_errors']} "
                    f"price-history fetches failed and {scan_diagnostics['history_fetch_timeouts']} timed "
                    f"out. That's over half -- looks like Yahoo Finance is still blocking or "
                    f"rate-limiting requests from this host, not a real zero-candidate result."
                )
            elif attempted > 0:
                diagnosis = (
                    f" Of those {attempted} live checks: {scan_diagnostics['no_upcoming_earnings']} had "
                    f"no earnings in the window, {scan_diagnostics['screened_out']} had earnings but "
                    f"didn't clear the screen -- looks like a genuine zero among the tickers checked so far."
                )
            else:
                # Every ticker this run was a cache hit or an already-held skip --
                # no live checks happened at all, so there's nothing yet to
                # diagnose about fetch health from this run alone.
                diagnosis = ""
            note = coverage + diagnosis
        errors.append({"ticker": None, "error": f"candidate scan: {note}", **scan_diagnostics})
        state.log_decision(run_at, None, "scan_diagnostic", {"note": note})

    idle_sweep_buy = None
    try:
        idle_sweep_buy = _check_idle_sweep(params, today, run_at)
    except Exception as e:
        errors.append({"ticker": params.idle_cash_sweep_ticker, "error": f"idle sweep check failed: {e}"})
        state.log_decision(run_at, params.idle_cash_sweep_ticker, "error", {"error": f"idle sweep check failed: {e}"})

    try:
        _record_snapshot(today, positions_value)
    except Exception as e:
        errors.append({"ticker": None, "error": f"snapshot recording failed: {e}"})

    summary = {
        "run_at": run_at.isoformat(),
        "exited_today": exited_today,
        "close_to_trigger": close_to_trigger,
        "new_candidates": new_candidates,
        "addon_opportunities": addon_opportunities,
        "addon_exits_recommended": addon_exits_recommended,
        "addon_merges": addon_merges,
        "idle_sweep_buy": idle_sweep_buy,
        "errors": errors,
    }

    if send_email:
        try:
            subject, html_body, text_body = notify.build_digest_content(
                exited_today, close_to_trigger, new_candidates, run_at,
                addon_opportunities=addon_opportunities,
                addon_exits_recommended=addon_exits_recommended,
                addon_merges=addon_merges,
            )
            notify.send_daily_digest(subject, html_body, text_body)
            summary["email_sent"] = True
        except Exception as e:
            summary["email_sent"] = False
            summary["email_error"] = str(e)
            state.log_decision(run_at, None, "error", {"error": f"digest send failed: {e}"})

    return summary


if __name__ == "__main__":
    result = run_once()
    print(result)
