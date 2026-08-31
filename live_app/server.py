"""
live_app/server.py

JSON API + static-file host for the dashboard (a Vite-built React SPA
served from live_app/static/). Positions are keyed by an integer id, not
by ticker (see state.py's schema note) -- a ticker can hold a base
position plus at most one Mechanism O add-on lot (O1 or O2) at the same
time, linked via parent_id.

Also exposes the endpoint an external free cron pinger hits once a day
after market close to run the daily job (see daily_job.py) -- the
pragmatic way to get a reliable daily trigger on a free hosting tier
without paying for a separate always-on scheduler process.

Everything under /api/* (other than /api/run-daily-job, which is
token-gated for the cron pinger, and /healthz) requires HTTP Basic Auth,
same as the SPA shell itself -- this is a private dashboard showing real
position and cash data.
"""

from __future__ import annotations

import os
from datetime import date, datetime
from functools import wraps
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory

from strategy_core import data_sources as ds
from . import csv_import, daily_job, github_backup, settings as live_settings, state

STATIC_DIR = Path(__file__).parent / "static"

app = Flask(__name__, static_folder=None)

DASHBOARD_USER = os.environ.get("DASHBOARD_USER")
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD")
CRON_SECRET = os.environ.get("CRON_SECRET")


def _maybe_restore_from_github_backup() -> bool:
    """Runs once per process start (called at module load, right below).
    Render's free tier has no persistent disk, so a cold start after the
    instance sleeps (its normal behavior after ~15 minutes idle, not just
    a code redeploy) boots into a fresh, empty SQLite file -- genuinely
    indistinguishable from a brand-new deploy. If that's what just
    happened, pull the last automated backup (see github_backup.py,
    pushed after every daily_job.run_once()) and restore it, so the app
    doesn't quietly show $0.00/0 positions/0 candidates after every nap.
    Best-effort and silent if unconfigured or the pull fails -- must
    never be why the server fails to start. Returns True only on an
    actual restore, so callers/tests can tell a no-op from a restore."""
    try:
        if not state.is_fresh_database():
            return False
        bundle = github_backup.pull_backup()
        if not bundle:
            return False
        state.import_all(bundle)
        return True
    except Exception:
        return False


_maybe_restore_from_github_backup()

LOT_LABELS = {"base": "Base", "o1": "O1", "o2": "O2"}


def require_auth(view):
    """HTTP Basic Auth gate. If DASHBOARD_USER/PASSWORD aren't set, the
    app still runs (useful for local testing) but everything is wide
    open -- don't deploy publicly without setting these, since this
    dashboard shows real position and cash data."""

    @wraps(view)
    def wrapped(*args, **kwargs):
        if not DASHBOARD_USER or not DASHBOARD_PASSWORD:
            return view(*args, **kwargs)
        auth = request.authorization
        if not auth or auth.username != DASHBOARD_USER or auth.password != DASHBOARD_PASSWORD:
            return Response(
                "Authentication required.", 401, {"WWW-Authenticate": 'Basic realm="Signal Ledger"'}
            )
        return view(*args, **kwargs)

    return wrapped


def _err(message: str, status: int = 400):
    return jsonify({"error": message}), status


# --------------------------------------------------------------------------- #
# Serialization helpers
# --------------------------------------------------------------------------- #


def _last_known_price(ticker: str, recent: list[dict]) -> tuple[float | None, str | None]:
    """Most recent price the daily job (or a manual refresh) actually
    observed for this ticker, from the decision log -- avoids a live
    network fetch on every dashboard page load. None until the first
    refresh/daily-job run after a position is added."""
    for entry in recent:
        if entry["ticker"] != ticker:
            continue
        detail = entry.get("detail") or {}
        price = detail.get("price")
        if price is not None:
            return float(price), entry["run_at"]
    return None, None


def _position_to_dict(p: "state.LivePosition", recent: list[dict]) -> dict:
    price, checked_at = _last_known_price(p.ticker, recent)
    return {
        "id": p.id,
        "ticker": p.ticker,
        "lot_type": p.lot_type,
        "lot_label": LOT_LABELS.get(p.lot_type, p.lot_type),
        "parent_id": p.parent_id,
        "entry_date": p.entry_date.isoformat(),
        "entry_price": p.entry_price,
        "dollar_amount": p.dollar_amount,
        "shares": p.shares,
        "catalyst_date": p.catalyst_date.isoformat(),
        "sector": p.sector,
        "status": p.status,
        "exit_date": p.exit_date.isoformat() if p.exit_date else None,
        "exit_price": p.exit_price,
        "exit_reason": p.exit_reason,
        "merged_into_base": p.merged_into_base,
        "last_known_price": price,
        "last_checked_at": checked_at,
    }


