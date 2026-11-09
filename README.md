# Crypto Trading Decision & Research Engine

A Python engine for scanning crypto markets, producing rule-qualified trade setups, sizing risk, and backtesting strategies across CCXT-supported exchanges.

## Current capabilities

- Universal public market-data access through CCXT.
- OHLCV normalization and quality validation.
- EMA 50/200, RSI 14, ATR 14, and volume baseline.
- Trend/range regime detection plus basic market structure.
- Trend-continuation setup scoring: CONFIRMED, FORMING, or NO_TRADE.
- Risk-defined trade plans with entry zone, stop, TP1, TP2, size, notional, and actual account risk.
- Multi-symbol scanning and ranking.
- Shared DecisionEngine used by both scanner and historical research.
- Scanner-aligned backtester with fees, slippage, drawdown, win rate, profit factor, and R-multiples.
- Paginated multi-year historical downloader and local dataset store.
- Batch research across multiple symbols and timeframes.
- Read-only local web dashboard.

The engine deliberately does **not** place live orders yet. Execution should consume validated setups only after out-of-sample and paper-trading validation.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
python -m pytest
```

## Scan markets

```powershell
python main.py
```

Choose a venue and symbols:

```powershell
python main.py --exchange kucoin --market-type spot --symbols BTC/USDT,ETH/USDT,SOL/USDT
```

A derivatives venue can use its CCXT market type and contract symbols, for example:

```powershell
python main.py --exchange bybit --market-type swap --symbols BTC/USDT:USDT,ETH/USDT:USDT
```

Show NO_TRADE markets:

```powershell
python main.py --show-no-trade
```

Change account assumptions:

```powershell
python main.py --equity 25000 --risk-percent 0.5 --min-score 80
```

After editable installation you can also run:

```powershell
crypto-engine --help
```

## Interpreting output

A **CONFIRMED** setup can include:

- LONG or SHORT direction
- quality score
- regime and volatility
- entry zone and reference price
- structure-aware / ATR stop
- TP1 at 1R and TP2 at 2R
- position size and notional value
- actual vs target account risk
- reasons that contributed to qualification

A **FORMING** setup is intentionally not given a trade plan. It has not met the confirmation threshold.

The setup score is a rules score, **not a probability of profit**.

## Backtesting

```powershell
python run_backtest.py
```

The quick backtest now uses the same decision rules as the market scanner. It is still a short-window smoke test; use the historical research pipeline for serious validation.

## Architecture

```text
Exchange data (CCXT)
        |
Data validation
        |
Indicators
        |
Market regime + structure
        |
Setup evaluator / score
        |
Trade planner + risk sizing
        |
