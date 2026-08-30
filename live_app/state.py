"""
live_app/state.py

Persisted live positions + a decision history log. SQLite, one file.

Schema note: a ticker can now hold more than one open lot at once -- a
base position (from the C-1 entry) plus at most one Mechanism O add-on
lot (O1 or O2), linked back to its base position via `parent_id`. Rows
are keyed by an integer `id`, not by ticker.

If you deployed an earlier version of this app, its `positions` table used
`ticker` as the primary key and can't hold an add-on lot alongside a base
position. Delete the old state DB file before your first run of this
version -- there's no in-place migration, and no real deployment has
persisted data to migrate yet as of this change.

A note on durability (worth knowing before you rely on this): a free
Render web service's disk survives while the service keeps running
(including sleep/wake cycles from inactivity), but a NEW DEPLOY wipes it.
That means redeploying this app (pushing a code change) resets live
state unless you've set `STATE_DB_PATH` to a Render persistent disk
(a paid add-on) or an external database. Fine to start with; worth
upgrading before you trust it with real capital tracking long-term --
the same "known, accepted limitation" spirit as the rest of this spec.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

DEFAULT_DB_PATH = Path(os.environ.get("STATE_DB_PATH", str(Path.home() / ".model3" / "live_state.db")))

SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    lot_type TEXT NOT NULL DEFAULT 'base',   -- 'base' | 'o1' | 'o2'
    parent_id INTEGER,                        -- for an o1/o2 lot: its base position's id
    entry_date TEXT NOT NULL,
    entry_price REAL NOT NULL,
    dollar_amount REAL NOT NULL,
    shares REAL NOT NULL,
    catalyst_date TEXT NOT NULL,
    sector TEXT,
    peak_price REAL NOT NULL,
    stagnation_deferred INTEGER NOT NULL DEFAULT 0,
    next_stagnation_check_date TEXT,
    addon_evaluated INTEGER NOT NULL DEFAULT 0,  -- 'base' lots only: has the C+1 O1/O2 check run?
    status TEXT NOT NULL DEFAULT 'open',   -- 'open' | 'closed'
    exit_date TEXT,
    exit_price REAL,
    exit_reason TEXT,
    merged_into_base INTEGER NOT NULL DEFAULT 0,
    added_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS decision_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at TEXT NOT NULL,
    ticker TEXT,
    action TEXT NOT NULL,
    detail TEXT NOT NULL    -- JSON blob
);

-- Single-row table (id always 1): the strategy parameters, editable from
-- the dashboard itself. Stored as one JSON blob rather than a column per
-- field so adding a new tunable parameter never needs a migration --
-- settings.py holds the typed schema/defaults/unit-conversion layer on
-- top of this.
CREATE TABLE IF NOT EXISTS settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    settings_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Deposits/withdrawals of cash into the tracked portfolio. Combined with
-- every position's dollar_amount/exit proceeds, this is what lets a real
-- cash balance be computed rather than stored/duplicated (see
-- compute_cash_balance) -- there's no separate running total to drift out
-- of sync.
CREATE TABLE IF NOT EXISTS cash_adjustments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    direction TEXT NOT NULL,   -- 'deposit' | 'withdrawal'
    amount REAL NOT NULL,
    note TEXT,
    effective_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- One row per idle-cash-sweep buy/sell, purely a record for the activity
-- feed and for computing the swept holding's cost basis -- the live
-- *current* sweep position (shares held right now) is idle_sweep_state.
CREATE TABLE IF NOT EXISTS idle_sweep_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date TEXT NOT NULL,
    action TEXT NOT NULL,     -- 'buy' | 'sell'
    ticker TEXT NOT NULL,
    shares REAL NOT NULL,
    price REAL NOT NULL,
    amount REAL NOT NULL,
    fees REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

-- Single-row table (id always 1): the idle-cash-sweep's current live
-- position -- how many shares are held right now, and how many
-- consecutive days cash has been sitting unused (the live-loop
-- equivalent of the backtester's in-memory idle_days_counter, which
-- needs to persist here since the live job only runs once/day).
CREATE TABLE IF NOT EXISTS idle_sweep_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    ticker TEXT NOT NULL,
    shares REAL NOT NULL DEFAULT 0,
    idle_days_counter INTEGER NOT NULL DEFAULT 0,
    last_checked_date TEXT
);

-- One row per daily job run: total portfolio value at that point, so the
-- performance chart is a real series instead of invented sample points.
CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_date TEXT NOT NULL UNIQUE,
    cash_balance REAL NOT NULL,
    positions_value REAL NOT NULL,
    sweep_value REAL NOT NULL,
    total_value REAL NOT NULL,
    created_at TEXT NOT NULL
);

-- Raw rows from an imported Wealthsimple/Yahoo CSV, kept for audit/undo
-- even after a row has been applied (or explicitly skipped) via the
-- review-and-confirm import flow -- see csv_import.py.
CREATE TABLE IF NOT EXISTS portfolio_imports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,       -- 'wealthsimple' | 'yahoo'
    file_name TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    row_count INTEGER NOT NULL,
    rows_json TEXT NOT NULL,    -- JSON list of the parsed CSV rows
    applied_json TEXT           -- JSON: which rows were applied and how (set on /apply)
);

-- A day's worth of cache for one ticker's earnings dates, so the daily
-- candidate scan doesn't re-fetch all ~200+ tickers from Yahoo Finance
-- every single run (the main driver of Yahoo's rate-limiting -- see
-- daily_job.py's _scan_upcoming_catalysts). Purely a performance cache:
-- deliberately left out of export_all/import_all since it's rebuildable
-- and not real user data -- a restore just starts it empty again.
CREATE TABLE IF NOT EXISTS earnings_date_cache (
    ticker TEXT PRIMARY KEY,
    catalyst_dates_json TEXT NOT NULL,   -- JSON list of ISO date strings, as Yahoo returned
    fetched_at TEXT NOT NULL
);

-- Single-row record of the most recent scan's universe size, so the
-- dashboard can show "N of M tickers cached" without re-fetching the
-- universe (a live Wikipedia scrape/fallback) on every page load -- it
-- only needs to change once per actual scan, not once per dashboard
-- visit. Purely a display convenience, like earnings_date_cache: left
-- out of export_all/import_all, rebuilds itself on the next scan.
CREATE TABLE IF NOT EXISTS scan_universe_stats (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    universe_size INTEGER NOT NULL,
    scanned_at TEXT NOT NULL
);

-- Per-ticker screening inputs (volatility, avg dollar volume, latest
-- close, shares outstanding), cached for the same reason as
-- earnings_date_cache above: fetching real price history and shares
-- outstanding is the actually expensive part of a scan, and the earnings
-- cache alone didn't save it -- a ticker with ANY upcoming earnings date
-- (nearly all of them, at a wide horizon) still needed both live-fetched
-- on every single run, which is why repeated same-day refreshes made no
-- forward progress at all. Cache the raw STATS, not the pass/fail
-- decision, so a settings change (the universe filter sliders) is picked
-- up immediately without needing to invalidate this cache -- same
-- principle as caching the unfiltered earnings-date list. Purely a
-- performance cache: left out of export_all/import_all, rebuilds itself.
CREATE TABLE IF NOT EXISTS screen_input_cache (
    ticker TEXT PRIMARY KEY,
    volatility REAL,
    avg_dollar_volume REAL,
    last_close REAL NOT NULL,
    shares_outstanding REAL NOT NULL,
    fetched_at TEXT NOT NULL
);

-- Where the next scan should start in the universe list (a plain
-- position offset, not a ticker identity -- the universe can reorder
-- slightly over time, and this only needs to roughly spread coverage
-- across runs, not track a specific ticker exactly). Without this, a
-- scan that stops early (time budget) always restarts at position 0 next
-- time, so tickers past whatever position the budget cuts off at are
-- never reached, ever, no matter how many times it runs. Purely a
-- performance/coverage aid: left out of export_all/import_all.
CREATE TABLE IF NOT EXISTS scan_cursor (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    offset_value INTEGER NOT NULL
);

-- One row per "ACTION RECOMMENDED" email actually sent, so a recommendation
-- that keeps re-triggering every day it's not acted on (e.g. a stop-loss
-- that's still breached) only ever emails once, not once per daily job
-- run. Real user-meaningful state (affects whether a Render redeploy's
-- restore causes a duplicate re-alert), unlike the caches above -- IS
-- included in export_all/import_all.
CREATE TABLE IF NOT EXISTS sent_action_alerts (
    action TEXT NOT NULL,
    alert_key TEXT NOT NULL,
    ticker TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    PRIMARY KEY (action, alert_key)
);
"""