ACTIVITY_LABELS = {
    "exit_recommended": ("Exit recommended", "warning"),
    "hold": ("Holding", "info"),
    "new_candidate": ("New candidate found", "info"),
    "addon_trigger_recommended": ("Add-on buy opportunity", "opportunity"),
    "addon_exit_recommended": ("Add-on exit recommended", "warning"),
    "addon_merged": ("Add-on merged into base", "info"),
    "idle_sweep_buy": ("Idle cash swept in", "info"),
    "idle_sweep_sell": ("Idle cash sweep sold", "info"),
    "position_added": ("Position recorded", "info"),
    "addon_position_added": ("Add-on lot recorded", "info"),
    "manual_exit": ("Position closed", "info"),
    "position_removed": ("Position removed", "info"),
    "error": ("Problem during a run", "error"),
    "scan_diagnostic": ("Candidate scan found nothing", "warning"),
}


def _activity_to_card(entry: dict) -> dict:
    label, category = ACTIVITY_LABELS.get(entry["action"], (entry["action"], "info"))
    return {
        "ticker": entry["ticker"],
        "action": entry["action"],
        "label": label,
        "category": category,
        "detail": entry["detail"],
        "time": entry["run_at"],
    }


# --------------------------------------------------------------------------- #
# Positions
# --------------------------------------------------------------------------- #


@app.route("/api/positions", methods=["GET"])
@require_auth
def list_positions():
    recent = state.get_recent_decisions(limit=500)
    all_positions = state.get_all_positions()
    open_positions = [_position_to_dict(p, recent) for p in all_positions if p.status == "open"]
    closed_positions = [_position_to_dict(p, recent) for p in all_positions if p.status == "closed"]
    open_base_ids_with_addon = {
        p.parent_id for p in all_positions if p.status == "open" and p.lot_type in ("o1", "o2") and p.parent_id
    }
    available_base_positions = [
        {"id": p.id, "ticker": p.ticker, "entry_date": p.entry_date.isoformat(), "catalyst_date": p.catalyst_date.isoformat()}
        for p in all_positions
        if p.status == "open" and p.lot_type == "base" and p.id not in open_base_ids_with_addon
    ]
    return jsonify({"open": open_positions, "closed": closed_positions, "available_base_positions": available_base_positions})


@app.route("/api/positions/<int:position_id>/history", methods=["GET"])
@require_auth
def position_history(position_id):
    p = state.get_position(position_id)
    if p is None:
        return _err("position not found", 404)
    fetch_start = p.entry_date
    fetch_end = p.exit_date or date.today()
    try:
        history = ds.fetch_price_history(p.ticker, fetch_start, fetch_end)
    except Exception as e:
        return _err(f"could not fetch price history: {e}", 502)

    points = [{"date": d.date().isoformat() if hasattr(d, "date") else d.isoformat(), "price": float(c)} for d, c in zip(history.index, history["Close"])]
    events = [{"date": p.entry_date.isoformat(), "label": f"{LOT_LABELS.get(p.lot_type, p.lot_type)} entry", "kind": "entry"}]
    events.append({"date": p.catalyst_date.isoformat(), "label": "Catalyst", "kind": "catalyst"})
    if p.status == "closed" and p.exit_date:
        reason_label = "Merged into base" if p.merged_into_base else (p.exit_reason or "Exit")
        events.append({"date": p.exit_date.isoformat(), "label": reason_label, "kind": p.exit_reason or "exit"})
    return jsonify({"points": points, "events": events})


def _backfill_peak_price(ticker: str, entry_date: date, entry_price: float) -> float:
    """For a position entered after the fact, the trailing-stop peak
    needs to start from the REAL price history since entry_date, not
    reset to entry_price. Falls back to entry_price if the fetch fails."""
    if entry_date >= date.today():
        return entry_price
    try:
        history = ds.fetch_price_history(ticker, entry_date, date.today())
        if not history.empty:
            return max(entry_price, float(history["Close"].max()))
    except Exception:
        pass
    return entry_price