Scanner / ranked setups
```

Core packages live under `src/trading_bot/`:

- `analysis/` indicators and market structure
- `backtesting/` legacy and scanner-aligned simulation
- `decision/` shared decision path used by scanner and research
- `data/` market data normalization, validation, pagination, and historical storage
- `exchanges/` exchange adapters
- `paper/` simulated broker, portfolio risk, journal, and replay
- `planning/` entry/stop/target planning
- `research/` multi-dataset historical experiments
- `risk/` position sizing and controls
- `scanner/` multi-market scanning
- `strategies/` testable strategy rules
- `web/` local research dashboard


## Web dashboard

Install and run the local read-only dashboard:

~~~powershell
python -m pip install -e ".[web]"
python -m uvicorn trading_bot.web.app:app --host 127.0.0.1 --port 8000
~~~

Open http://127.0.0.1:8000. The dashboard does not place orders.

## Multi-year historical research

Download candles once and keep them locally under data/history/:

~~~powershell
crypto-research download --exchange kucoin --market-type spot --symbols BTC/USDT,ETH/USDT,SOL/USDT --timeframes 1h,4h --start 2022-01-01 --end 2026-11-01
~~~

Run the scanner-aligned strategy over those local datasets:

~~~powershell
crypto-research backtest --exchange kucoin --market-type spot --symbols BTC/USDT,ETH/USDT,SOL/USDT --timeframes 1h,4h --min-score 70 --risk-percent 1 --fee-rate 0.001 --slippage-bps 5 --export data/processed/research_summary.csv
~~~

Spot research is long-only. Non-spot research can evaluate both LONG and SHORT setups. Research output includes trade count, return, drawdown, win rate, profit factor, average P&L, average R, and setup-score diagnostics.


## Out-of-sample evaluation

Use explicit chronological cutoffs so later periods stay unseen during development:

~~~powershell
crypto-research oos --exchange kucoin --market-type spot --symbols BTC/USDT,ETH/USDT --timeframes 1h --train-cutoff 2024-01-01 --validation-cutoff 2025-01-01 --min-score 70 --export data/processed/oos.csv
~~~

The output is split into TRAIN, VALIDATION, and TEST periods using the same scanner-aligned rules and cost assumptions.

## Walk-forward stability

Run rolling fixed-parameter train/test windows:

~~~powershell
crypto-research walk-forward --exchange kucoin --market-type spot --symbols BTC/USDT --timeframes 1h --train-bars 8760 --test-bars 2160 --step-bars 2160 --min-score 70 --export data/processed/walk_forward.csv
~~~

This is a stability test, not parameter optimization. Parameter selection per training window is a later research layer.


## Paper trading

Replay the shared decision engine through the simulated broker using a local historical dataset:

~~~powershell
crypto-paper replay --exchange kucoin --market-type spot --symbol BTC/USDT --timeframe 1h --equity 10000 --risk-percent 1 --min-score 70
~~~

Inspect the SQLite journal:

~~~powershell
crypto-paper status --journal data/paper/paper_trading.db
~~~

Verify that paper execution and historical backtesting use the same fill assumptions:

~~~powershell
crypto-paper compare --exchange kucoin --market-type spot --symbol BTC/USDT --timeframe 1h --min-score 70
~~~

The paper broker models pending-entry expiry, fees, slippage, stop/TP2 exits, duplicate exposure protection, portfolio open-risk limits, position tracking, realized/unrealized P&L, and persistent orders/trades. Local paper state is stored under data/paper/ and is ignored by Git.


## Futures selection and verified symbols

**Kraken spot is not Kraken Futures.** In CCXT the venue IDs are
`kraken` (spot) and `krakenfutures` (derivatives). You must select
`swap` for perpetual contracts or `future` for delivery-dated contracts,
then select a symbol actually returned by that venue's market metadata.
`SOL/USDT` is *not* automatically a futures instrument.

Open the dashboard and choose exchange and market type, then click
**Find valid markets**. This requests `/api/markets`, which lists actual
active CCXT markets of that type; select a returned symbol before scanning.
Incorrect venue/type/symbol combinations are rejected rather than
silently interpreted as spot.

**Futures research limitation:** the current OHLCV simulator does not
model exchange-specific contract multipliers, margin, funding, inverse
settlement, liquidation or order-book execution. Its futures P&L is a
synthetic base-unit calculation; do not interpret it as an executable
futures account balance or deploy it with money.

## Verify full multi-year historical coverage

The downloader now rejects partial history **by default**. Many exchange
APIs retain only a limited OHLCV lookback; asking for three years does
not prove three years were returned. It validates every requested bar
timestamp and refuses to save an incomplete dataset:

```powershell
crypto-research download --exchange kucoin --market-type spot --symbols BTC/USDT --timeframes 1h --start 2023-01-01 --end 2026-01-01
```

If the exchange cannot deliver that history, the command exits nonzero
with the actual candle count and dates. Find an authoritative archival
data source for older candles before proceeding. Only if you understand
the coverage gaps, use `--allow-partial` to save a partial dataset; it
will be **explicitly marked PARTIAL**. Avoid reporting partial datasets
as three-year results.

For an actual local dataset covering at least three years:

```powershell
crypto-research backtest --exchange kucoin --market-type spot --symbols BTC/USDT --timeframes 1h --min-history-years 3 --export data/processed/three_year_summary.csv
```

The summary exposes `span_days` and `missing_intervals`. If the
requested duration isn't present, the dataset status is
`INSUFFICIENT_HISTORY`; if internal candles are missing it reports
`GAPS_PRESENT`. **The time-span requirement checks length, not strategy
profitability or data provenance.**

Historical-data research, out-of-sample tests, and walk-forward stability
tests are available through `crypto-research`. Historical datasets are
stored locally under `data/history/`, not in this public repository.

In the browser dashboard, set **Data source → Local multi-year data**
and choose the same exchange, market type, symbol and timeframe as the
saved dataset. The result includes the **actual first and last candle
dates**, unlike quick exchange backtests limited to recent candles.

## Execution verification

The shared `DecisionEngine` feeds both the scanner and research
backtester. The backtest uses completed candles only, a resting reference
entry that must be touched on a subsequent candle, a conservative
stop-first assumption if stop and target touch during the same OHLC bar,
and adverse opening-gap fills for existing positions. Modeled 1R sizing
now includes entry/exit fees and stop slippage. Real stops may slip more
than modeled, and a gap may lose more than 1R.

The test suite covers exchange market identity, incomplete data
detection, local history, multi-year chronological train/validation/test
and walk-forward windows, stop gaps, and trade-risk accounting.
Automated unit tests are not evidence of profitable strategies.