@contextmanager
def _connect(db_path: Path = DEFAULT_DB_PATH):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


@dataclass
class LivePosition:
    ticker: str
    entry_date: date
    entry_price: float
    dollar_amount: float
    shares: float
    catalyst_date: date
    sector: Optional[str]
    peak_price: float
    id: Optional[int] = None
    lot_type: str = "base"  # 'base' | 'o1' | 'o2'
    parent_id: Optional[int] = None
    stagnation_deferred: bool = False
    next_stagnation_check_date: Optional[date] = None
    addon_evaluated: bool = False
    status: str = "open"
    exit_date: Optional[date] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None
    merged_into_base: bool = False


def _row_to_position(row) -> LivePosition:
    return LivePosition(
        id=row["id"],
        ticker=row["ticker"],
        lot_type=row["lot_type"],
        parent_id=row["parent_id"],
        entry_date=date.fromisoformat(row["entry_date"]),
        entry_price=row["entry_price"],
        dollar_amount=row["dollar_amount"],
        shares=row["shares"],
        catalyst_date=date.fromisoformat(row["catalyst_date"]),
        sector=row["sector"],
        peak_price=row["peak_price"],
        stagnation_deferred=bool(row["stagnation_deferred"]),
        next_stagnation_check_date=(
            date.fromisoformat(row["next_stagnation_check_date"])
            if row["next_stagnation_check_date"]
            else None
        ),
        addon_evaluated=bool(row["addon_evaluated"]),
        status=row["status"],
        exit_date=date.fromisoformat(row["exit_date"]) if row["exit_date"] else None,
        exit_price=row["exit_price"],
        exit_reason=row["exit_reason"],
        merged_into_base=bool(row["merged_into_base"]),
    )


