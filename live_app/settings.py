"""
live_app/settings.py

The strategy parameters, editable from the dashboard itself -- this is
what replaces the old env-var-only `daily_job._default_params()`. Two
things live here:

1. A UI-friendly dict shape (percents as 0-100, dollars in $B/$M, plain
   ticker/day/session counts) matching what the dashboard's sliders show
   -- this is what's persisted (as JSON, via state.py) and what the
   /api/settings endpoint speaks.
2. A conversion to strategy_core.strategy.StrategyParams (fractions like
   0.40, raw dollar amounts) -- what the actual engine (screening,
   entry/exit, Mechanism O, idle sweep) consumes.

A couple of sliders inherited from the original Signal Ledger mockup
still don't correspond to any real, tested engine behavior: Mechanism
O's trigger check is always exactly C+1, and O2's exit is a whole
trading day's close, not an intraday hour. Rather than silently drop
those controls (changing the look of the settings dialog) or silently
pretend they do something, they're kept here with harmless stored
defaults and listed in NOT_WIRED so the frontend can show them disabled
with a short note -- honest about what's real.

`base.entry_lead_days` used to be in that list too, but it's genuinely
wired now: the candidate scanner (daily_job._scan_upcoming_catalysts)
uses it to compute a "recommended entry date" shown on each candidate
card (catalyst date minus this many business days). This is advisory
only -- Mike always enters his own real purchase date by hand in
"Record a purchase," so this doesn't touch the tested backtest engine's
own C-1 entry-timing assumption (strategy_core/portfolio.py), which is
a separate, backtest-only concern.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from strategy_core import strategy as strat

from . import state

# --------------------------------------------------------------------------- #
# Defaults -- derived from strategy_core.StrategyParams()'s own dataclass
# defaults, not invented separately, so the two never silently drift apart.
# --------------------------------------------------------------------------- #

_BASE_DEFAULTS = strat.StrategyParams()

DEFAULTS: dict[str, Any] = {
    "strategies": {
        "base_enabled": _BASE_DEFAULTS.enable_base_strategy,
        "idle_sweep_enabled": _BASE_DEFAULTS.idle_cash_sweep_enabled,
        "dip_enabled": _BASE_DEFAULTS.enable_o1_dip_buy,
        "spike_enabled": _BASE_DEFAULTS.enable_o2_momentum_buy,
    },
    "universe": {
        "min_volatility_pct": _BASE_DEFAULTS.min_volatility * 100.0,
        "max_market_cap_b": _BASE_DEFAULTS.market_cap_ceiling / 1_000_000_000.0,
        "min_dollar_volume_m": _BASE_DEFAULTS.min_avg_dollar_volume / 1_000_000.0,
        "earnings_horizon_days": _BASE_DEFAULTS.catalyst_window_days,
    },
    "base": {
        "entry_lead_days": 1,  # business days before the catalyst for the "recommended entry date" shown on candidate cards
        "base_allocation_pct": _BASE_DEFAULTS.position_size_pct * 100.0,
        "sector_limit": _BASE_DEFAULTS.max_positions_per_sector,
        "trailing_stop_pct": _BASE_DEFAULTS.stop_loss_pct * 100.0,
        "stagnation_window_days": _BASE_DEFAULTS.stagnation_start_days,
        "trend_threshold_pct": _BASE_DEFAULTS.stagnation_min_gain_pct * 100.0,
        "trend_window_sessions": _BASE_DEFAULTS.trend_window_days,
    },
    "idle": {
        "sweep_ticker": _BASE_DEFAULTS.idle_cash_sweep_ticker,
        "min_holding_days": _BASE_DEFAULTS.idle_cash_min_holding_days,
    },
    "dip": {
        "check_delay_days": 1,  # NOT WIRED -- O1's trigger check is always C+1
        "threshold_pct": _BASE_DEFAULTS.o1_decline_threshold_pct * 100.0,
        "allocation_pct": _BASE_DEFAULTS.o1_position_size_pct * 100.0,
        "holding_sessions": _BASE_DEFAULTS.o1_exit_duration_days,
    },
    "spike": {
        "threshold_pct": _BASE_DEFAULTS.o2_increase_threshold_pct * 100.0,
        "allocation_pct": _BASE_DEFAULTS.o2_position_size_pct * 100.0,
        "exit_lead_hours": 1.0,  # NOT WIRED -- O2 exits on the first down day's close
    },
}

# Dotted paths the frontend should render disabled, with the reason shown
# alongside them.
NOT_WIRED = {
    "dip.check_delay_days": "Buy-the-Dip's trigger is always checked exactly one trading day after the catalyst (C+1) -- this isn't adjustable yet.",
    "spike.exit_lead_hours": "Sell-the-Spike exits at the close of the first down day -- there's no intraday/hour-based exit yet.",
}


def _deep_merge(base: dict, overlay: dict) -> dict:
    result = {k: (dict(v) if isinstance(v, dict) else v) for k, v in base.items()}
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        elif key in result:
            result[key] = value
    return result


def load_ui_settings(db_path=state.DEFAULT_DB_PATH) -> dict:
    """Current settings in UI units, deep-merged over DEFAULTS so an older
    saved settings blob missing a newer field never crashes -- it just
    picks up that field's default."""
    stored = state.get_settings_raw(db_path=db_path)
    merged = _deep_merge(DEFAULTS, stored) if stored else {k: dict(v) for k, v in DEFAULTS.items()}
    merged["not_wired"] = NOT_WIRED
    return merged


