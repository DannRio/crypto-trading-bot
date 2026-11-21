# Independent Regime Signal Bot

This repository now contains a second, isolated signal engine under
`trading_bot.regime_bot`. It does **not** reuse the existing strategy decision
logic. It can therefore be evaluated head-to-head against the original bot.

## Quick start

```bash
python -m pip install -e ".[dev,web]"

crypto-regime analyze \
  --exchange kraken \
  --market-type spot \
  --symbol BTC/USDT \
  --execution-timeframe 15m \
  --context-timeframes 1h,4h \
  --equity 10000 \
  --risk-percent 0.5
```

Start the local API:

```bash
crypto-regime-api --host 127.0.0.1 --port 8081
```

Run an offline backtest:

```bash
crypto-regime backtest \
  --symbol BTC/USDT \
  --execution-timeframe 15m \
  --execution-csv data/raw/btc_15m.csv \
  --context-csv 1h=data/raw/btc_1h.csv \
  --context-csv 4h=data/raw/btc_4h.csv
```

See [`docs/REGIME_SIGNAL_BOT.md`](docs/REGIME_SIGNAL_BOT.md) for the signal
model, risk controls, API contract, and validation requirements.
