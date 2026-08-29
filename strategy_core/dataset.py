"""
strategy_core/dataset.py

Save / list / load named datasets -- the fix for problem 5a. A dataset
bundles everything a backtest run needs *except* the execution parameters
(position size, stop-loss, stagnation, fees, idle-cash-sweep): the
screening parameters used, the full daily price history for every ticker
screened (whether or not it became a candidate), the resulting candidate
list, and a timestamp/label.

Re-running a backtest against an existing dataset with different execution
parameters is then a pure in-memory replay -- zero network calls, since
none of those parameters change *which* stocks got screened.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import pandas as pd

from . import data_sources as ds
from . import strategy as strat

DEFAULT_DATASETS_DIR = Path.home() / ".model3" / "datasets"


@dataclass
class CandidateRecord:
    ticker: str
    catalyst_date: date
    sector: Optional[str] = None


@dataclass
class Dataset:
    name: str  # slug used as the directory name
    label: str  # human-readable label shown in the picker
    created_at: datetime
    universe_mode: str  # 'auto_sweep' | 'index' | 'manual' | 'holdings_file'
    screening_params: dict  # snapshot of the screening-relevant StrategyParams fields
    start_date: date
    end_date: date
    candidates: list[CandidateRecord]
    price_history: dict[str, pd.DataFrame]  # ticker -> OHLCV, ALL screened tickers
    shares_outstanding: dict[str, float]
    sectors: dict[str, Optional[str]] = field(default_factory=dict)
    benchmark_ticker: Optional[str] = None
    benchmark_price_history: Optional[pd.DataFrame] = None

    def ticker_count(self) -> int:
        return len(self.price_history)

    def candidate_count(self) -> int:
        return len(self.candidates)


# --------------------------------------------------------------------------- #
# Save
# --------------------------------------------------------------------------- #


def save_dataset(dataset: Dataset, base_dir: Path = DEFAULT_DATASETS_DIR) -> Path:
    out_dir = base_dir / dataset.name
    if out_dir.exists():
        shutil.rmtree(out_dir)
    prices_dir = out_dir / "prices"
    prices_dir.mkdir(parents=True, exist_ok=True)

    for ticker, df in dataset.price_history.items():
        safe_name = ticker.replace("/", "_")
        df.to_csv(prices_dir / f"{safe_name}.csv")

    if dataset.benchmark_price_history is not None and dataset.benchmark_ticker:
        dataset.benchmark_price_history.to_csv(out_dir / "benchmark.csv")

    manifest = {
        "name": dataset.name,
        "label": dataset.label,
        "created_at": dataset.created_at.isoformat(),
        "universe_mode": dataset.universe_mode,
        "screening_params": dataset.screening_params,
        "start_date": dataset.start_date.isoformat(),
        "end_date": dataset.end_date.isoformat(),
        "candidates": [
            {
                "ticker": c.ticker,
                "catalyst_date": c.catalyst_date.isoformat(),
                "sector": c.sector,
            }
            for c in dataset.candidates
        ],
        "shares_outstanding": dataset.shares_outstanding,
        "sectors": dataset.sectors,
        "benchmark_ticker": dataset.benchmark_ticker,
        "ticker_count": dataset.ticker_count(),
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return out_dir


# --------------------------------------------------------------------------- #
# List
# --------------------------------------------------------------------------- #


@dataclass
class DatasetSummary:
    name: str
    label: str
    created_at: datetime
    universe_mode: str
    ticker_count: int
    candidate_count: int
    start_date: date
    end_date: date


def list_datasets(base_dir: Path = DEFAULT_DATASETS_DIR) -> list[DatasetSummary]:
    if not base_dir.exists():
        return []
    summaries = []
    for entry in sorted(base_dir.iterdir()):
        manifest_path = entry / "manifest.json"
        if not manifest_path.exists():
            continue
        m = json.loads(manifest_path.read_text())
        summaries.append(
            DatasetSummary(
                name=m["name"],
                label=m["label"],
                created_at=datetime.fromisoformat(m["created_at"]),
                universe_mode=m["universe_mode"],
                ticker_count=m.get("ticker_count", 0),
                candidate_count=len(m.get("candidates", [])),
                start_date=date.fromisoformat(m["start_date"]),
                end_date=date.fromisoformat(m["end_date"]),
            )
        )
    summaries.sort(key=lambda s: s.created_at, reverse=True)
    return summaries


# --------------------------------------------------------------------------- #
# Load
# --------------------------------------------------------------------------- #


def load_dataset(name: str, base_dir: Path = DEFAULT_DATASETS_DIR) -> Dataset:
    out_dir = base_dir / name
    manifest_path = out_dir / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"No dataset named '{name}' in {base_dir}")
    m = json.loads(manifest_path.read_text())

    price_history = {}
    prices_dir = out_dir / "prices"
    for csv_file in prices_dir.glob("*.csv"):
        ticker = csv_file.stem
        df = pd.read_csv(csv_file, index_col=0, parse_dates=True)
        df.index = df.index.date
        df.index.name = "Date"
        price_history[ticker] = df

    benchmark_df = None
    benchmark_path = out_dir / "benchmark.csv"
    if benchmark_path.exists():
        benchmark_df = pd.read_csv(benchmark_path, index_col=0, parse_dates=True)
        benchmark_df.index = benchmark_df.index.date
        benchmark_df.index.name = "Date"

    candidates = [
        CandidateRecord(
            ticker=c["ticker"],
            catalyst_date=date.fromisoformat(c["catalyst_date"]),
            sector=c.get("sector"),
        )
        for c in m["candidates"]
    ]

    return Dataset(
        name=m["name"],
        label=m["label"],
        created_at=datetime.fromisoformat(m["created_at"]),
        universe_mode=m["universe_mode"],
        screening_params=m["screening_params"],
        start_date=date.fromisoformat(m["start_date"]),
        end_date=date.fromisoformat(m["end_date"]),
        candidates=candidates,
        price_history=price_history,
        shares_outstanding=m["shares_outstanding"],
        sectors=m.get("sectors", {}),
        benchmark_ticker=m.get("benchmark_ticker"),
        benchmark_price_history=benchmark_df,
    )


def delete_dataset(name: str, base_dir: Path = DEFAULT_DATASETS_DIR) -> None:
    out_dir = base_dir / name
    if out_dir.exists():
        shutil.rmtree(out_dir)


def make_dataset_name(label: str, when: Optional[datetime] = None) -> str:
    when = when or datetime.now()
    slug = "".join(c if c.isalnum() else "-" for c in label.lower()).strip("-")
    return f"{when.strftime('%Y%m%d-%H%M%S')}-{slug or 'dataset'}"


# --------------------------------------------------------------------------- #
# "Fetch new dataset" -- the slow, network-hitting action from problem 5a.
# Only ever needed when universe, lookback window, or screening thresholds
# change. Everything downstream ("Run backtest") replays the result of this
# with zero network calls.
# --------------------------------------------------------------------------- #


def _empty_universe_message(universe_mode: str) -> str:
    """A zero-ticker universe used to be saved silently as a valid (empty)
    dataset -- confusing, since nothing on screen said anything had gone
    wrong. Raising this instead surfaces a real, mode-specific error in
    the GUI's 'Fetch failed' dialog."""
    if universe_mode == "index":
        return (
            "Got zero S&P/TSX Composite constituents. The live Wikipedia lookup "
            "didn't return any tickers (often a network/firewall block -- common on "
            "a locked-down work computer) and the offline backup list came back "
            "empty too. If you're on a restricted network, try again from a "
            "different connection, or switch Universe mode to 'Manual ticker list' "
            "or 'Uploaded ETF holdings file' instead."
        )
    if universe_mode == "auto_sweep":
        return (
            "Auto-sweep found zero tickers. This mode screens the list in "
            "strategy_core/data/tsx_tickers.csv -- if it's still the empty default, see "
            "strategy_core/data/README.md for how to populate it. If you've already populated it, "
            "this generic message means something unexpected happened resolving it -- normally you'd "
            "see a more specific error instead (network block, wrong ticker format, or nothing in the "
            "market-cap band), so this is worth reporting. In the meantime, try switching Universe "
            "mode to 'S&P/TSX Composite constituents'."
        )
    if universe_mode == "manual":
        return "No manual tickers were entered. Type one or more comma-separated tickers in the 'Manual tickers' field."
    if universe_mode == "holdings_file":
        return "The holdings file produced zero tickers -- check that it has a ticker/symbol column with data in it."
    return f"Got zero tickers for universe mode '{universe_mode}'."