@app.route("/api/positions", methods=["POST"])
@require_auth
def add_position():
    body = request.get_json(force=True, silent=True) or {}
    try:
        entry_price = float(body["entry_price"])
        shares = float(body["shares"])
        ticker = str(body["ticker"]).strip().upper()
        entry_date = date.fromisoformat(body["entry_date"])
        catalyst_date = date.fromisoformat(body["catalyst_date"])
    except (KeyError, ValueError) as e:
        return _err(f"invalid or missing field: {e}")

    peak_price = _backfill_peak_price(ticker, entry_date, entry_price)
    position = state.LivePosition(
        ticker=ticker, entry_date=entry_date, entry_price=entry_price, dollar_amount=shares * entry_price,
        shares=shares, catalyst_date=catalyst_date,
        sector=body.get("sector") or None, peak_price=peak_price, lot_type="base",
    )
    position_id = state.add_position(position)
    state.log_decision(
        datetime.now(), ticker, "position_added",
        {"position_id": position_id, "entry_date": entry_date.isoformat(), "entry_price": entry_price,
         "backdated": entry_date < date.today()},
    )
    return jsonify({"position": _position_to_dict(position, [])}), 201


@app.route("/api/positions/addon", methods=["POST"])
@require_auth
def add_addon_position():
    body = request.get_json(force=True, silent=True) or {}
    try:
        parent_id = int(body["parent_id"])
        lot_type = body["lot_type"]
        entry_price = float(body["entry_price"])
        shares = float(body["shares"])
        entry_date = date.fromisoformat(body["entry_date"])
    except (KeyError, ValueError) as e:
        return _err(f"invalid or missing field: {e}")
    if lot_type not in ("o1", "o2"):
        return _err("lot_type must be o1 or o2")

    base = state.get_position(parent_id)
    if base is None or base.lot_type != "base":
        return _err("base position not found")

    peak_price = _backfill_peak_price(base.ticker, entry_date, entry_price)
    addon = state.LivePosition(
        ticker=base.ticker, entry_date=entry_date, entry_price=entry_price, dollar_amount=shares * entry_price,
        shares=shares, catalyst_date=base.catalyst_date,
        sector=base.sector, peak_price=peak_price, lot_type=lot_type, parent_id=base.id,
    )
    addon_id = state.add_position(addon)
    state.log_decision(
        datetime.now(), base.ticker, "addon_position_added",
        {"position_id": addon_id, "parent_id": base.id, "lot_type": lot_type,
         "entry_date": entry_date.isoformat(), "entry_price": entry_price},
    )
    return jsonify({"position": _position_to_dict(addon, [])}), 201


@app.route("/api/positions/<int:position_id>", methods=["PUT"])
@require_auth
def update_position_route(position_id):
    """Edits quantity/entry price of an existing position, or reopens a
    closed one. dollar_amount is recomputed from quantity*entry price so
    the cash ledger (state.compute_cash_balance) stays consistent."""
    p = state.get_position(position_id)
    if p is None:
        return _err("position not found", 404)
    body = request.get_json(force=True, silent=True) or {}

    if "shares" in body:
        p.shares = float(body["shares"])
    if "entry_price" in body:
        p.entry_price = float(body["entry_price"])
    p.dollar_amount = p.shares * p.entry_price

    if body.get("reopen"):
        p.status = "open"
        p.exit_date = None
        p.exit_price = None
        p.exit_reason = None

    state.update_position(p)
    state.log_decision(datetime.now(), p.ticker, "position_edited", {"position_id": position_id, **body})
    return jsonify({"position": _position_to_dict(p, [])})


@app.route("/api/positions/<int:position_id>/close", methods=["POST"])
@require_auth
def close_position_route(position_id):
    body = request.get_json(force=True, silent=True) or {}
    p = state.get_position(position_id)
    if p is None:
        return _err("position not found", 404)
    if body.get("exit_price") not in (None, ""):
        exit_price = float(body["exit_price"])
    else:
        # No exit price given -- fetch a real live price rather than ever
        # fabricating one. Silently falling back to entry_price here would
        # record a fake flat P&L as permanent trade history.
        try:
            exit_price = ds.fetch_current_price(p.ticker)
        except Exception:
            exit_price = None
        if exit_price is None:
            return _err(
                f"couldn't fetch a live price for {p.ticker} to use as the exit price -- enter one manually.",
                502,
            )
    exit_date = date.fromisoformat(body["exit_date"]) if body.get("exit_date") else date.today()
    reason_text = (body.get("reason") or "").strip()
    exit_reason = f"manual: {reason_text}" if reason_text else "manual"

    state.close_position(position_id, exit_date, exit_price, exit_reason)
    state.log_decision(
        datetime.now(), p.ticker, "manual_exit",
        {"position_id": position_id, "exit_date": exit_date.isoformat(), "exit_price": exit_price, "reason": exit_reason},
    )
    return jsonify({"ok": True})


