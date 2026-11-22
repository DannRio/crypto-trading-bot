# Multi-asset development and operation — v0.17

## What this release completes

The original three-symbol dashboard/paper defaults are replaced by exchange discovery. Scanner, paper daemon and dashboard now expose broader coverage without a hard-coded investment list. The strategy engine still returns LONG, SHORT or NO_TRADE with qualified entries, stops, targets and size. Spot is long-only; linear derivative signals remain research/paper features unless separately supported by the existing guarded execution code.

This release integrates the historical-engine and strategy-correctness work from the earlier research branches. It adds shared configuration, more reliable market/data eligibility, repeatable multi-asset research, resilient continuous paper operation, and deployment packaging. It does not establish a profitable strategy or enable live execution.

## 1. Discover more assets

```bash
crypto-engine --profile liquid-spot --exchange kraken --quote-currencies USD
crypto-engine --profile broad-spot --exchange kucoin --quote-currencies USDT
crypto-engine --profile liquid-perps --exchange bybit --quote-currencies USDT
```

These are venue/interface examples, not exchange or asset recommendations. Availability depends on the selected exchange's actual metadata, public data access and filters. A cap of 50 is a maximum; fewer markets can qualify.

| Profile | Maximum assets | Minimum 24h quote volume | Maximum spread | Per-setup risk |
| --- | ---: | ---: | ---: | ---: |
| `liquid-spot` | 30 | 5,000,000 | 40 bps | 0.5% |
| `broad-spot` | 50 | 1,000,000 | 60 bps | 0.25% |
| `liquid-perps` | 30 | 5,000,000 | 40 bps | 0.5% |

All presets use the corrected trend-v2 research candidate, 1h primary candles, 4h context, 15m entry confirmation, 250 closed primary candles for universe preflight, a 100-market history-probe budget and a 200-request ticker fallback budget. Paper profiles limit gross notional to 100% of equity, total open risk to 3%, positions to five and pending orders to ten; the existing correlation/loss controls remain active. These settings are configurable starting assumptions. The scanner itself does not aggregate a portfolio or reserve risk across displayed opportunities.

Discovery refreshes listing metadata and rejects unsuitable quote currencies, inactive markets, stablecoin bases, leveraged tokens, missing/low volume, missing/wide spreads and invalid/insufficient/stale closed history. A base asset contributes at most one selected pair. A second quote pair can qualify if the first fails history checks. Linear derivatives require explicit linear, quote-settled metadata. Ticker spread and turnover are screening proxies, not executable order-book depth.

Public clients and rate limiters are reused within a process. Requests on the same client are serialized. Fallback requests are bounded; warnings disclose incomplete coverage. Multiple worker processes still have independent rate limiters. Mixed quote volumes are not FX-normalized, so use a single quote currency when comparing liquidity thresholds.

Manual symbols remain explicit overrides:

```bash
crypto-engine --profile broad-spot --exchange kucoin --symbols XRP/USDT,LINK/USDT,ADA/USDT,AVAX/USDT,DOT/USDT,NEAR/USDT,ATOM/USDT,LTC/USDT,JUP/USDT
```

The symbols above are syntax examples and must exist on the chosen venue. Passing `--symbols` selects manual mode unless `--auto-universe` is explicitly supplied. `--no-auto-universe` requires nonempty symbols. Without symbols, discovery is the default. Manual mode bypasses the liquidity-universe preflight, while scanner candle/strategy checks still apply.

## 2. Use one configuration across interfaces

`--profile` accepts a built-in name or a JSON file. Explicit CLI flags override profile settings. Profiles override ordinary parser/environment defaults. Abbreviated flags are disabled on profile-aware commands to avoid ambiguous overrides. Credentials and execution-arming fields are not accepted in profiles.

```json
{
  "schema_version": 1,
  "name": "my-liquid-spot",
  "description": "Custom research settings",
  "settings": {
    "market_type": "spot",
    "auto_universe": true,
    "quote_currencies": "USD",
    "max_markets": 40,
    "min_quote_volume": 3000000,
    "max_spread_bps": 35,
    "timeframe": "1h",
    "confirm_timeframes": "4h",
    "entry_timeframe": "15m",
    "strategies": "trend_continuation_v2",
    "risk_percent": 0.25
  }
}
```

The dashboard reads the same built-in presets from `/api/profiles`. It supports up to 100 automatic or manual markets, displays coverage/exclusion counts, and lets a backtest target any scanned symbol or an explicitly typed symbol. A form change invalidates old scan actions. A response for superseded settings is discarded. Fee/slippage assumptions carry through scanner, backtest and assisted staging.

## 3. Freeze a research universe

```bash
crypto-research universe --exchange kucoin --market-type spot --quote-currencies USDT --max-markets 30 --min-quote-volume 5000000 --max-spread-bps 40 --min-history-candles 250 --max-history-probes 100 --output configs/universes/liquid-30.json
```

The new file records selection time, exchange/market type, full filters, exact ordered symbols, candidate observations, warnings, rejection counts and a SHA256 checksum. Existing files are never overwritten by discovery. Downstream research reuses that file without discovering a fresh set. Conflicting exchange/type overrides and edited provenance are rejected.