def add_position(position: LivePosition, db_path: Path = DEFAULT_DB_PATH) -> int:
    """Inserts a new position row (base or add-on) and sets/returns its id.
    Use update_position for an existing row (position.id already set)."""
    with _connect(db_path) as conn:
        cur = conn.execute(
            """INSERT INTO positions
               (ticker, lot_type, parent_id, entry_date, entry_price, dollar_amount, shares,
                catalyst_date, sector, peak_price, stagnation_deferred, next_stagnation_check_date,
                addon_evaluated, status, exit_date, exit_price, exit_reason, merged_into_base, added_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                position.ticker, position.lot_type, position.parent_id,
                position.entry_date.isoformat(), position.entry_price, position.dollar_amount,
                position.shares, position.catalyst_date.isoformat(), position.sector, position.peak_price,
                int(position.stagnation_deferred),
                position.next_stagnation_check_date.isoformat() if position.next_stagnation_check_date else None,
                int(position.addon_evaluated), position.status,
                position.exit_date.isoformat() if position.exit_date else None,
                position.exit_price, position.exit_reason, int(position.merged_into_base),
                datetime.now().isoformat(),
            ),
        )
        position.id = cur.lastrowid
        return position.id


def update_position(position: LivePosition, db_path: Path = DEFAULT_DB_PATH) -> None:
    if position.id is None:
        raise ValueError("update_position requires an existing position.id -- use add_position for a new row")
    with _connect(db_path) as conn:
        conn.execute(
            """UPDATE positions SET
                 ticker=?, lot_type=?, parent_id=?, entry_date=?, entry_price=?, dollar_amount=?, shares=?,
                 catalyst_date=?, sector=?, peak_price=?, stagnation_deferred=?, next_stagnation_check_date=?,
                 addon_evaluated=?, status=?, exit_date=?, exit_price=?, exit_reason=?, merged_into_base=?
               WHERE id=?""",
            (
                position.ticker, position.lot_type, position.parent_id,
                position.entry_date.isoformat(), position.entry_price, position.dollar_amount,
                position.shares, position.catalyst_date.isoformat(), position.sector, position.peak_price,
                int(position.stagnation_deferred),
                position.next_stagnation_check_date.isoformat() if position.next_stagnation_check_date else None,
                int(position.addon_evaluated), position.status,
                position.exit_date.isoformat() if position.exit_date else None,
                position.exit_price, position.exit_reason, int(position.merged_into_base),
                position.id,
            ),
        )


def get_position(position_id: int, db_path: Path = DEFAULT_DB_PATH) -> Optional[LivePosition]:
    with _connect(db_path) as conn:
        row = conn.execute("SELECT * FROM positions WHERE id=?", (position_id,)).fetchone()
        return _row_to_position(row) if row else None


def get_open_base_positions(db_path: Path = DEFAULT_DB_PATH) -> list[LivePosition]:
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM positions WHERE status='open' AND lot_type='base' ORDER BY entry_date"
        ).fetchall()
        return [_row_to_position(r) for r in rows]


def get_open_addon_positions(db_path: Path = DEFAULT_DB_PATH) -> list[LivePosition]:
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM positions WHERE status='open' AND lot_type IN ('o1','o2') ORDER BY entry_date"
        ).fetchall()
        return [_row_to_position(r) for r in rows]


def get_open_positions(db_path: Path = DEFAULT_DB_PATH) -> list[LivePosition]:
    """All open lots -- base and add-on together (dashboard display)."""
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT * FROM positions WHERE status='open' ORDER BY entry_date").fetchall()
        return [_row_to_position(r) for r in rows]


def get_all_positions(db_path: Path = DEFAULT_DB_PATH) -> list[LivePosition]:
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT * FROM positions ORDER BY entry_date DESC").fetchall()
        return [_row_to_position(r) for r in rows]


def close_position(
    position_id: int, exit_date: date, exit_price: float, exit_reason: str, db_path: Path = DEFAULT_DB_PATH
) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE positions SET status='closed', exit_date=?, exit_price=?, exit_reason=? WHERE id=?",
            (exit_date.isoformat(), exit_price, exit_reason, position_id),
        )


def remove_position(position_id: int, db_path: Path = DEFAULT_DB_PATH) -> None:
    """Hard delete -- for correcting a mistaken manual entry, not for a
    real exit (use close_position for that, which keeps history)."""
    with _connect(db_path) as conn:
        conn.execute("DELETE FROM positions WHERE id=?", (position_id,))


def mark_addon_evaluated(position_id: int, db_path: Path = DEFAULT_DB_PATH) -> None:
    with _connect(db_path) as conn:
        conn.execute("UPDATE positions SET addon_evaluated=1 WHERE id=?", (position_id,))


def merge_addon_into_base(
    addon: LivePosition, base: LivePosition, exit_date: date, exit_price: float, db_path: Path = DEFAULT_DB_PATH
) -> None:
    """Bookkeeping-only: folds an O1 add-on's shares into its base
    position (blended cost basis) and closes the add-on row with reason
    'merged_into_base'. No cash changes hands and nothing is bought or
    sold -- both lots were already positions the person confirmed they
    own, so this just continues tracking them correctly as one combined
    position going forward (the base's own peak_price is untouched: it
    already ratchets from the same daily price series and needs no
    adjustment)."""
    total_shares = base.shares + addon.shares
    if total_shares <= 0:
        blended_entry_price = base.entry_price
    else:
        blended_entry_price = (base.entry_price * base.shares + addon.entry_price * addon.shares) / total_shares

    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE positions SET shares=?, entry_price=?, dollar_amount=dollar_amount+? WHERE id=?",
            (total_shares, blended_entry_price, addon.dollar_amount, base.id),
        )
        conn.execute(
            "UPDATE positions SET status='closed', exit_date=?, exit_price=?, exit_reason='merged_into_base', "
            "merged_into_base=1 WHERE id=?",
            (exit_date.isoformat(), exit_price, addon.id),
        )


def log_decision(run_at: datetime, ticker: Optional[str], action: str, detail: dict, db_path: Path = DEFAULT_DB_PATH) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO decision_log (run_at, ticker, action, detail) VALUES (?,?,?,?)",
            (run_at.isoformat(), ticker, action, json.dumps(detail, default=str)),
        )


def get_recent_decisions(limit: int = 100, db_path: Path = DEFAULT_DB_PATH) -> list[dict]:
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM decision_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [
            {
                "run_at": r["run_at"],
                "ticker": r["ticker"],
                "action": r["action"],
                "detail": json.loads(r["detail"]),
            }
            for r in rows
        ]


# --------------------------------------------------------------------------- #
# Settings -- one JSON blob, so a new tunable parameter never needs a schema
# migration. settings.py owns the typed defaults/unit conversions on top.
# --------------------------------------------------------------------------- #


def get_settings_raw(db_path: Path = DEFAULT_DB_PATH) -> Optional[dict]:
    with _connect(db_path) as conn:
        row = conn.execute("SELECT settings_json FROM settings WHERE id=1").fetchone()
        return json.loads(row["settings_json"]) if row else None


def save_settings_raw(values: dict, db_path: Path = DEFAULT_DB_PATH) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO settings (id, settings_json, updated_at) VALUES (1, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET settings_json=excluded.settings_json, updated_at=excluded.updated_at",
            (json.dumps(values), datetime.now().isoformat()),
        )


# --------------------------------------------------------------------------- #
# Cash ledger -- balance is always computed from the ledger + position
# history, never stored, so there's nothing to drift out of sync.
# --------------------------------------------------------------------------- #


def add_cash_adjustment(
    direction: str, amount: float, note: Optional[str], effective_at: date, db_path: Path = DEFAULT_DB_PATH
) -> int:
    if direction not in ("deposit", "withdrawal"):
        raise ValueError("direction must be 'deposit' or 'withdrawal'")
    with _connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO cash_adjustments (direction, amount, note, effective_at, created_at) VALUES (?,?,?,?,?)",
            (direction, amount, note, effective_at.isoformat(), datetime.now().isoformat()),
        )
        return cur.lastrowid


def get_cash_adjustments(db_path: Path = DEFAULT_DB_PATH) -> list[dict]:
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT * FROM cash_adjustments ORDER BY effective_at, id").fetchall()
        return [dict(r) for r in rows]


def compute_cash_balance(db_path: Path = DEFAULT_DB_PATH) -> float:
    """Derived, not stored: deposits/withdrawals, minus the cost of every
    position ever opened (base + add-on lots), plus proceeds from every
    closed position that wasn't a cost-basis-only merge, plus idle-sweep
    buy/sell cash flow. A merged add-on moved no cash (see
    merge_addon_into_base) so it's excluded on both sides."""
    with _connect(db_path) as conn:
        deposits = conn.execute(
            "SELECT COALESCE(SUM(amount),0) FROM cash_adjustments WHERE direction='deposit'"
        ).fetchone()[0]
        withdrawals = conn.execute(
            "SELECT COALESCE(SUM(amount),0) FROM cash_adjustments WHERE direction='withdrawal'"
        ).fetchone()[0]
        spent = conn.execute("SELECT COALESCE(SUM(dollar_amount),0) FROM positions").fetchone()[0]
        proceeds = conn.execute(
            "SELECT COALESCE(SUM(exit_price * shares),0) FROM positions "
            "WHERE status='closed' AND merged_into_base=0 AND exit_price IS NOT NULL"
        ).fetchone()[0]
        sweep_buys = conn.execute(
            "SELECT COALESCE(SUM(amount + fees),0) FROM idle_sweep_events WHERE action='buy'"
        ).fetchone()[0]
        sweep_sells = conn.execute(
            "SELECT COALESCE(SUM(amount - fees),0) FROM idle_sweep_events WHERE action='sell'"
        ).fetchone()[0]
        return deposits - withdrawals - spent + proceeds - sweep_buys + sweep_sells


# --------------------------------------------------------------------------- #
# Idle cash sweep -- live equivalent of the backtester's in-memory
# sweep_shares/idle_days_counter (see portfolio.run_backtest), persisted
# here since the live job only runs once per day.
# --------------------------------------------------------------------------- #


def get_idle_sweep_state(ticker: str, db_path: Path = DEFAULT_DB_PATH) -> dict:
    with _connect(db_path) as conn:
        row = conn.execute("SELECT * FROM idle_sweep_state WHERE id=1").fetchone()
        if row is None or row["ticker"] != ticker:
            # Ticker changed (or first run) -- start fresh rather than
            # silently carrying over a different ETF's share count.
            return {"ticker": ticker, "shares": 0.0, "idle_days_counter": 0, "last_checked_date": None}
        return {
            "ticker": row["ticker"],
            "shares": row["shares"],
            "idle_days_counter": row["idle_days_counter"],
            "last_checked_date": row["last_checked_date"],
        }


def save_idle_sweep_state(
    ticker: str, shares: float, idle_days_counter: int, last_checked_date: date, db_path: Path = DEFAULT_DB_PATH
) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO idle_sweep_state (id, ticker, shares, idle_days_counter, last_checked_date) "
            "VALUES (1, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
            "ticker=excluded.ticker, shares=excluded.shares, idle_days_counter=excluded.idle_days_counter, "
            "last_checked_date=excluded.last_checked_date",
            (ticker, shares, idle_days_counter, last_checked_date.isoformat()),
        )


