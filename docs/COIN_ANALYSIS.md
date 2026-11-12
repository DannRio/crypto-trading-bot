# Coin analysis — v0.19

This feature turns a coin name, ticker, or exact pair into a public-data research report. It is available from the **Analyze a coin** link in the paper dashboard and through `crypto-bot analyze`.

The output is a conditional decision under explicit rules. A ready label does not establish profitable expectancy, a measured win probability, or an executed order. The combined technical, liquidity, and event workflow has not yet been validated as a profitable strategy.

## Run it

From the repository root, with Python 3.12 or later:

```bash
python -m pip install -e .
crypto-bot
```

Open **http://127.0.0.1:8000/analyze**. Enter a name such as `Bitcoin`, a ticker such as `QNT`, or a pair such as `BTC/USDT`. Select the venue, spot/perpetual market, quote, and your analysis assumptions. Names come from exchange metadata; no handwritten coin list or fuzzy ticker guess selects a market.

The existing Docker workflow includes the page and command:

```bash
docker compose up --build -d dashboard
```

Docker persists reports under the existing `/app/data` volume. As with the original development dashboard, bind access locally: the API has no user authentication.

### CLI

```bash
crypto-bot analyze "Bitcoin" --exchange kucoin
crypto-bot analyze QNT --equity 10000 --risk-percent 0.25 --notional 1000
crypto-bot analyze BTC/USDT:USDT --exchange kucoin --market-type swap
crypto-bot analyze ETH --fundamentals
crypto-bot analyze SOL --journal data/analysis/sol-reports.db
```

The command prints the complete journaled report as strict JSON. It does not place an order. `--risk-percent 0.25` means **0.25%**, while `--fee-rate 0.001` means **0.1% per side**. Equity, risk amounts, and notional are denominated in the selected quote currency; USD, USDT, and USDC are supported by this profile. A stablecoin quote is not converted to dollars at a guaranteed exchange rate.

Exit status is `0` for a valid saved report, including WAIT or NO_TRADE; `2` for invalid inputs; and `1` for runtime or persistence failures. Scripts must inspect `decision.action` and `expires_at`, rather than interpreting exit code zero as a trade signal.

## Report contents

| Section | What it contains |
| --- | --- |
| Instrument | Exact venue, canonical pair, market type, base/quote, contract metadata, and identity resolution |
| Decision | LONG_READY, SHORT_READY, WAIT, NO_TRADE, or INSUFFICIENT_DATA; reasons, opposing evidence, and blockers |
| Technical analysis | EMA 50/200, RSI 14, ATR 14, relative volume, support/resistance, regime, strategy events, higher-timeframe alignment, and entry confirmation |
| Charts | Actual closed candles and EMA series from the decision's inputs, with entry zone, stop, and targets |
| Market context | Same-venue BTC comparison, 24-hour relative performance, and recent hourly return correlation when available |
| Liquidity | Active status, 24-hour quote volume, bid/ask spread, both sides of the visible book, estimated fills, and timestamp provenance |
| Fundamentals/events | Verified project identity, supply/valuation, available protocol metrics, matching KuCoin announcements, sources, and coverage gaps |
| Derivatives | Available funding/open-interest observations and explicit missing-cost or margin coverage |
| Trade plan | Conditional entry zone, structural stop, TP1/TP2, exit fractions, modeled risk, size, cost assumptions, cancellation rules, and expiry |
| Journal | Immutable report ID, UTC timestamps, canonical content digest, raw candle inputs, market snapshots, and engine version |

The UI displays timestamps in **America/Halifax**. Stored timestamps remain UTC. Download JSON to inspect or retain the full evidence record.

## Decision rules

The analysis profile uses the existing shared strategy engine:

1. **4h context:** the higher-timeframe regime must be compatible with the selected strategy.
2. **1h setup:** evaluate `trend_continuation_v2` under the shared core policy. Each timeframe uses the same trailing 400 raw closed candles for indicators; a complete window is required.
3. **15m entry:** require the selected strategy's lower-timeframe confirmation.
4. **Instrument and liquidity:** verify the requested market and test the actual proposed exposure against the public order book.
5. **Optional project/event context:** fundamental providers are disabled by default. When requested, identity, supply, events and gaps are explanatory evidence and do not silently change the technical policy. A critical event sets `decision.manual_review_required` and appears in warnings/counter-evidence while preserving the technical action. Notices from other selected exchanges are not checked; that gap remains explicit.
6. **Price and risk:** ensure current executable-side price is inside the entry zone and that stops, targets, size, and modeled costs still qualify.
7. **Freshness and persistence:** expire the report when its required market evidence expires and save the snapshot before returning it.

The chart-pattern labels are the engine's reproducible setup events. They are not an unrestricted visual chart-pattern classifier. Market context and raw fundamentals are evidence; they are not converted into an invented sentiment score or probability.

| Action | Meaning |
| --- | --- |
| LONG_READY | A conditional long plan passes the configured gates at the saved snapshot |
| SHORT_READY | A conditional short plan passes the gates for a supported perpetual contract |
| WAIT | A setup, entry price, or evidence review is incomplete |
| NO_TRADE | No qualifying setup exists, or an instrument/liquidity/risk/invalidation gate fails |
| INSUFFICIENT_DATA | Required metadata, candles, quotes, or book evidence is absent, stale, or invalid |

`decision.score` is the existing technical rule-alignment score. `win_probability` is always null and `validated` is false. More indicators or a higher score do not establish an edge.

### Timing is explicit

The primary decision uses fully closed 1h candles. The higher and lower timeframes are frozen at that same decision close. The charts show those exact inputs, not candles fetched later and presented as if they caused the original decision. A live quote is separately timestamped and used to check whether the old setup is still enterable.

Expiry is bounded by the next 1h decision boundary, a 15-minute report cap, and the remaining quote/order-book freshness window. The default market snapshot lifetime is at most **120 seconds**, so a ready report usually needs refreshing within about two minutes. The UI expires ready labels and treats saved reports as historical snapshots. It does not continuously stream prices or monitor exits.

## Instrument and liquidity checks

KuCoin spot and perpetual data use different CCXT adapters. The workflow uses `kucoin` for spot and **`kucoinfutures` for swaps**, resolving the exchange's actual contract symbol. It does not manufacture a futures symbol or use spot candles for a short thesis.

Only active spot instruments and explicitly linear, quote-settled perpetuals qualify. Stablecoin bases, leveraged-token bases, inverse contracts, inactive markets, and unknown required metadata are rejected or marked insufficient. Ordinary tickers ending in similar letters are checked against the actual exchange catalog to avoid indiscriminate suffix exclusions.

Default liquidity limits are:

| Check | Default |
| --- | --- |
| Minimum 24-hour volume | 1,000,000 units of quote currency |
| Maximum spread | 40 basis points |
| Maximum estimated fill slippage | 50 basis points on either side |
| Maximum snapshot age | 120 seconds |
| Depth display band | 100 basis points from the best price on each side |

The requested notional is tested against both sides of the visible book. Derivative book quantities are converted using the contract multiplier. If the proposed exposure requires more quantity than the original check, it is checked again. An incomplete book fill cannot qualify.

Some public endpoints do not supply a source timestamp. A successfully received response can use its receipt time, with that limitation displayed. A known stale exchange timestamp cannot be overwritten with a fresh receipt. Futures volume may come from a separate contract-statistics endpoint; its provenance is retained separately from the live ticker.

## Costs, sizing, and exits

The plan retains the selected strategy's entry zone, structural stop, and targets. It does not move a stop closer merely to make the reward/risk ratio look better.

Size is capped by the user's risk budget and by the smaller of requested notional and equity. The risk calculation uses the adverse edge of the entry zone, per-side fees, and the larger of configured slippage and observed book slippage. It reports both modeled loss at the stop and the reference-price risk measure. These are estimates; gaps and changing liquidity can produce larger losses.

Targets must remain profitable under modeled costs. The full TP2 net reward/risk and the fraction-weighted TP1/TP2 net reward/risk are reported, with a 1R minimum for the blended target scenario. That scenario assumes the targets are reached; it is not expected value and does not include a measured probability of winning.