**The snapshot describes today's membership.** Applying it to older data creates selection/survivorship bias. Freezing the list makes the experiment reproducible; it does not reconstruct historical listings or remove that bias.

## 4. Download and evaluate the same assets

```bash
crypto-research download --universe-file configs/universes/liquid-30.json --timeframes 15m,1h,4h --start 2023-01-01 --end 2026-11-01 --export data/processed/download-coverage.csv

crypto-research backtest --universe-file configs/universes/liquid-30.json --timeframes 1h --confirm-timeframes 4h --entry-timeframe 15m --execution-timeframe 15m --fee-rate 0.001 --slippage-bps 5 --export data/processed/multi-asset.csv

crypto-research oos --universe-file configs/universes/liquid-30.json --timeframes 1h --confirm-timeframes 4h --entry-timeframe 15m --execution-timeframe 15m --train-cutoff 2024-01-01 --validation-cutoff 2025-01-01 --export data/processed/multi-asset-oos.csv

crypto-research walk-forward --universe-file configs/universes/liquid-30.json --timeframes 1h --confirm-timeframes 4h --entry-timeframe 15m --execution-timeframe 15m --train-bars 8760 --test-bars 2160 --step-bars 2160 --export data/processed/multi-asset-walk.csv
```

Date ranges are examples, not a claim that every venue supplies complete history or that those intervals are untouched. New listings may lack the requested years. Read the coverage report. Missing/partial/invalid data remain visible, each asset is isolated, and incomplete batches return a nonzero exit code. Missing secondary timeframes never silently downgrade an MTF run. Strict raw-page checks precede overlap merging, and saved-data loads preserve defects for validation instead of silently sorting them away.

CSV exports include a companion `.csv.manifest.json` with arguments and frozen selection provenance. The more detailed single-market correctness audit remains available in `python -m trading_bot.research.audit`, with input hashes and code revision. This release's batch runs allocate separate starting capital to each asset. Their returns must not be added together or described as one portfolio's performance. A shared-capital historical portfolio simulator remains a separate research stage.

## 5. Run restart-safe paper trading

Use a new journal for a materially changed configuration:

```bash
crypto-paper daemon --profile broad-spot --exchange kraken --quote-currencies USD --journal data/paper/broad-v017.db --once
```

Inspect the result, then omit `--once` for continuous paper operation. Discovery refreshes every 900 seconds by default; ordinary cycles reuse the selected list. A failed refresh immediately blocks new entries from stale selections, while pending/open exposure continues management. A restart always requires fresh discovery. The journal records the last successful selection and attempted refresh separately.

Signal/universe policy is checked against journal metadata on restart. A mismatch requires the original settings or a new journal. Legacy journals missing prior policy records receive an explicit provenance warning; old unrecorded settings cannot be verified retroactively. Paper management now uses actual candle close timestamps, retaining the latest candle from closed-only feeds and excluding a still-forming candle. The scanner shares one frozen decision time per cycle.

Read-only health probe:

```bash
python -m trading_bot.paper.health --journal data/paper/broad-v017.db --max-age-seconds 1800
```

The command returns JSON and exit code 0 only for a recent healthy completed cycle (or a new running cycle after a recent successful one). Missing, stale, degraded or errored service state returns 1. It opens SQLite read-only and does not create a journal. The dashboard exposes the heartbeat and universe status. Choose a maximum age appropriate for the configured polling interval and request budget; a very large universe may take longer to scan.

## 6. Deploy the packaged services

```bash
docker compose up --build -d dashboard
```

Open `http://127.0.0.1:8000`. The container runs as a nonroot user. The dashboard binds to loopback on the host and shares a persistent named data volume. It remains a local development/research interface with no user authentication; use an SSH tunnel for remote access instead of publishing its port to the Internet.

The paper service is opt-in:

```bash
docker compose --profile paper up --build -d
docker compose logs --tail 100 paper
```

Compose uses the `liquid-spot` profile, Kraken and USD by default. `TRADING_EXCHANGE` and `TRADING_QUOTE_CURRENCIES` customize venue/quote selection. Changing persisted service policy may require a new journal path. Stop containers without deleting the named volume to retain state. No exchange keys or live-execution environment flags are supplied by this deployment.

CI builds the actual image, validates Compose, checks packaged CLI entry points, starts the dashboard and probes its API. Separate browser checks use synthetic API fixtures to exercise market expansion, configuration changes and stale-result handling. These are functional checks, not market-performance evidence. A real VPS has not been provisioned by this change.

## Remaining research and execution work

Real-market saved-data runs and forward paper observation must establish net expectancy, drawdown and robustness across assets and regimes. Current-list snapshots and repeated parameter selection cannot substitute for untouched evidence. Shared-capital historical portfolio validation, frozen-entry exit comparisons, serial-dependence-aware uncertainty and calibrated expectancy ranking remain research tasks. Derivatives funding, borrow costs, margin/liquidation, contract sizing and full execution costs remain venue-specific work. Automated live trading, live derivatives and WebSocket execution are not activated by this release.
