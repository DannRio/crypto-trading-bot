# Crypto Trading Decision & Research Engine

A Python engine for scanning crypto markets, producing rule-qualified trade setups, sizing risk, and backtesting strategies across CCXT-supported exchanges.

## Current capabilities

- Universal public market-data access through CCXT.
- OHLCV normalization and quality validation.
- EMA 50/200, RSI 14, ATR 14, and volume baseline.
- Trend/range regime detection plus basic market structure.
- Strategy registry with trend-continuation, breakout/retest, and range mean-reversion candidates.
- Risk-defined trade plans with entry zone, stop, TP1, TP2, size, notional, and actual account risk.
- Multi-symbol scanning and ranking.
- Configurable higher-timeframe regime confirmation.
- Dynamic liquid-market discovery and ranking.
- Persistent deduplicated scanner alerts.
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


## Multi-timeframe scanning

The scanner can require one or more higher-timeframe regimes to agree with the primary setup direction. This is a qualification gate; it does not add points to the strategy score.

Example: scan 15-minute setups only when both 1-hour and 4-hour trends agree:

~~~powershell
python main.py --exchange kucoin --market-type spot --symbols BTC/USDT,ETH/USDT,SOL/USDT --timeframe 15m --confirm-timeframes 1h,4h --show-no-trade
~~~

Behavior:

- Primary LONG + all higher timeframes BULL_TREND -> primary status is preserved.
- Primary SHORT + all higher timeframes BEAR_TREND -> primary status is preserved.
- Any opposite higher-timeframe trend -> NO_TRADE and no trade plan.
- RANGE, UNKNOWN, or unavailable higher-timeframe context -> a primary CONFIRMED setup is downgraded to FORMING.
- The original rule score is preserved so the effect of the filter can be measured independently.

The browser dashboard exposes the same higher-timeframe confirmation controls.


## Dynamic market universe

Instead of hard-coding symbols, the scanner can discover markets from the selected exchange and rank the liquid candidates before analysis.

Example:

~~~powershell
python main.py --exchange kucoin --market-type spot --auto-universe --quote-currencies USDT --max-markets 20 --min-quote-volume 5000000 --timeframe 15m --confirm-timeframes 1h,4h
~~~

Auto-universe selection:

- uses CCXT market metadata instead of parsing exchange-specific symbol strings
- keeps the requested market type and quote currencies
- excludes markets explicitly marked inactive
- requires measurable 24h quote volume
- ranks candidates by 24h quote volume
- can optionally reject markets above a ticker bid/ask spread threshold
- caps the final universe before OHLCV scanning
- leaves manual `--symbols` mode available as an override

Optional spread filtering is a coarse liquidity screen only. Ticker bid/ask fields are not a replacement for order-book data and should not be used as the live execution spread model.

The web dashboard exposes the same manual/auto universe choice and filters.


## Scanner alerts

Scanner results can emit structured alerts without coupling the strategy engine to a specific messaging platform.

Example with console and JSONL sinks:

~~~powershell
python main.py --exchange kucoin --market-type spot --auto-universe --timeframe 15m --confirm-timeframes 1h,4h --alert-sinks console,jsonl --alert-cooldown-minutes 60
~~~

Generic webhook delivery:

~~~powershell
$env:TRADING_ALERT_WEBHOOK_URL="https://example.invalid/webhook"
python main.py --alert-sinks webhook
~~~

Alert behavior:

- CONFIRMED setups alert by default.
- FORMING alerts are opt-in with `--alert-forming`.
- scan failures can emit operational alerts.
- a persistent SQLite ledger suppresses repeated identical alerts across process restarts.
- console, JSONL and generic JSON webhook sinks are supported.
- local alert state is stored under `data/alerts/` and ignored by Git.

Webhook URLs are supplied at runtime or through environment variables and are not stored in the repository.


## Strategy variants

The decision engine now has a strategy registry. **Trend continuation remains the default production/research candidate.** The other strategies are opt-in research candidates and are not assumed profitable.

Available strategy names:

- `trend_continuation` — existing trend-following setup scorer.
- `breakout_retest` — looks for a recent break of structure followed by a controlled retest/hold.
- `range_mean_reversion` — only activates in RANGE regimes near support/resistance with reversal confirmation.

Scan one strategy:

~~~powershell
python main.py --exchange kucoin --market-type spot --auto-universe --timeframe 15m --confirm-timeframes 1h,4h --strategies breakout_retest
~~~

Evaluate multiple candidates and let the decision engine select the highest-qualified setup:

~~~powershell
python main.py --strategies trend_continuation,breakout_retest,range_mean_reversion
~~~

Research candidates independently:

~~~powershell
crypto-research backtest --exchange kucoin --market-type spot --symbols BTC/USDT,ETH/USDT,SOL/USDT --timeframes 1h --strategies breakout_retest --export data/processed/breakout_retest.csv
~~~

Or compare candidate selection behavior:

~~~powershell
crypto-research backtest --exchange kucoin --market-type spot --symbols BTC/USDT,ETH/USDT,SOL/USDT --timeframes 1h --strategies trend_continuation,breakout_retest,range_mean_reversion --export data/processed/multi_strategy.csv
~~~

Historical trades record the selected strategy, and batch output includes the resulting strategy mix. Paper orders/trades also persist strategy identity. Existing SQLite paper journals are migrated in place by adding the strategy column.

Adding a strategy to the registry does **not** promote it to live use. Each candidate still needs multi-year, out-of-sample, walk-forward and paper validation.


## Parameter experiments

Parameter experiments are **TRAIN-only research runs**. They do not rank or promote a winner, and they never require using validation/test data to generate the experiment table.

An example grid is included at:

~~~text
configs/experiments/trend_continuation_v1.json
~~~

Run it against local historical data:

~~~powershell
crypto-research experiment --exchange kucoin --market-type spot --symbols BTC/USDT,ETH/USDT,SOL/USDT --timeframes 1h --strategies trend_continuation --train-end 2025-01-01 --grid configs/experiments/trend_continuation_v1.json --export data/processed/trend_experiments.csv
~~~

Supported grid paths include:

- `min_confirmed_score`
- `entry_valid_bars`
- `decision_lookback`
- `planner.atr_stop_multiplier`
- `planner.structure_buffer_atr`
- `planner.entry_zone_atr`
- `planner.max_structure_stop_atr`
- `planner.max_notional_percent`
- `planner.reward_to_risk_1`
- `planner.reward_to_risk_2`
- `strategy.<strategy_name>.<parameter>`

For example:

~~~json
{
  "min_confirmed_score": [60, 70, 80],
  "planner.atr_stop_multiplier": [1.5, 2.0, 2.5],
  "planner.reward_to_risk_2": [1.5, 2.0, 2.5],
  "strategy.trend_continuation.volume_ratio_threshold": [1.0, 1.1, 1.25]
}
~~~

Each experiment row includes a deterministic experiment ID, the exact grid values, the full effective configuration, TRAIN window, trade count, return, drawdown, win rate, profit factor, average P&L, average R, and strategy mix.

The grid is capped at 500 combinations by default to prevent accidental combinatorial explosions. A parameter combination that looks good on TRAIN still has to survive untouched validation, test, walk-forward, and paper-trading gates before it can be promoted.