def fetch_and_build_dataset(
    label: str,
    universe_mode: str,  # 'auto_sweep' | 'index' | 'manual' | 'holdings_file'
    params: strat.StrategyParams,
    start_date: date,
    end_date: date,
    manual_tickers: Optional[list[str]] = None,
    holdings_file_path: Optional[str] = None,
    benchmark_ticker: str = "VFV.TO",
    progress_callback: Optional[callable] = None,
) -> Dataset:
    """Hits the network once to build a complete, self-contained dataset:
    resolves the universe, fetches full daily price history + shares
    outstanding + catalyst (earnings) dates for every ticker in it, and
    records which tickers passed the numeric screen at each of their
    catalyst dates. Nothing here decides entries/exits -- that's still
    portfolio.run_backtest's job, operating on the saved result.
    """

    def _progress(msg: str):
        if progress_callback:
            progress_callback(msg)

    _progress(f"Resolving universe ({universe_mode})...")
    if universe_mode == "auto_sweep":
        universe = ds.get_universe_auto_sweep(params.market_cap_floor, params.market_cap_ceiling)
    elif universe_mode == "index":
        universe = ds.get_universe_index_constituents()
    elif universe_mode == "manual":
        universe = ds.get_universe_manual_list(manual_tickers or [])
    elif universe_mode == "holdings_file":
        if not holdings_file_path:
            raise ValueError("holdings_file_path is required when universe_mode='holdings_file'")
        universe = ds.get_universe_from_holdings_file(holdings_file_path)
    else:
        raise ValueError(f"Unknown universe_mode: {universe_mode}")

    if not universe:
        raise RuntimeError(_empty_universe_message(universe_mode))

    price_history: dict[str, pd.DataFrame] = {}
    shares_outstanding: dict[str, float] = {}
    sectors: dict[str, Optional[str]] = {}
    candidates: list[CandidateRecord] = []

    # Fetch a little before start_date so the first catalysts in-window
    # still have enough trailing history for the 60-day vol / 20-day
    # dollar-volume / stagnation lookbacks to be computable.
    fetch_start = start_date - pd.Timedelta(days=120)

    total = len(universe)
    for i, u in enumerate(universe, start=1):
        _progress(f"Fetching {u.ticker} ({i}/{total})...")
        try:
            prices = ds.fetch_price_history(u.ticker, fetch_start.date() if hasattr(fetch_start, "date") else fetch_start, end_date)
        except Exception:
            continue
        if prices.empty:
            continue

        shares = ds.fetch_shares_outstanding(u.ticker)
        sector = u.sector if u.sector else ds.fetch_sector(u.ticker)
        catalyst_dates = ds.fetch_earnings_dates(u.ticker, lookback_start=start_date, lookahead_end=end_date)

        price_history[u.ticker] = prices
        shares_outstanding[u.ticker] = shares or 0.0
        sectors[u.ticker] = sector

        for cdate in catalyst_dates:
            candidates.append(CandidateRecord(ticker=u.ticker, catalyst_date=cdate, sector=sector))

    _progress("Fetching benchmark...")
    try:
        benchmark_prices = ds.fetch_price_history(benchmark_ticker, start_date, end_date)
    except Exception:
        benchmark_prices = None

    name = make_dataset_name(label)
    screening_params = {
        "min_volatility": params.min_volatility,
        "market_cap_floor": params.market_cap_floor,
        "market_cap_ceiling": params.market_cap_ceiling,
        "min_avg_dollar_volume": params.min_avg_dollar_volume,
        "catalyst_window_days": params.catalyst_window_days,
    }

    return Dataset(
        name=name,
        label=label,
        created_at=datetime.now(),
        universe_mode=universe_mode,
        screening_params=screening_params,
        start_date=start_date,
        end_date=end_date,
        candidates=candidates,
        price_history=price_history,
        shares_outstanding=shares_outstanding,
        sectors=sectors,
        benchmark_ticker=benchmark_ticker,
        benchmark_price_history=benchmark_prices,
    )