# --------------------------------------------------------------------------- #
# Earnings-date cache -- see the earnings_date_cache schema comment.
# --------------------------------------------------------------------------- #


def get_cached_earnings_dates(
    ticker: str, max_age_hours: float, db_path: Path = DEFAULT_DB_PATH
) -> Optional[list[date]]:
    """None means "no usable cache entry -- go fetch it"; an empty list is
    a real cached answer (Yahoo had nothing for this ticker as of last
    fetch), distinct from "we don't know yet"."""
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT catalyst_dates_json, fetched_at FROM earnings_date_cache WHERE ticker=?", (ticker,)
        ).fetchone()
    if row is None:
        return None
    fetched_at = datetime.fromisoformat(row["fetched_at"])
    if (datetime.now() - fetched_at).total_seconds() > max_age_hours * 3600:
        return None
    return [date.fromisoformat(d) for d in json.loads(row["catalyst_dates_json"])]


def save_cached_earnings_dates(ticker: str, catalyst_dates: list[date], db_path: Path = DEFAULT_DB_PATH) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO earnings_date_cache (ticker, catalyst_dates_json, fetched_at) VALUES (?, ?, ?) "
            "ON CONFLICT(ticker) DO UPDATE SET catalyst_dates_json=excluded.catalyst_dates_json, "
            "fetched_at=excluded.fetched_at",
            (ticker, json.dumps([d.isoformat() for d in catalyst_dates]), datetime.now().isoformat()),
        )


