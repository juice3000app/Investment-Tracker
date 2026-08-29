# strategy_core/data

Static reference data used by `data_sources.py` when a free, comprehensive
API doesn't exist for something the strategy needs.

## `tsx_tickers.csv`

Used by `get_universe_auto_sweep()`. One column, `ticker`, containing every
TSX-listed ticker (Yahoo Finance format, e.g. `AC.TO`, `SHOP.TO`) you want
the auto-sweep universe to consider before it's narrowed down by market cap.

This file ships **empty except for the header** — there is no free, reliable
API that lists every TSX-listed equity. Populate it yourself, e.g. by
exporting a full TSX symbol list from the TMX Money website or a broker's
symbol lookup, save as CSV with one `ticker` (or `symbol`) column, one
ticker per row, each in Yahoo Finance's TSX format (`SHOP.TO`, not just
`SHOP` — a bare ticker without `.TO` won't fetch). Refresh it
occasionally (new listings, delistings) — this is a known, accepted
limitation of the free-data-source approach, the same category as the
market-cap approximation described in the main strategy spec.

If auto-sweep still comes back with zero tickers after you've populated
this file, the error message now says exactly why: every ticker failed to
fetch from Yahoo Finance (almost always a network/firewall block, or a
ticker missing its `.TO` suffix), or every ticker fetched fine but none
fell inside the market-cap floor/ceiling you've set on the Numeric Screen
tab. Widen the market-cap band or fetch from a different network,
whichever the message points to — it no longer looks identical to the
file simply being empty.

## `tsx_composite_snapshot.csv`

Fallback for `get_universe_index_constituents()` if the live Wikipedia
scrape fails (offline, page layout changed, a corporate firewall blocking
Wikipedia, etc.). Two columns: `ticker`, `sector`. Ships with a real
snapshot (~220 tickers) taken August 2026 — it'll drift out of date as
index membership changes, so treat it as a "still works when the live
fetch can't" backup rather than a source of truth. Refresh it periodically
by hand (or ask Claude to) if you rely on this fallback path often.

If BOTH the live scrape and this snapshot come back empty, `index` mode
now raises a clear error instead of silently saving a zero-ticker dataset
— you'll see it in the "Fetch failed" dialog rather than discovering it
later from a dataset that mysteriously has 0 tickers and 0 candidates.
