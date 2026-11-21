# Regime Signal Bot: Design and Operations

## Purpose

The regime bot is an independent, signal-only engine that answers two separate
questions:

1. Is the market currently directionally **bullish**, **bearish**, **ranging**,
   or **transitioning**?
2. Is there a confirmed, risk-defined entry now, or should the bot wait for a
   specific trigger?

It does not claim to predict every price move. It deliberately returns
`NO_TRADE`, `WATCH_LONG`, or `WATCH_SHORT` when conditions are incomplete. The
`confidence` value is a normalized rules score, **not** a calibrated probability
of profit.

## Architecture

The implementation lives in `src/trading_bot/regime_bot/` and is isolated from
the existing strategy engine.

| Module | Responsibility |
|---|---|
| `indicators.py` | EMA, RSI, ATR, ADX/DMI, MACD, volume, Donchian, swing and volatility features |
| `engine.py` | Per-timeframe classification, multi-timeframe consensus, setup detection and risk plan |
| `service.py` | Exchange-agnostic CCXT market-data retrieval and closed-candle filtering |
| `backtest.py` | Next-bar, cost-aware backtesting without higher-timeframe lookahead |
| `cli.py` | Live analysis, watch mode and CSV backtesting |
| `api.py` | Local FastAPI endpoints for live or supplied OHLCV analysis |
| `config.py` | Validated immutable configuration |
| `models.py` | Typed signal, plan, timeframe and backtest records |

## Regime model

Each timeframe receives a directional score from `-100` to `+100` using
independent evidence groups:

- EMA ordering and price location;
- medium- and slow-trend slope;
- ADX trend strength and +DI/-DI direction;
- MACD and RSI momentum;
- position within or beyond the prior Donchian range.

Higher timeframes receive more weight. A trade is rejected when the execution
and highest context timeframe materially disagree, or when weighted consensus
is below the configured threshold.

The default regime labels are:

- `strong_bullish`
- `bullish`
- `range`
- `transition`
- `bearish`
- `strong_bearish`

## Entry logic

The bot supports two setup families in either direction:

### Breakout

A breakout requires:

- a close beyond the previous Donchian range;
- the previous close not already beyond that level;
- relative volume above the configured floor;
- a candle body that is not abnormally large versus ATR;
- acceptable distance from the fast EMA and acceptable volatility.

### Pullback continuation

A pullback requires:

- correctly ordered fast, medium and slow EMAs;
- a controlled retracement toward the fast/medium EMA area;
- reclaim or rejection of the fast EMA;
- a close beyond the prior candle trigger;
- RSI inside a continuation band rather than at an extreme.

A directional regime without one of these confirmations produces
`WATCH_LONG` or `WATCH_SHORT`, not an executable signal.

## Entry and exit plan

Every executable signal includes:

- trigger price;
- allowed entry zone;
- cost-adjusted reference entry;
- structure/ATR invalidation stop;
- TP1 at 1R;
- TP2 at 2R;
- TP3 at 3R;
- cost-aware position size;
- estimated round-trip trading cost;
- move-to-break-even rule after TP1;
- ATR trailing rule after TP2;
- maximum holding-period exit.

Position sizing is constrained by both account risk and maximum notional. The
default account risk is 0.5% and requests above 2% are rejected. Estimated fees
and slippage are included in the risk budget.

## Closed-candle safety

Live mode drops the currently forming candle by default. Use
`--include-open-candle` only for diagnostics; it can cause signals to disappear
before candle close. Live requests also reject market data older than three
candles by default. Configure this with `--maximum-staleness-bars`; use `0` only
when deliberately disabling the safety check.

The backtester:

- builds a signal only after a candle closes;
- enters at the next candle open;
- uses only higher-timeframe candles that were fully closed at decision time;
- charges configured fees and slippage;
- assumes the stop fills first when a single candle touches both stop and target;
- fills a gap through the stop from the adverse next-bar open rather than the old stop price;
- preserves the original structural invalidation level when the next-bar entry price differs;
- makes new trailing stops effective on the next candle.

These rules reduce, but do not eliminate, simulation error.

## CLI

### One live analysis

```bash
crypto-regime analyze \
  --exchange kraken \
  --market-type spot \
  --symbol BTC/USDT \
  --execution-timeframe 15m \
  --context-timeframes 1h,4h \
  --limit 500 \
  --equity 10000 \
  --risk-percent 0.5
```

Add `--json` for machine-readable output.

### Watch mode

```bash
crypto-regime watch \
  --exchange kucoinfutures \
  --market-type swap \
  --symbol BTC/USDT:USDT \
  --execution-timeframe 15m \
  --context-timeframes 1h,4h \
  --interval-seconds 60
```

Watch mode prints only when the signal fingerprint changes. It does not place
orders.

### Backtest CSV data

Every CSV must contain:

```text
timestamp,open,high,low,close,volume
```

Run:

```bash
crypto-regime backtest \
  --symbol BTC/USDT \
  --execution-timeframe 15m \
  --execution-csv data/raw/btc_15m.csv \
  --context-csv 1h=data/raw/btc_1h.csv \
  --context-csv 4h=data/raw/btc_4h.csv \
  --equity 10000 \
  --risk-percent 0.5 \
  --json
```

## API

Install the web extra and bind locally:

```bash
python -m pip install -e ".[web]"
crypto-regime-api --host 127.0.0.1 --port 8081
```

Endpoints:

- `GET /health`
- `GET /v1/live-signal`
- `POST /v1/analyze`

Example live request:

```text
GET /v1/live-signal?exchange=kraken&market_type=spot&symbol=BTC%2FUSDT&execution_timeframe=15m&context_timeframes=1h%2C4h&equity=10000&risk_percent=0.5
```

Do not expose this development API directly to the internet without an
authenticated reverse proxy, TLS, request limits, logging and operational
monitoring.

## Configuration

Start from `configs/regime_bot.default.json`:

```bash
crypto-regime --config configs/regime_bot.default.json analyze ...
```

Unknown keys and unsafe risk values are rejected rather than silently ignored.
Do not optimize all thresholds on one dataset; that is overfitting, not edge
discovery.

## Required validation before real-money use

A functioning signal engine is not the same as a demonstrated trading edge.
Before any live execution integration, require all of the following:

1. Multi-year testing across bull, bear, range and high-volatility regimes.
2. Separate in-sample, validation and untouched out-of-sample periods.
3. Walk-forward analysis with parameters frozen before each test window.
4. Realistic taker/maker fees, spread, slippage, funding and rejected/missed fills.
5. Results by asset, exchange, year, direction, setup type and volatility regime.
6. Positive expectancy and profit factor that remain acceptable after costs are
   increased materially.
7. Monte Carlo trade-order analysis and drawdown limits.
8. Paper trading with signal/fill reconciliation before any order-routing code.

The bot should remain signal-only until those checks show a robust edge over
hundreds or thousands of out-of-sample trades.
