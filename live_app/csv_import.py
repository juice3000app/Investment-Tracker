"""
live_app/csv_import.py

Parses a Wealthsimple/Yahoo Finance portfolio CSV export and proposes
what it would mean for the tracked positions -- review-and-confirm, same
as everything else in this app: nothing here writes to state directly.
server.py's /api/import/preview calls parse_csv + build_proposals and
saves the raw rows; /api/import/apply applies only the specific
proposals Mike actually confirmed, via apply_decisions.

Ported from the original Signal Ledger mockup's client-side CSV parser
and column-name heuristics (app/page.tsx's parseCsv, app/api/import/
portfolio/route.ts's `first`/`number` helpers), which only ever stored
raw rows and never actually affected tracked positions -- this is the
part that makes it real.
"""

from __future__ import annotations

import csv
import io
from datetime import date
from typing import Callable, Optional

# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #


def parse_csv(text: str) -> list[dict]:
    """Standard CSV parsing (quoted fields, embedded commas) via the
    stdlib csv module, with a BOM strip on the header row -- Wealthsimple
    and Yahoo exports are both plain comma-separated with a header row."""
    text = text.lstrip("﻿")
    reader = csv.DictReader(io.StringIO(text))
    rows = [dict(row) for row in reader if any((v or "").strip() for v in row.values())]
    return rows


def _first(row: dict, names: list[str]) -> Optional[str]:
    lower = {k.lower(): v for k, v in row.items() if k}
    for name in names:
        value = lower.get(name.lower())
        if value and value.strip():
            return value.strip()
    return None


def _number(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    negative = "(" in value and ")" in value
    cleaned = value.replace("$", "").replace(",", "").replace("(", "").replace(")", "")
    try:
        parsed = float(cleaned)
    except ValueError:
        return None
    return -parsed if negative else parsed


def _date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%b %d, %Y", "%B %d, %Y"):
        try:
            from datetime import datetime as _dt
            return _dt.strptime(value.strip(), fmt).date()
        except ValueError:
            continue
    return None


SYMBOL_COLS = ["Symbol", "Ticker", "Security symbol"]
QUANTITY_COLS = ["Quantity", "Shares", "Units", "Current shares"]
PRICE_COLS = ["Average cost", "Book value per share", "Price", "Market price", "Execution price", "Current price"]
AMOUNT_COLS = ["Amount", "Market value", "Net amount", "Book value", "Current value"]
DATE_COLS = ["Date", "Transaction date", "Activity date", "Trade date", "Settlement date"]
TYPE_COLS = ["Transaction type", "Activity type", "Type", "Action"]


def detect_import_type(rows: list[dict]) -> str:
    if not rows:
        return "holdings"
    header_text = " ".join(rows[0].keys()).lower()
    if any(k in header_text for k in ("activity", "transaction", "trade date", "settlement")):
        return "activities"
    return "holdings"


def _is_sell(row: dict, quantity: Optional[float]) -> bool:
    type_value = (_first(row, TYPE_COLS) or "").lower()
    if "sell" in type_value or "sold" in type_value:
        return True
    if "buy" in type_value or "bought" in type_value:
        return False
    return quantity is not None and quantity < 0


# --------------------------------------------------------------------------- #
# Proposals -- review-and-confirm, one row per proposed change.
# --------------------------------------------------------------------------- #


def build_proposals(rows: list[dict], open_positions: list) -> list[dict]:
    """open_positions: list of state.LivePosition (open, any lot type)."""
    open_by_ticker = {p.ticker: p for p in open_positions if p.lot_type == "base"}
    import_type = detect_import_type(rows)
    proposals = []

    if import_type == "holdings":
        totals: dict[str, dict] = {}
        for row in rows:
            symbol = _first(row, SYMBOL_COLS)
            if not symbol:
                continue
            symbol = symbol.strip().upper()
            quantity = _number(_first(row, QUANTITY_COLS)) or 0.0
            price = _number(_first(row, PRICE_COLS))
            amount = _number(_first(row, AMOUNT_COLS))
            bucket = totals.setdefault(symbol, {"quantity": 0.0, "price": price, "amount": 0.0})
            bucket["quantity"] += quantity
            if price is not None:
                bucket["price"] = price
            if amount is not None:
                bucket["amount"] += amount

        for symbol, bucket in totals.items():
            quantity = bucket["quantity"]
            if quantity <= 0:
                continue
            price = bucket["price"] or (bucket["amount"] / quantity if quantity else None)
            if symbol in open_by_ticker:
                existing = open_by_ticker[symbol]
                proposals.append({
                    "kind": "skip", "ticker": symbol, "default_include": False,
                    "reason": f"Already tracked as an open position (#{existing.id}) -- quantity/price "
                              "differences from this import aren't applied automatically; edit that "
                              "position by hand if this snapshot shows something different.",
                })
                continue
            if price is None:
                proposals.append({
                    "kind": "skip", "ticker": symbol, "default_include": False,
                    "reason": "No usable price column found for this holding -- can't record it without one.",
                })
                continue
            proposals.append({
                "kind": "new_position", "ticker": symbol, "default_include": True,
                "entry_date": date.today().isoformat(), "entry_price": price,
                "dollar_amount": round(price * quantity, 2), "shares": quantity,
                "reason": "This is a snapshot of current holdings, not a purchase record, so the entry date "
                          "defaults to today -- edit it if you know the real purchase date.",
            })

    else:  # activities
        for i, row in enumerate(rows):
            symbol = _first(row, SYMBOL_COLS)
            if not symbol:
                continue
            symbol = symbol.strip().upper()
            quantity = _number(_first(row, QUANTITY_COLS))
            price = _number(_first(row, PRICE_COLS))
            amount = _number(_first(row, AMOUNT_COLS))
            occurred = _date(_first(row, DATE_COLS))

            if _is_sell(row, quantity):
                proposals.append({
                    "kind": "skip", "ticker": symbol, "default_include": False, "row_index": i,
                    "reason": "This looks like a sell/withdrawal activity -- close the matching position "
                              "by hand if it represents a real exit; imports don't auto-close positions.",
                })
                continue

            qty = abs(quantity) if quantity else None
            if qty is None and amount and price:
                qty = abs(amount) / price
            if qty is None or not price:
                proposals.append({
                    "kind": "skip", "ticker": symbol, "default_include": False, "row_index": i,
                    "reason": "Couldn't find both a quantity and a price for this row.",
                })
                continue

            proposals.append({
                "kind": "new_position", "ticker": symbol, "default_include": True, "row_index": i,
                "entry_date": (occurred or date.today()).isoformat(), "entry_price": price,
                "dollar_amount": round(price * qty, 2), "shares": qty,
                "reason": "Recorded as a new base position from this activity row." if occurred else
                          "No usable date column found -- entry date defaulted to today; edit if you know the real date.",
            })

    return proposals


def apply_decisions(decisions: list[dict], add_position_fn: Callable[[dict], int]) -> list[dict]:
    """decisions: the subset of proposals the person confirmed (each a
    'new_position'-kind proposal, possibly hand-edited in the review
    screen first). Returns what was actually applied, for the audit
    trail and the response shown to the person."""
    applied = []
    for decision in decisions:
        if decision.get("kind") != "new_position":
            continue
        position_id = add_position_fn(decision)
        applied.append({"ticker": decision["ticker"], "position_id": position_id})
    return applied
