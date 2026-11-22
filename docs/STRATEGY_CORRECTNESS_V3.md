# Strategy correctness audit — research v3

This branch is based on PR #30 (`8ba6f8e`). It is **not a validated trading edge**.
Nothing arms live trading or starts the continuous paper service. The legacy
strategy remains in Python for comparison. The dashboard recommends the v2
research candidate, not a proven winner. The new simulation and sizing mean
legacy-strategy returns will NOT exactly reproduce the old engine's benchmark.

## Corrected

- Breakout/retest must actually touch, reject and hold its original breakout
  level. Failed retests cannot qualify through bonus points. The anchor and
  event identity are frozen at the original breakout candle.
- Trend v2 requires directional, non-extended momentum on both paths, limits
  EMA extension, checks a preceding impulse for pullbacks, and tags breakout
  and pullback events separately.
- Range entries require a boundary rejection and a close inside the range.
  The classifier additionally requires separated interactions with both bounds.
- Optional event metadata carries the original invalidation into the planner.
  An excessively distant structural stop rejects the trade instead of being
  replaced by a closer ATR stop. Range targets remain within the opposite edge.
- New event plans screen the final target's net reward/risk and reserve costs
  at the worst allowed entry. Fills are resized downward to the cash-risk
  budget. Gaps through stops can still exceed this budget.
- Setup identity, expiry and structural/context invalidation are separate from
  a fresh trigger. A pending event does not require a second breakout. Consumed
  event IDs prevent repeat attempts; the paper journal retains them on restart.
- Spot eligibility, timeframe gates and plan viability run BEFORE ranking. Rule
  scores are explicitly not probabilities or comparable net expectancy estimates.
- NaN, infinity, non-numeric values, invalid/misaligned/unsorted timestamps and
  stale snapshots fail closed. Primary gaps and incomplete execution bars reject
  the run. Child OHLC must aggregate to the primary candle. Loading a CSV no
  longer silently hides duplicated timestamps.
- Research and paper execution share the ordered-price-path kernel, price/cost
  break-even calculations and fee-aware sizing. Only the selected speculative
  paper path writes to the journal; other paths never create persisted trades.
- Full 2R / partial-no-BE / partial-BE ablations override each strategy profile
  as well as the default profile. Effective settings are recorded. These are
  full-system comparisons, NOT paired observations of identical entry events.
- Diagnostics add setup subtype, direction, MFE/MAE and ambiguous bar counts.
  Mark-to-market equity is distinct from the closed-trade equity series.
- Both dashboard selectors include v2. The legacy baseline remains explicit.
  All corrected candidates excludes legacy. No score is labelled a win chance.

## What execution assumptions mean

OHLC cannot establish the actual path inside a candle. Replay considers the
O-H-L-C and O-L-H-C paths and a feasible TP1-then-reversal path. Stops and targets
only act after their activation. This handles pre-entry extrema and prevents
retroactively using old prices as new break-even stops. Gap-through stops fill
at the adverse open; gapped targets conservatively fill at the target level.

`ambiguity_policy="conservative"` selects the lower marked-equity result among
these paths; `"optimistic"` selects the higher. They are sensitivity scenarios,
**not mathematical bounds on every possible intrabar path and not observed tick
fills**. The trade MFE/MAE uses the selected path and is also assumption-dependent.
Use matching finer candles and inspect ambiguous bar counts. Mark-to-market
maximum drawdown is sampled at execution-candle closes, not every tick.

The order model remains an idealized entry-zone touch, not an exchange limit
queue. Spread is covered only to the extent it is represented by the chosen
slippage assumption. Funding, borrowing, contract-specific/inverse P&L,
liquidation, partial liquidity fills and latency are not modelled here. Do not
interpret the linear research model as futures execution validation.

A cost-adjusted break-even stop covers the remaining quantity's proportional
entry fee plus modelled exit fee/slippage, not adverse gaps. Existing paper
journal rows retain `price` break-even on migration; new plans use `cost`.
Use a fresh paper journal for a new controlled experiment.

## Run the local-data audit

Protect local edits before switching branches. This is a stacked PR against
`research/historical-mtf-gates-phase1`; `main` is intentionally unchanged.
Install and test from the repository root:

```powershell
python -m pip install -e ".[dev]"
python -m pytest
```

Run matching saved KuCoin BTC/USDT spot 4h/1h/15m data:

```powershell
python -m trading_bot.research.audit --root data/history --exchange kucoin --market-type spot --symbol BTC/USDT --timeframe 1h --confirm 4h --entry 15m --execution 15m --all-strategies --output data/research/strategy-audit-v3
```

The command does NOT fetch data, place orders, optimize parameters or select a
winner. It writes dataset hashes, code revision/dirty flag, effective configs,
trade CSVs, marked equity, diagnostics and summary results. It refuses to
silently overwrite an existing nonempty experiment folder. Missing required
timeframes produce an explicit error, never a fallback to different data.

The scanner and historical gates make decisions at the primary candle close;
the lower-timeframe trigger is sampled at that time. This is NOT an asynchronous
lower-timeframe event watcher. Paper/live parity across polling times, restart
boundaries, multiple simultaneous markets and actual venue execution still
requires forward validation.

## Still required before considering deployment

The user's saved multi-year datasets are not committed to the repository.
Synthetic tests establish implementation behaviour, not profitability. Obtain
positive net expectancy under realistic costs on genuinely unexamined data;
check parameter stability, cost stress, concentration and portfolio drawdown;
then run forward paper observation. Previously inspected 2023–2025 data remains
development data regardless of a later 60/20/20 split. Do not tune against a
holdout and continue calling it untouched.

A paired, frozen-entry exit experiment, a calibrated expectancy-based selector,
confidence intervals with serial dependence, and a venue-complete futures
simulator remain separate research work. The rule-score selector is now gated
correctly but is still a heuristic ranker, not an expected-profit model.