def count_fresh_earnings_cache_entries(max_age_hours: float, db_path: Path = DEFAULT_DB_PATH) -> int:
    """How many tickers currently have a usable (not-yet-stale) cached
    earnings-date entry -- the numerator for a "cached N of M tickers"
    coverage display. A stale entry (older than max_age_hours) doesn't
    count, since the scan will treat it as needing a fresh fetch too."""
    cutoff = (datetime.now() - timedelta(hours=max_age_hours)).isoformat()
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM earnings_date_cache WHERE fetched_at > ?", (cutoff,)
        ).fetchone()
    return row["n"]


def save_scan_universe_size(universe_size: int, scanned_at: datetime, db_path: Path = DEFAULT_DB_PATH) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO scan_universe_stats (id, universe_size, scanned_at) VALUES (1, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET universe_size=excluded.universe_size, "
            "scanned_at=excluded.scanned_at",
            (universe_size, scanned_at.isoformat()),
        )


def get_scan_universe_size(db_path: Path = DEFAULT_DB_PATH) -> Optional[dict]:
    """The universe size as of the most recent scan, plus when that scan
    ran -- or None if no scan has ever recorded one yet (e.g. a brand new
    deploy before the first refresh)."""
    with _connect(db_path) as conn:
        row = conn.execute("SELECT universe_size, scanned_at FROM scan_universe_stats WHERE id=1").fetchone()
    if row is None:
        return None
    return {"universe_size": row["universe_size"], "scanned_at": row["scanned_at"]}


