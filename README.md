# Crypto Trading Decision & Research Engine

A Python engine for scanning crypto markets, producing rule-qualified trade setups, sizing risk, and backtesting strategies across CCXT-supported exchanges.

## Current v1 capabilities

- Universal public market-data access through CCXT.
- OHLCV normalization and quality validation.
- EMA 50/200, RSI 14, ATR 14, and volume baseline.
- Trend/range regime detection plus basic market structure.
- Trend-continuation setup scoring: CONFIRMED, FORMING, or NO_TRADE.
- Risk-defined trade plans with entry zone, stop, TP1, TP2, size, notional, and actual account risk.
- Multi-symbol scanning and ranking.
- Baseline backtester with long/short, fees, slippage, drawdown, win rate, and profit factor.

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

The current backtester is a baseline research engine. Before real-money automation, expand it to multi-year local data, realistic venue-specific costs, out-of-sample testing, and walk-forward testing.

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
- `backtesting/` simulation and metrics
- `data/` market data normalization and validation
- `exchanges/` exchange adapters
- `planning/` entry/stop/target planning
- `risk/` position sizing and controls
- `scanner/` multi-market scanning
- `strategies/` testable strategy rules


## Web dashboard (local development)

The optional read-only app uses the existing `MarketScanner` and backtest engine.
It never loads exchange credentials or places orders.

```powershell
# From the repository root, after switching to this branch:
python -m pip install -e ".[web]"
python -m uvicorn trading_bot.web.app:app --host 127.0.0.1 --port 8000
```

Open **http://127.0.0.1:8000** to scan up to 8 CCXT markets, inspect
CONFIRMED/FORMING/NO_TRADE setups, and run a baseline backtest on one symbol.
The API documentation is at **http://127.0.0.1:8000/docs**.

For tests:

```powershell
python -m pip install -e ".[dev]"
python -m pytest
```

**Development limitations:** no login, scheduled scanning, notifications,
paper broker, live order placement or portfolio reconciliation. Bind on
**127.0.0.1 only**, not a public interface. The backtest includes configurable
illustrative fees and slippage but omits contract funding, liquidation and
venue-specific matching rules. Risk and notional are denominated in the
symbol's quote currency; use a matching account-equity denomination.
