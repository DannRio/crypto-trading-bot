# v0.19 core release audit

## Product decision

The previous project had independent scanner, analysis, assisted dry-run approval and continuous paper flows. The approval adapter could record a simulated immediate fill outside the persistent PaperBroker account, so a dashboard approval did not demonstrate that the continuous paper worker could manage that trade. The operator surface now controls one durable paper account; separate research reports remain explicitly informational.

This release completes that paper workflow. It does not enable unattended real-money execution or establish profitability. No saved market-history dataset or real account record was present in the development checkout, so no actual strategy return is claimed.

## Removed and retained

| Removed or changed | Reason |
| --- | --- |
| Root prototype indicator/data/strategy/risk/backtest scripts | Superseded implementations could disagree with the package |
| `backtesting/engine.py`, old trend strategy, `StrategyResult`, legacy `TradePlan` | Unused older simulation/model path; retained the current sizing and completed-trade models |
| Old UTF-16 requirements freeze | Package installation is defined by `pyproject.toml` |
| Assisted approval/order/execution-status API routes and home panels | The dry-run ledger was separate from the continuously managed paper portfolio |
| Five installed console aliases | One supported launcher with explicit research subcommands |
| Second Compose paper service | The dashboard owns the worker; no competing default control path |
| Automatic strategy mixture in core analysis | One identifiable policy can be compared across workflows |
| Mandatory fundamentals availability | Present-day external context cannot be validated as a historical strategy gate without historical evidence |

Portfolio risk controls, atomic audit records, historical quality checks, cost diagnostics, alternative research strategies and execution guard libraries were retained. The execution libraries have no installed live console alias and are not wired into the new controller. Existing journals and research data are not deleted.

## Reproduced failures and corrections

| Failure observed in tests/code | Correction |
| --- | --- |
| A close could commit the CLOSED order before its realized trade; restart then lost recorded P&L | Order/trade/risk/checkpoint mutations share a transaction; rollback restores in-memory broker state |
| A bounded recent-candle response could skip an outage gap, including possible stops | Verify exact continuity from the saved checkpoint; no checkpoint advancement across missing bars |
| Two workers could write the same account from separately restored state | Persistent OS writer lease; reads use a separate read-only connection |
| Checkpoint/expiry and submission/signal identity could commit independently | Atomic transitions and durable deduplication, including ambiguous commit handling |
| Submission time could be backdated to the signal candle open | Use actual UTC submission time; conservatively skip bars that began before submission |
| Historical all-history EMA initialization differed from the scanner's bounded window | Shared trailing 400 raw closed-candle indicator window in primary, confirmation and entry decisions |
| KuCoin spot/futures selection could differ by caller | Central adapter and history namespace routing |
| Historical start-date filtering discarded warmup | Retain preceding 400 candles, exclude them from the selected evaluation period, and require matching secondary coverage |
| History CSV replacement could truncate an existing dataset on interruption | Flush/fsync a same-directory temporary file, then atomically replace |

Durable commit errors poison the active writer and stop the service. The next process reconciles persisted order/trade state before managing the account. Status does not rewrite or reset an unreadable account.

## Operating contract

`crypto-bot` launches one local web process. The worker starts only after an explicit dashboard command. It creates its journal connection inside the worker thread, persists compatible configuration after startup checks, uses interruptible waits, and releases its lease on shutdown. An external owner is displayed and cannot be stopped by a dashboard that does not own it.

The core policy is spot-only, with 0.25% default risk, a 1,000 quote-unit maximum trade notional and configured fees/slippage. The absolute cap stays fixed as P&L changes equity. A policy ID/fingerprint records the shared strategy, timing, window, sizing and cost settings; starting account equity is separately persisted.

Stop preserves account state and suspends management. The process must run to simulate stops/exits. After downtime, missing intervening candles cause an explicit management error. A partial current bar is never retroactively used to fill an order submitted partway through it. No automatic long-range recovery backfill is implemented.

The core journal is `data/paper/core_paper.db`; its saved settings are beside it. Old `continuous_paper.db` and other research journals are not automatically migrated. Use an explicit separate data directory for a new configuration. Account locking and SQLite require a local filesystem with working OS locks.

## Validation boundary

The regression suite covers account ownership, cancellation, transaction failures, lost commit acknowledgements, recovery, liquidity evidence expiry, closed-candle timing, strategy window parity, API validation and research coverage. Browser tests exercise actual HTML/JavaScript against synthetic API responses, including stale replies, errors, mobile layout and explicit start/stop. A standalone installed-package smoke runs the real runtime/service/broker/journal through three starts and one closed trade with networking disabled.

GitHub Actions runs the full Python suite with real dependencies, Windows ownership/recovery checks, both browser smokes and the installed Docker image. Consult the workflow for the exact revision and results; local injected-provider checks alone are not a substitute for the clean-install gates.

## Remaining limitations

- Live public-data availability and venue/account eligibility depend on the actual operating environment. Deterministic tests do not validate those on the user's machine.
- Historical results are single-market simulations. They do not reproduce historical order books, current fundamental/event data or a shared-capital multi-market portfolio.
- Paper timing includes polling latency and conservative partial-bar exclusion. Historical testing evaluates an idealized sequence of closed candles. Their signal rules align, but fills need not match.
- Public book slippage can raise the risk estimate used to admit/size a paper order. Later OHLC fills still use the configured execution-cost model; evidence records these assumptions.
- Derivative research is not an account-level leverage, liquidation or full future-funding simulation.
- The local API has no authentication. A hosted service needs an explicit deployment/access-control design.
- No actual out-of-sample or forward performance evidence was generated by this simplification. Strategy scores remain rule scores; win probability remains uncalibrated.

The release makes failed data/recovery checks visible instead of manufacturing a completed trade or an attractive performance result.