TP1/TP2 fractions and the existing move-stop-after-TP1 rule are included in the report. They are instructions for a conditional plan, not managed orders. Quantity is expressed in base-asset units, with a contract-count estimate for swaps; final exchange amount precision and account eligibility are not verified.

Spot cannot open a short in this workflow. A bearish spot view can therefore produce NO_TRADE. Perpetual research does not establish appropriate leverage, required margin, liquidation distance, or future funding cost.

## Fundamental and event coverage

The provider layer makes bounded, read-only requests and records source URLs, observation times when supplied, actual retrieval times, cache status, and failures.

- **CoinGecko:** exact asset identity; market capitalization, fully diluted valuation, circulating/total/max supply, supply ratio, project links, and available project notices.
- **KuCoin:** a bounded recent announcements feed, exact name/ticker matching, publication times, and critical-event review flags. It is incomplete event coverage, not a complete global news service.
- **DefiLlama:** TVL, daily fees, and daily revenue only when a unique protocol mapping matches the verified asset. These figures are not available or meaningful for every coin.

Ambiguous symbols are not guessed. Missing unlock schedules and unavailable provider or event coverage remain visible gaps. Holder concentration and broader adoption/activity metrics are not supplied. A failed request does not mean there are no risks. An optional `COINGECKO_DEMO_API_KEY` is sent only in the provider's header; exchange trading keys are not needed for this feature.

`--fundamentals` or the UI toggle explicitly adds fundamental research context. `--no-fundamentals` remains available; disabled is the default. The choice remains in saved inputs and warnings. Technical readiness does not certify that the coin has passed project or news checks.

Current fundamental observations have `historical_backtest_eligible=false`. They must not be copied backward into historical simulations. The shared strategy backtests do not replay present-day event/liquidity evidence; that requires appropriate historical inputs and forward paper evaluation. Reports carry the core policy and fingerprint so the modeled settings can be compared explicitly.

## API

```http
POST /api/analyze
Content-Type: application/json

{
  "query": "QNT",
  "exchange": "kucoin",
  "market_type": "spot",
  "quote": "USDT",
  "equity": 10000,
  "risk_percent": 0.25,
  "notional": 1000,
  "fee_rate": 0.001,
  "slippage_bps": 5,
  "include_fundamentals": true
}
```

- `200`: a structured, saved analysis, including ambiguity or unavailable-data reports.
- `422`: invalid request; unknown fields and nonfinite numeric values are rejected.
- `503`: analysis or journal persistence could not complete; no successful signal response is returned.

`GET /api/analysis/history?limit=25` returns up to 500 verified summaries. `GET /api/analysis/reports/{report_id}` returns the stored report, 404 if absent, or 503 if its integrity check fails. These endpoints do not reanalyze an archived report or make its old readiness current.

## Journal and verification

The default SQLite file is `data/analysis/reports.db` relative to the process working directory. Set `TRADING_ANALYSIS_DB` or the CLI `--journal` option to choose a different location. Docker sets an absolute path under its mounted data directory. Analysis databases are ignored by Git.

Reports use insert-only storage. Duplicate IDs cannot replace earlier reports, and update/delete guards prevent accidental editing. A SHA-256 digest covers canonical report JSON excluding the journal metadata. Reads verify the digest and indexed identity/timestamps. This detects inconsistent storage; it is not a cryptographic signature against an operator who controls the database and application.

The feature adds deterministic tests for identity resolution, order-book calculations, provider failures, risk/entry gates, API validation, journal integrity, and CLI behavior. The browser smoke uses synthetic reports and checks charts, ambiguity selection, delayed-response rejection, expiry, source safety, download, and mobile layout. CI also verifies that the page and command are present in the built container.

```bash
python -m pip install -e ".[dev]"
python -m pytest
python -m pip install playwright
python -m playwright install chromium
python tests/browser/coin_analysis_smoke.py
```

These are engineering checks. They do not establish live provider availability, fills, account permissions, or trading profitability.