@app.route("/api/positions/<int:position_id>/remove", methods=["POST"])
@require_auth
def remove_position_route(position_id):
    p = state.get_position(position_id)
    state.remove_position(position_id)
    state.log_decision(datetime.now(), p.ticker if p else None, "position_removed", {"position_id": position_id})
    return jsonify({"ok": True})


# --------------------------------------------------------------------------- #
# Candidates + activity feed
# --------------------------------------------------------------------------- #


@app.route("/api/candidates", methods=["GET"])
@require_auth
def candidates():
    recent = state.get_recent_decisions(limit=200)
    seen = set()
    results = []
    for entry in recent:
        if entry["action"] == "new_candidate" and entry["ticker"] not in seen:
            results.append(entry["detail"])
            seen.add(entry["ticker"])
        if len(results) >= 25:
            break

    # Cache coverage: how much of the universe has a fresh, usable
    # earnings-date cache entry right now, vs. the universe size as of
    # the most recent scan -- makes it obvious in the UI when a "zero
    # candidates" result might just mean the scan hasn't reached every
    # ticker yet (see daily_job.py's stopped_early diagnostic).
    cached = state.count_fresh_earnings_cache_entries(daily_job._EARNINGS_CACHE_MAX_AGE_HOURS)
    universe_stats = state.get_scan_universe_size()
    cache_coverage = {
        "cached": cached,
        "universe_size": universe_stats["universe_size"] if universe_stats else None,
        "as_of": universe_stats["scanned_at"] if universe_stats else None,
    }
    return jsonify({"candidates": results, "cache_coverage": cache_coverage})


@app.route("/api/activity", methods=["GET"])
@require_auth
def activity():
    limit = int(request.args.get("limit", 50))
    recent = state.get_recent_decisions(limit=limit)
    return jsonify({"activity": [_activity_to_card(e) for e in recent]})


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #


@app.route("/api/settings", methods=["GET"])
@require_auth
def get_settings():
    return jsonify(live_settings.load_ui_settings())


@app.route("/api/settings", methods=["PUT"])
@require_auth
def put_settings():
    body = request.get_json(force=True, silent=True) or {}
    merged = live_settings.save_ui_settings(body)
    state.log_decision(datetime.now(), None, "settings_updated", body)
    return jsonify(merged)


# --------------------------------------------------------------------------- #
# Cash + idle sweep
# --------------------------------------------------------------------------- #


@app.route("/api/cash", methods=["GET"])
@require_auth
def get_cash():
    return jsonify({"balance": state.compute_cash_balance(), "adjustments": state.get_cash_adjustments()})


@app.route("/api/cash", methods=["POST"])
@require_auth
def post_cash():
    body = request.get_json(force=True, silent=True) or {}
    direction = body.get("direction")
    try:
        amount = float(body["amount"])
    except (KeyError, ValueError, TypeError):
        return _err("amount is required and must be a number")
    if direction not in ("deposit", "withdrawal") or amount <= 0:
        return _err("direction must be 'deposit' or 'withdrawal', amount must be > 0")
    effective_at = date.fromisoformat(body["effective_at"]) if body.get("effective_at") else date.today()
    state.add_cash_adjustment(direction, amount, body.get("note"), effective_at)
    state.log_decision(datetime.now(), None, "cash_adjusted", {"direction": direction, "amount": amount, "note": body.get("note")})
    return jsonify({"balance": state.compute_cash_balance()}), 201


@app.route("/api/idle-sweep", methods=["GET"])
@require_auth
def get_idle_sweep():
    params = live_settings.load_params()
    sweep_state = state.get_idle_sweep_state(params.idle_cash_sweep_ticker)
    return jsonify({"state": sweep_state, "events": state.get_idle_sweep_events(limit=50)})


@app.route("/api/idle-sweep/sell", methods=["POST"])
@require_auth
def post_idle_sweep_sell():
    try:
        result = daily_job.sell_idle_sweep_now()
    except Exception as e:
        return _err(str(e), 502)
    if result is None:
        return _err("nothing to sell -- the idle sweep isn't holding any shares right now")
    return jsonify(result)


# --------------------------------------------------------------------------- #
# Performance snapshots
# --------------------------------------------------------------------------- #


@app.route("/api/snapshots", methods=["GET"])
@require_auth
def snapshots():
    return jsonify({"snapshots": state.get_portfolio_snapshots()})


# --------------------------------------------------------------------------- #
# CSV import (review-and-confirm) -- see csv_import.py
# --------------------------------------------------------------------------- #