def get_cached_screen_inputs(ticker: str, max_age_hours: float, db_path: Path = DEFAULT_DB_PATH) -> Optional[dict]:
    """A ticker's cached volatility/avg_dollar_volume/last_close/shares_outstanding,
    or None if there isn't a fresh one -- see screen_input_cache's schema
    comment for why this exists (the earnings-date cache alone didn't
    stop repeated live history/shares fetches for in-window tickers)."""
    cutoff = (datetime.now() - timedelta(hours=max_age_hours)).isoformat()
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT volatility, avg_dollar_volume, last_close, shares_outstanding "
            "FROM screen_input_cache WHERE ticker=? AND fetched_at > ?",
            (ticker, cutoff),
        ).fetchone()
    if row is None:
        return None
    return {
        "volatility": row["volatility"],
        "avg_dollar_volume": row["avg_dollar_volume"],
        "last_close": row["last_close"],
        "shares_outstanding": row["shares_outstanding"],
    }


def save_cached_screen_inputs(
    ticker: str, volatility: Optional[float], avg_dollar_volume: Optional[float],
    last_close: float, shares_outstanding: float, db_path: Path = DEFAULT_DB_PATH,
) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO screen_input_cache "
            "(ticker, volatility, avg_dollar_volume, last_close, shares_outstanding, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(ticker) DO UPDATE SET volatility=excluded.volatility, "
            "avg_dollar_volume=excluded.avg_dollar_volume, last_close=excluded.last_close, "
            "shares_outstanding=excluded.shares_outstanding, fetched_at=excluded.fetched_at",
            (ticker, volatility, avg_dollar_volume, last_close, shares_outstanding, datetime.now().isoformat()),
        )


