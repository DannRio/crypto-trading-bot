# Crypto Trading Bot

**v0.19 — one persistent paper-trading workflow.** Start the local dashboard, choose a public exchange and market coverage, then explicitly start the paper account. The bot scans closed candles, checks current market liquidity, sizes qualified setups, manages simulated entries/exits, and saves its ledger for restart.

KuCoin spot is the default. The architecture remains exchange-agnostic. The supported operator policy is `trend_continuation_v2` with **4h confirmation → 1h setup → 15m entry**, using the same fixed 400-candle indicator window in analysis, historical tests, and paper scanning.

This release is a working **paper bot and research application**. It does not place real exchange orders, demonstrate a profitable edge, or supply calibrated win probabilities. The existing guarded execution libraries are outside this supported workflow. See [release audit and limits](docs/CORE_RELEASE.md).

## Install and start

Use Python 3.12 or later. From this repository:

```bash
python -m venv .venv
```

Activate the environment on Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
```

Or on macOS/Linux:

```bash
source .venv/bin/activate
```

Install and launch:

```bash
python -m pip install -e .
crypto-bot
```

Open **http://127.0.0.1:8000**. `python -m trading_bot` and `python main.py` use the same launcher. The server starts with the worker stopped; opening a page does not trade or download history.

1. Select the venue and quote currency. Leave the watchlist empty to discover up to ten qualifying liquid spot markets, or enter exact pairs such as `BTC/USDT,ETH/USDT`.
2. Review starting paper equity, risk per trade, and maximum trade notional. The defaults are 10,000 quote units, 0.25% risk, and a 1,000-unit cap.
3. Select **Start paper bot**. Check the displayed worker owner, cycle health, last completed cycle, pending orders, positions, and trade history.
4. Select **Stop** before shutting down when practical. This preserves pending orders and positions. **Management is suspended while stopped**; restart resumes only if the intervening candle history is complete.

The first cycle may take time while exchange metadata and sufficient history are checked. No qualifying setup is a valid outcome. A running process is not proof that every market is healthy; degraded or stale cycle health stays visible.

### Docker

```bash
docker compose up --build -d
```

Open the same local address and start the worker explicitly. Compose runs one dashboard with one controlled worker and a persistent `bot-data` volume. It does not start a second competing daemon. `docker compose down` stops the service without deleting that volume; removing the volume removes its saved account and history.

### Keep accounts separate

```bash
crypto-bot --data-dir ./data-kucoin-experiment
```

An explicit `--data-dir` selects the account, history, and report paths together. Default storage is `data/` under the launch directory. Saved configuration is restored for review, but the worker does not automatically restart. An existing account rejects incompatible strategy, cost, equity, or market configuration; use its original settings or a separate data directory for a new experiment.

Older `continuous_paper.db` journals remain untouched. The core release uses `data/paper/core_paper.db`. It does not silently import old orders into a different policy.

## What remains in the product

| Feature | Purpose |
| --- | --- |
| Paper home | Start/stop, actual worker ownership, health, persistent portfolio, and closed trades |
| Analyze a coin | Resolve a coin name or ticker to an exact market; inspect technical evidence, charts, conditional levels, costs, and expiry |
| Historical validation | Explicitly prepare matching 4h/1h/15m data, then run the shared signal policy with fees and slippage |
| Durable account and risk controls | Exclusive writer, atomic journal transitions, restart reconciliation, risk/exposure limits, and loss halts |
| Advanced research commands | Explicit historical, batch, and alternative-strategy experiments |

The duplicate dry-run approval queue, execution-status dashboard, prototype scripts, old simulator, and multiple normal launch commands were removed. Existing risk controls and research diagnostics remain because they help detect bad results.

## Analyze a coin

Open **http://127.0.0.1:8000/analyze**, or use:

```bash
crypto-bot analyze "Bitcoin" --exchange kucoin
crypto-bot analyze QNT --risk-percent 0.25 --notional 1000
crypto-bot analyze ETH --fundamentals
```

Fundamentals and announcements are optional explanatory context. Provider outages do not secretly change the technical strategy. Critical events are identified for manual review; they are not replayed as if their present-day information existed historically.

A `LONG_READY` report is a conditional, expiring research snapshot. It does not submit an order. Spot operation is long-only; bearish spot signals may be `NO_TRADE`. Perpetual analysis can describe short setups, but leverage, liquidation and venue-complete funding mechanics are outside the paper bot's supported spot account. [Full analysis documentation](docs/COIN_ANALYSIS.md).

## Historical validation

Expand the research section in the home page, enter an exact pair and a UTC date range, and select **Prepare history**. The job downloads the three required timeframes plus 400 warmup candles for each. The end is limited to the latest completed 4h boundary. A provider that returns truncated or gapped history produces an explicit failure, not a successful partial backtest.

Once preparation completes, run validation explicitly. The requested period excludes the preceding warmup from evaluation. Indicators use only trailing closed candles. Results include return, drawdown, trade count, fees/slippage diagnostics and the policy fingerprint.

These are single-market historical simulations. They do not replay historical order books or shared-capital portfolio limits. Current universe selection can introduce survivorship bias. Paper submissions use actual submission time and skip the partial bar that began before submission, so their fills can differ from an idealized candle-close backtest. The same rule configuration does not guarantee identical realized fills.

Advanced commands are available through one launcher:

```bash
crypto-bot research --help
crypto-bot paper --help
```

Alternative strategies and research profiles are experiments, not automatically selected winners. Old `crypto-engine`, `crypto-paper`, `crypto-research`, `crypto-analyze`, and `crypto-execution` console aliases are no longer installed. Use `crypto-bot analyze`, `crypto-bot research`, and `crypto-bot paper`; the old standalone scanner remains available explicitly through `python -m trading_bot.cli` for research.

## Risk and persistence

The core account uses a 0.25% default risk budget, maximum five open positions and five pending orders, a 100% gross-notional ceiling, symbol/correlation constraints, and daily/weekly/drawdown halts. Fees default to 0.1% per side and slippage to 5 basis points per side; these are configurable modeling assumptions, not verified account fee tiers. Stops are risk estimates and cannot guarantee a maximum loss through gaps.

The SQLite journal and OS writer lease permit one account writer. Read-only status does not initialize or repair a journal. Order state, realized trades, checkpoints and risk state are committed together. A persistence failure stops the worker; ambiguous commit outcomes require reopening and reconciling the ledger. This requires a local filesystem with working OS locks, not distributed shared storage.

Missing price history prevents checkpoint advancement and new submissions for the affected market. The bot does not invent the missing path or claim an unobserved stop was managed. Automatic long-range recovery backfill is not implemented; saved orders and the failed checkpoint remain visible for investigation.

All stored timestamps use UTC. The dashboard displays operational timestamps in **America/Halifax**; historical date inputs are labeled UTC.

## Test

```bash
python -m pip install -e ".[dev]"
python -m pytest
python tests/smoke_core_container.py
```

CI also checks the installed Docker image, Windows account ownership/recovery, and both browser workflows. The standalone lifecycle smoke disables network access and exercises a real paper service through submit, stop, restart, fill, close, and another restart. Synthetic test P&L is an accounting assertion, not a trading result.

For browser checks:

```bash
python -m pip install playwright
python -m playwright install chromium
python tests/browser/dashboard_smoke.py
python tests/browser/coin_analysis_smoke.py
```

Bind this unauthenticated development dashboard locally. The default launcher and Compose configuration do so. A public deployment requires a separate access-control/deployment design.

## Remaining evidence

No real historical profitability report or forward paper track record is bundled with this release. A credible trading decision requires data-quality review, untouched out-of-sample results, cost sensitivity and forward observations. Automated real-money execution also requires account-specific precision, minimums, permissions, reconciliation, failure drills and monitoring on the actual venue. More features do not substitute for that evidence.

See [release audit](docs/CORE_RELEASE.md) and [roadmap](ROADMAP.md) for the supported boundary and remaining work.