def save_ui_settings(values: dict, db_path=state.DEFAULT_DB_PATH) -> dict:
    """Merges the given (possibly partial) groups onto the current
    settings and persists. Returns the full merged settings (UI units)."""
    current = load_ui_settings(db_path=db_path)
    current.pop("not_wired", None)
    incoming = {k: v for k, v in values.items() if k != "not_wired"}
    merged = _deep_merge(current, incoming)
    state.save_settings_raw(merged, db_path=db_path)
    merged["not_wired"] = NOT_WIRED
    return merged


def to_strategy_params(ui: dict) -> strat.StrategyParams:
    """Converts the UI-units settings dict into the engine's
    StrategyParams (fractions, raw dollars). Any field the dict doesn't
    have falls back to StrategyParams' own dataclass default."""
    s = ui.get("strategies", {})
    u = ui.get("universe", {})
    b = ui.get("base", {})
    i = ui.get("idle", {})
    d = ui.get("dip", {})
    p = ui.get("spike", {})

    return strat.StrategyParams(
        min_volatility=u.get("min_volatility_pct", DEFAULTS["universe"]["min_volatility_pct"]) / 100.0,
        market_cap_ceiling=u.get("max_market_cap_b", DEFAULTS["universe"]["max_market_cap_b"]) * 1_000_000_000.0,
        min_avg_dollar_volume=u.get("min_dollar_volume_m", DEFAULTS["universe"]["min_dollar_volume_m"]) * 1_000_000.0,
        catalyst_window_days=int(u.get("earnings_horizon_days", DEFAULTS["universe"]["earnings_horizon_days"])),
        enable_base_strategy=bool(s.get("base_enabled", True)),
        position_size_pct=b.get("base_allocation_pct", DEFAULTS["base"]["base_allocation_pct"]) / 100.0,
        max_positions_per_sector=int(b.get("sector_limit", DEFAULTS["base"]["sector_limit"])),
        stop_loss_pct=b.get("trailing_stop_pct", DEFAULTS["base"]["trailing_stop_pct"]) / 100.0,
        stagnation_start_days=int(b.get("stagnation_window_days", DEFAULTS["base"]["stagnation_window_days"])),
        stagnation_min_gain_pct=b.get("trend_threshold_pct", DEFAULTS["base"]["trend_threshold_pct"]) / 100.0,
        trend_aware=True,
        trend_window_days=int(b.get("trend_window_sessions", DEFAULTS["base"]["trend_window_sessions"])),
        idle_cash_sweep_enabled=bool(s.get("idle_sweep_enabled", False)),
        idle_cash_sweep_ticker=(i.get("sweep_ticker") or DEFAULTS["idle"]["sweep_ticker"]).strip().upper(),
        idle_cash_min_holding_days=int(i.get("min_holding_days", DEFAULTS["idle"]["min_holding_days"])),
        enable_o1_dip_buy=bool(s.get("dip_enabled", False)),
        o1_decline_threshold_pct=d.get("threshold_pct", DEFAULTS["dip"]["threshold_pct"]) / 100.0,
        o1_position_size_pct=d.get("allocation_pct", DEFAULTS["dip"]["allocation_pct"]) / 100.0,
        o1_exit_duration_days=int(d.get("holding_sessions", DEFAULTS["dip"]["holding_sessions"])),
        enable_o2_momentum_buy=bool(s.get("spike_enabled", False)),
        o2_increase_threshold_pct=p.get("threshold_pct", DEFAULTS["spike"]["threshold_pct"]) / 100.0,
        o2_position_size_pct=p.get("allocation_pct", DEFAULTS["spike"]["allocation_pct"]) / 100.0,
    )


def load_params(db_path=state.DEFAULT_DB_PATH) -> strat.StrategyParams:
    """Convenience: current settings straight to StrategyParams, for
    daily_job and the candidate scanner to consume."""
    return to_strategy_params(load_ui_settings(db_path=db_path))


def load_entry_lead_days(db_path=state.DEFAULT_DB_PATH) -> int:
    """entry_lead_days doesn't map to any StrategyParams field (it's
    advisory-only, see module docstring) -- read separately from the raw
    UI settings for the candidate scanner's "recommended entry date"."""
    ui = load_ui_settings(db_path=db_path)
    try:
        return max(0, int(ui["base"]["entry_lead_days"]))
    except (KeyError, TypeError, ValueError):
        return DEFAULTS["base"]["entry_lead_days"]