def get_scan_offset(db_path: Path = DEFAULT_DB_PATH) -> int:
    """Where the next scan should start in the universe list. Defaults to
    0 (start of the list) if no scan has ever recorded a stopping point."""
    with _connect(db_path) as conn:
        row = conn.execute("SELECT offset_value FROM scan_cursor WHERE id=1").fetchone()
    return row["offset_value"] if row is not None else 0


def save_scan_offset(offset: int, db_path: Path = DEFAULT_DB_PATH) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO scan_cursor (id, offset_value) VALUES (1, ?) "
            "ON CONFLICT(id) DO UPDATE SET offset_value=excluded.offset_value",
            (offset,),
        )


def has_alert_been_sent(action: str, alert_key: str, db_path: Path = DEFAULT_DB_PATH) -> bool:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM sent_action_alerts WHERE action=? AND alert_key=?", (action, alert_key)
        ).fetchone()
    return row is not None


def mark_alert_sent(action: str, alert_key: str, ticker: str, sent_at: datetime, db_path: Path = DEFAULT_DB_PATH) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO sent_action_alerts (action, alert_key, ticker, sent_at) VALUES (?, ?, ?, ?)",
            (action, alert_key, ticker, sent_at.isoformat()),
        )


def log_idle_sweep_event(
    run_date: date, action: str, ticker: str, shares: float, price: float, amount: float, fees: float,
    db_path: Path = DEFAULT_DB_PATH,
) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO idle_sweep_events (run_date, action, ticker, shares, price, amount, fees, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (run_date.isoformat(), action, ticker, shares, price, amount, fees, datetime.now().isoformat()),
        )


def get_idle_sweep_events(limit: int = 100, db_path: Path = DEFAULT_DB_PATH) -> list[dict]:
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM idle_sweep_events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# Portfolio snapshots -- the real performance-over-time series.
# --------------------------------------------------------------------------- #