def dataset_to_ticker_data(dataset: Dataset) -> list:
    """Converts a loaded Dataset into the list[portfolio.TickerData] that
    run_backtest expects. Kept here (not in portfolio.py) so portfolio.py
    never has to import dataset.py -- the engine only knows about plain
    price/catalyst inputs, not how they were persisted."""
    from . import portfolio as pf

    by_ticker_candidates: dict[str, list[date]] = {}
    for c in dataset.candidates:
        by_ticker_candidates.setdefault(c.ticker, []).append(c.catalyst_date)

    out = []
    for ticker, prices in dataset.price_history.items():
        out.append(
            pf.TickerData(
                ticker=ticker,
                prices=pf.PointInTimePrices(prices),
                catalyst_dates=by_ticker_candidates.get(ticker, []),
                shares_outstanding=dataset.shares_outstanding.get(ticker, 0.0),
                sector=dataset.sectors.get(ticker),
            )
        )
    return out


def dataset_benchmark_ticker_data(dataset: Dataset):
    from . import portfolio as pf

    if dataset.benchmark_price_history is None:
        return None
    return pf.TickerData(
        ticker=dataset.benchmark_ticker or "BENCHMARK",
        prices=pf.PointInTimePrices(dataset.benchmark_price_history),
        catalyst_dates=[],
        shares_outstanding=0.0,
        sector=None,
    )