@app.route("/api/import/preview", methods=["POST"])
@require_auth
def import_preview():
    body = request.get_json(force=True, silent=True) or {}
    source = body.get("source")
    file_name = body.get("file_name", "upload.csv")
    csv_text = body.get("csv_text")
    if source not in ("wealthsimple", "yahoo") or not csv_text:
        return _err("source ('wealthsimple' or 'yahoo') and csv_text are required")

    try:
        rows = csv_import.parse_csv(csv_text)
    except Exception as e:
        return _err(f"could not parse CSV: {e}")
    if not rows:
        return _err("no data rows found in that file")

    import_id = state.save_portfolio_import(source, file_name, rows)
    open_positions = [p for p in state.get_all_positions() if p.status == "open"]
    proposals = csv_import.build_proposals(rows, open_positions)
    return jsonify({"import_id": import_id, "row_count": len(rows), "proposals": proposals})


@app.route("/api/import/apply", methods=["POST"])
@require_auth
def import_apply():
    body = request.get_json(force=True, silent=True) or {}
    import_id = body.get("import_id")
    decisions = body.get("decisions") or []
    record = state.get_portfolio_import(import_id) if import_id else None
    if record is None:
        return _err("import not found -- run /api/import/preview first")

    applied = csv_import.apply_decisions(decisions, add_position_fn=_apply_add_position)
    state.mark_portfolio_import_applied(import_id, applied)
    state.log_decision(datetime.now(), None, "import_applied", {"import_id": import_id, "count": len(applied)})
    return jsonify({"applied": applied})


def _apply_add_position(proposal: dict) -> int:
    entry_price = float(proposal["entry_price"])
    entry_date = date.fromisoformat(proposal["entry_date"])
    # build_proposals already computes shares directly from the CSV row's
    # own price*quantity -- use that authoritative value rather than
    # re-deriving it from dollar_amount, same reasoning as the manual
    # add-position routes above.
    shares = float(proposal["shares"])
    ticker = proposal["ticker"].strip().upper()
    peak_price = _backfill_peak_price(ticker, entry_date, entry_price)
    position = state.LivePosition(
        ticker=ticker, entry_date=entry_date, entry_price=entry_price, dollar_amount=shares * entry_price,
        shares=shares,
        catalyst_date=entry_date, sector=None, peak_price=peak_price, lot_type="base",
    )
    position_id = state.add_position(position)
    state.log_decision(
        datetime.now(), ticker, "position_added",
        {"position_id": position_id, "entry_date": entry_date.isoformat(), "entry_price": entry_price, "via": "csv_import"},
    )
    return position_id


# --------------------------------------------------------------------------- #
# Backup export/import -- Mike's chosen durability mechanism (free Render
# tier wipes the disk on redeploy; export before, import after).
# --------------------------------------------------------------------------- #


@app.route("/api/backup/export", methods=["GET"])
@require_auth
def backup_export():
    return jsonify(state.export_all())


@app.route("/api/backup/import", methods=["POST"])
@require_auth
def backup_import():
    bundle = request.get_json(force=True, silent=True)
    if not bundle or "positions" not in bundle:
        return _err("that doesn't look like a Signal Ledger backup file")
    state.import_all(bundle)
    return jsonify({"ok": True, "restored_from": bundle.get("exported_at")})


# --------------------------------------------------------------------------- #
# Daily job trigger + health check
# --------------------------------------------------------------------------- #


@app.route("/api/refresh", methods=["POST"])
@require_auth
def refresh():
    result = daily_job.run_once(send_email=False)
    return jsonify(result)


@app.route("/run-daily-job", methods=["GET", "POST"])
def run_daily_job_route():
    """Meant to be called once a day by an external free cron pinger
    (e.g. cron-job.org). Protected by a shared-secret token, NOT the
    dashboard's basic auth, since an external service is calling it."""
    if not CRON_SECRET or request.args.get("token") != CRON_SECRET:
        return jsonify({"error": "unauthorized"}), 401
    result = daily_job.run_once(send_email=True)
    return jsonify(result)


@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok", "time": datetime.now().isoformat()})


# --------------------------------------------------------------------------- #
# Static SPA hosting -- everything that isn't /api/* or /healthz falls
# through to the built React app (see live_app/static/, built from the
# earnings-strategy frontend). Client-side routing means any path should
# resolve to index.html.
# --------------------------------------------------------------------------- #


@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
@require_auth
def spa(path):
    if path and (STATIC_DIR / path).is_file():
        return send_from_directory(STATIC_DIR, path)
    index = STATIC_DIR / "index.html"
    if not index.is_file():
        return (
            "Dashboard build not found. Run the frontend build and copy its output into "
            "live_app/static/ (see DEPLOYMENT.md).",
            501,
        )
    return send_from_directory(STATIC_DIR, "index.html")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