def record_portfolio_snapshot(
    snapshot_date: date, cash_balance: float, positions_value: float, sweep_value: float,
    db_path: Path = DEFAULT_DB_PATH,
) -> None:
    total_value = cash_balance + positions_value + sweep_value
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO portfolio_snapshots (snapshot_date, cash_balance, positions_value, sweep_value, total_value, created_at) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(snapshot_date) DO UPDATE SET "
            "cash_balance=excluded.cash_balance, positions_value=excluded.positions_value, "
            "sweep_value=excluded.sweep_value, total_value=excluded.total_value",
            (snapshot_date.isoformat(), cash_balance, positions_value, sweep_value, total_value, datetime.now().isoformat()),
        )


def get_portfolio_snapshots(limit: int = 400, db_path: Path = DEFAULT_DB_PATH) -> list[dict]:
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM portfolio_snapshots ORDER BY snapshot_date DESC LIMIT ?", (limit,)
        ).fetchall()
        return list(reversed([dict(r) for r in rows]))


# --------------------------------------------------------------------------- #
# CSV import staging -- raw parsed rows, kept for audit even after applied.
# --------------------------------------------------------------------------- #


def save_portfolio_import(source: str, file_name: str, rows: list[dict], db_path: Path = DEFAULT_DB_PATH) -> int:
    with _connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO portfolio_imports (source, file_name, imported_at, row_count, rows_json, applied_json) "
            "VALUES (?,?,?,?,?,NULL)",
            (source, file_name, datetime.now().isoformat(), len(rows), json.dumps(rows)),
        )
        return cur.lastrowid


def get_portfolio_import(import_id: int, db_path: Path = DEFAULT_DB_PATH) -> Optional[dict]:
    with _connect(db_path) as conn:
        row = conn.execute("SELECT * FROM portfolio_imports WHERE id=?", (import_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["rows"] = json.loads(d.pop("rows_json"))
        d["applied"] = json.loads(d.pop("applied_json")) if d.get("applied_json") else None
        return d


def mark_portfolio_import_applied(import_id: int, applied: list[dict], db_path: Path = DEFAULT_DB_PATH) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE portfolio_imports SET applied_json=? WHERE id=?", (json.dumps(applied), import_id)
        )


# --------------------------------------------------------------------------- #
# Full-state backup -- Mike's chosen durability mechanism in place of a
# paid Render persistent disk: export before a redeploy, import after.
# --------------------------------------------------------------------------- #


def export_all(db_path: Path = DEFAULT_DB_PATH) -> dict:
    with _connect(db_path) as conn:
        def _all(table: str) -> list[dict]:
            return [dict(r) for r in conn.execute(f"SELECT * FROM {table}").fetchall()]

        return {
            "exported_at": datetime.now().isoformat(),
            "version": 1,
            "positions": _all("positions"),
            "decision_log": _all("decision_log"),
            "settings": _all("settings"),
            "cash_adjustments": _all("cash_adjustments"),
            "idle_sweep_events": _all("idle_sweep_events"),
            "idle_sweep_state": _all("idle_sweep_state"),
            "portfolio_snapshots": _all("portfolio_snapshots"),
            "portfolio_imports": _all("portfolio_imports"),
            # Real user-meaningful state (not a rebuildable cache like
            # earnings_date_cache/screen_input_cache/scan_cursor): without
            # it, a post-redeploy restore would forget which "ACTION
            # RECOMMENDED" emails were already sent, and could re-alert on
            # something the user already saw before the redeploy.
            "sent_action_alerts": _all("sent_action_alerts"),
        }


def import_all(bundle: dict, db_path: Path = DEFAULT_DB_PATH) -> None:
    """Replaces every table's contents with the backup's rows. Meant for
    restoring onto a freshly-redeployed (empty) database -- not a merge."""
    tables = [
        "positions", "decision_log", "settings", "cash_adjustments",
        "idle_sweep_events", "idle_sweep_state", "portfolio_snapshots", "portfolio_imports",
        "sent_action_alerts",
    ]
    with _connect(db_path) as conn:
        for table in tables:
            rows = bundle.get(table) or []
            conn.execute(f"DELETE FROM {table}")
            for row in rows:
                cols = list(row.keys())
                placeholders = ",".join("?" for _ in cols)
                conn.execute(
                    f"INSERT INTO {table} ({','.join(cols)}) VALUES ({placeholders})",
                    [row[c] for c in cols],
                )
