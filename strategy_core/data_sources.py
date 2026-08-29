"""
strategy_core/data_sources.py

Price / earnings-date / shares-outstanding fetching, and the four universe
definitions from the strategy spec (auto-sweep, index constituents, manual
list, uploaded holdings file).

This is the ONLY file in strategy_core that talks to the network. Everything
in strategy.py and portfolio.py is pure and gets handed data -- it never
fetches anything itself.

Data source: Yahoo Finance via `yfinance` (free, no API key). Two
limitations are inherited from that choice and are worth knowing about
up front, in the same spirit as the market-cap approximation the strategy
spec already accepts:

  1. Auto-sweep universe: Yahoo Finance has no free "every TSX-listed
     equity in a market-cap band" endpoint. `fetch_auto_sweep_universe()`
     screens a bundled/user-maintained list of TSX tickers (see
     `data/tsx_tickers.csv`) rather than truly sweeping the whole exchange.
     Swap in a paid listings API later if broader coverage is needed --
     the function signature won't need to change.
  2. Earnings dates for small/micro-cap TSX names are sometimes missing or
     late on Yahoo Finance. `fetch_earnings_dates()` returns what Yahoo
     has; a ticker with no data there will simply screen out (produces
     silently-thin coverage, not a crash) -- worth spot-checking against a
     couple of known catalyst dates before trusting a first real run.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import pandas as pd

DATA_DIR = Path(__file__).parent / "data"

_YFINANCE_IMPORT_ERROR = None
try:
    import yfinance as yf
except ImportError as e:  # pragma: no cover
    yf = None
    _YFINANCE_IMPORT_ERROR = e


def _require_yfinance():
    if yf is None:
        raise ImportError(
            "yfinance is required for live data fetching. "
            "Install it with: pip install yfinance"
        ) from _YFINANCE_IMPORT_ERROR


@dataclass
class UniverseTicker:
    ticker: str
    sector: Optional[str] = None


# --------------------------------------------------------------------------- #
# Price history
# --------------------------------------------------------------------------- #


def fetch_price_history(ticker: str, start: date, end: date) -> pd.DataFrame:
    """Daily OHLCV, Date-indexed ascending. Columns: Open, High, Low,
    Close, Volume. Empty DataFrame (not an exception) if nothing comes
    back, so callers can treat "no data" as a normal screening failure."""
    _require_yfinance()
    df = yf.Ticker(ticker).history(start=start, end=end, interval="1d", auto_adjust=False)
    if df.empty:
        return df
    df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
    df.index = pd.to_datetime(df.index).date
    df.index.name = "Date"
    return df.sort_index()


def fetch_current_price(ticker: str) -> Optional[float]:
    _require_yfinance()
    fast = yf.Ticker(ticker).fast_info
    price = getattr(fast, "last_price", None)
    return float(price) if price else None


# --------------------------------------------------------------------------- #
# Shares outstanding (today's figure, used for the historical approximation)
# --------------------------------------------------------------------------- #


def fetch_shares_outstanding(ticker: str) -> Optional[float]:
    _require_yfinance()
    t = yf.Ticker(ticker)
    try:
        shares = t.fast_info.get("shares", None)
    except Exception:
        shares = None
    if not shares:
        info = t.info or {}
        shares = info.get("sharesOutstanding")
    return float(shares) if shares else None


# --------------------------------------------------------------------------- #
# Earnings dates (the catalyst)
# --------------------------------------------------------------------------- #


def fetch_earnings_dates(
    ticker: str, lookback_start: Optional[date] = None, lookahead_end: Optional[date] = None
) -> list[date]:
    """Historical + upcoming public earnings dates for a ticker, from
    Yahoo Finance. Returns whatever falls inside [lookback_start,
    lookahead_end] if given, else everything Yahoo returns (Yahoo caps
    history depth on its own)."""
    _require_yfinance()
    t = yf.Ticker(ticker)
    # Deliberately NOT catching exceptions here: a ticker Yahoo genuinely has
    # no earnings calendar for comes back as an empty/None dataframe (handled
    # below) and is a normal, expected gap in coverage -- but a failed
    # network call (blocked, rate-limited, timed out) is a different problem
    # and needs to look different to the caller, not silently collapse into
    # "no earnings" (see live_app/daily_job.py's scan diagnostics, which
    # depend on this distinction to tell a real zero from a broken fetch).
    df = t.get_earnings_dates(limit=60)
    if df is None or df.empty:
        return []
    dates = [d.date() if hasattr(d, "date") else d for d in df.index]
    if lookback_start is not None:
        dates = [d for d in dates if d >= lookback_start]
    if lookahead_end is not None:
        dates = [d for d in dates if d <= lookahead_end]
    return sorted(dates)


def fetch_sector(ticker: str) -> Optional[str]:
    _require_yfinance()
    try:
        info = yf.Ticker(ticker).info or {}
    except Exception:
        return None
    return info.get("sector")


# --------------------------------------------------------------------------- #
# Universe definitions (spec section 2)
# --------------------------------------------------------------------------- #


def get_universe_manual_list(tickers: list[str]) -> list[UniverseTicker]:
    return [UniverseTicker(ticker=t.strip().upper()) for t in tickers if t.strip()]


def get_universe_index_constituents() -> list[UniverseTicker]:
    """Current S&P/TSX Composite Index membership. Scraped from Wikipedia's
    maintained constituent table; falls back to a bundled snapshot
    (data/tsx_composite_snapshot.csv) if the fetch fails (offline, table
    layout changed, etc.) -- refresh that snapshot periodically by hand if
    you rely on this path a lot."""
    try:
        tables = pd.read_html("https://en.wikipedia.org/wiki/S%26P/TSX_Composite_Index")
        for tbl in tables:
            cols_lower = [str(c).lower() for c in tbl.columns]
            if any("ticker" in c or "symbol" in c for c in cols_lower):
                ticker_col = tbl.columns[
                    next(i for i, c in enumerate(cols_lower) if "ticker" in c or "symbol" in c)
                ]
                sector_col = None
                for i, c in enumerate(cols_lower):
                    if "sector" in c:
                        sector_col = tbl.columns[i]
                        break
                out = []
                for _, row in tbl.iterrows():
                    raw = str(row[ticker_col]).strip()
                    if not raw or raw.lower() == "nan":
                        continue
                    ticker = raw if raw.endswith(".TO") else f"{raw}.TO"
                    sector = str(row[sector_col]) if sector_col is not None else None
                    out.append(UniverseTicker(ticker=ticker, sector=sector))
                if out:
                    return out
    except Exception:
        pass

    snapshot = DATA_DIR / "tsx_composite_snapshot.csv"
    if snapshot.exists():
        df = pd.read_csv(snapshot)
        return [
            UniverseTicker(ticker=row["ticker"], sector=row.get("sector"))
            for _, row in df.iterrows()
        ]
    return []


def get_universe_auto_sweep(
    market_cap_min: float,
    market_cap_max: float,
    ticker_list_path: Optional[Path] = None,
) -> list[UniverseTicker]:
    """Broadest coverage: screens a bundled/maintained list of TSX tickers
    by *current* market cap (a cheap pre-filter -- the real, point-in-time
    -safe screen still runs per-catalyst-date in strategy.py). See the
    module docstring for why this isn't a true full-exchange sweep.

    When nothing survives the screen, raises a specific error explaining
    WHY rather than just returning an empty list -- "the file is empty"
    (the ships-empty default), "every ticker in it failed to fetch"
    (almost always a network/firewall block, or tickers not in Yahoo's
    .TO format), and "every ticker fetched fine but none fell in the
    market-cap band" are three very different problems with three very
    different fixes, and a populated file that still comes back empty
    used to look identical to an empty file."""
    _require_yfinance()
    path = ticker_list_path or (DATA_DIR / "tsx_tickers.csv")
    if not path.exists():
        raise FileNotFoundError(
            f"No TSX ticker list found at {path}. Auto-sweep needs a maintained "
            "list of TSX-listed tickers to screen -- see strategy_core/data/README.md."
        )

    df = pd.read_csv(path)
    cols_lower = {c.lower().strip(): c for c in df.columns}
    ticker_col = next((cols_lower[c] for c in ("ticker", "symbol") if c in cols_lower), None)
    if ticker_col is None:
        raise ValueError(
            f"{path} doesn't have a 'ticker' column (found: {', '.join(df.columns) or 'no columns at all'}). "
            "It needs one column named 'ticker', with each row in Yahoo Finance's TSX format "
            "(e.g. SHOP.TO, not just SHOP) -- see strategy_core/data/README.md."
        )
    all_tickers = [str(t).strip() for t in df[ticker_col].dropna().tolist() if str(t).strip()]
    if not all_tickers:
        raise ValueError(
            f"{path} has a 'ticker' column but no ticker rows underneath it -- add one ticker per row "
            "(Yahoo Finance format, e.g. SHOP.TO)."
        )

    fetch_failures = 0
    out_of_band = 0
    out = []
    for ticker in all_tickers:
        try:
            fast = yf.Ticker(ticker).fast_info
            price = fast.get("last_price")
            shares = fast.get("shares")
            if not price or not shares:
                fetch_failures += 1
                continue
            mcap = price * shares
            if market_cap_min <= mcap <= market_cap_max:
                out.append(UniverseTicker(ticker=ticker))
            else:
                out_of_band += 1
        except Exception:
            fetch_failures += 1
            continue

    if not out:
        if fetch_failures == len(all_tickers):
            raise RuntimeError(
                f"None of the {len(all_tickers)} tickers in {path.name} could be fetched from Yahoo "
                "Finance -- price/shares data never came back for any of them. This is almost always "
                "either a network/firewall block (common on a locked-down work computer -- the same "
                "class of issue that can block the S&P/TSX Composite lookup) or tickers that aren't in "
                "Yahoo's TSX format (must end in .TO, e.g. SHOP.TO -- not just SHOP). Try again from a "
                "different network, or double-check the ticker format in the file."
            )
        raise RuntimeError(
            f"{len(all_tickers)} ticker(s) in {path.name} were checked ({fetch_failures} couldn't be "
            f"fetched at all, {out_of_band} fetched fine but fell outside the market-cap band), but none "
            f"landed within ${market_cap_min:,.0f}-${market_cap_max:,.0f}. Widen the market-cap floor/"
            "ceiling on the Numeric Screen tab, or add tickers in that range to the file."
        )
    return out


def get_universe_from_holdings_file(file_path: str | Path) -> list[UniverseTicker]:
    """Parse an ETF's published holdings export (csv or xlsx), returning
    ticker + sector for each holding. Handles the common column-name
    variants different providers use."""
    path = Path(file_path)
    if path.suffix.lower() in (".xlsx", ".xls"):
        df = pd.read_excel(path)
    else:
        df = pd.read_csv(path)

    cols_lower = {c.lower().strip(): c for c in df.columns}
    ticker_col = next(
        (cols_lower[c] for c in ("ticker", "symbol", "holding ticker", "stock ticker") if c in cols_lower),
        None,
    )
    sector_col = next(
        (cols_lower[c] for c in ("sector", "gics sector", "gics sector name") if c in cols_lower),
        None,
    )
    if ticker_col is None:
        raise ValueError(
            f"Couldn't find a ticker/symbol column in {path.name}. "
            f"Columns found: {list(df.columns)}"
        )

    out = []
    for _, row in df.iterrows():
        raw = str(row[ticker_col]).strip()
        if not raw or raw.lower() == "nan":
            continue
        ticker = raw if raw.endswith(".TO") else f"{raw}.TO"
        sector = str(row[sector_col]).strip() if sector_col is not None else None
        out.append(UniverseTicker(ticker=ticker, sector=sector))
    return out
