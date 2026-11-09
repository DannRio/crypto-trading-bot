# Crypto Trading Decision & Research Engine

## Phase 1 — Foundation
- [x] Professional project structure
- [x] Shared models and enums
- [x] Existing tests migrated
- [x] Existing prototype migrated without regression
- [x] CLI entry point
- [x] Read-only web dashboard
- [ ] Central configuration profiles

## Phase 2 — Market Data
- [x] Historical bulk-data loader
- [x] OHLCV validation
- [x] Missing candle detection
- [x] Duplicate detection
- [x] Timestamp normalization
- [x] Multi-exchange data interface
- [x] Dynamic liquid-market universe

## Phase 3 — Market Analysis
- [x] EMA
- [x] RSI
- [x] ATR
- [x] Volume analysis
- [x] Support/resistance
- [x] Basic market structure
- [x] Trend/range regime detection
- [x] Higher-timeframe regime confirmation

## Phase 4 — Strategy Engine
- [x] LONG / SHORT / NO_TRADE baseline
- [x] Trend-continuation setup scoring
- [x] Setup validation rules
- [x] Configurable strategy registry
- [x] Breakout/retest strategy candidate
- [x] Range/mean-reversion strategy candidate

## Phase 5 — Risk & Trade Planning
- [x] Risk-based position sizing
- [x] ATR stops
- [x] Structure-aware stop protection
- [x] Entry zone
- [x] TP1 / TP2 and R-multiples
- [x] Notional exposure cap
- [x] Portfolio-wide open-risk and position limits
- [x] Gross-notional exposure limit
- [x] Correlation-aware concentration limit
- [x] Daily loss controls
- [x] Weekly loss controls
- [x] Persistent portfolio drawdown halt

## Phase 6 — Research & Backtesting
- [x] Shared scanner/backtest DecisionEngine
- [x] Fee support
- [x] Slippage support
- [x] Long + short simulation
- [x] Performance metrics
- [x] Average R and setup-score diagnostics
- [x] Spot long-only research enforcement
- [x] Multiple years of local historical data
- [x] Multiple-asset batch research
- [x] Train / validation / out-of-sample testing
- [x] Fixed-parameter walk-forward stability testing
- [x] Train-only parameter experiment registry
- [ ] Per-window parameter optimization

## Phase 7 — Market Scanner
- [x] Scan multiple symbols
- [x] Setup filtering and scoring
- [x] Trade-plan generation
- [x] Rank results
- [x] Multiple-timeframe scan
- [x] Persistent deduplicated alerts

## Phase 8 — Paper Trading
- [x] Simulated broker
- [x] Continuous restart-safe paper service
- [x] Per-market closed-candle checkpoints
- [x] Persistent signal deduplication
- [x] Broker state and P&L recovery after restart
- [x] Read-only web paper-service status
- [x] Position tracking
- [x] Realized/unrealized P&L
- [x] Persistent SQLite trade journal
- [x] Paper-vs-backtest execution comparison
- [x] Restart-persistent portfolio risk state
- [x] Manual guarded risk-halt reset

## Phase 9 — Assisted Trading
- [ ] Approve/reject workflow
- [ ] Authenticated broker abstraction
- [ ] Order management

## Phase 10 — Automated Execution
- [ ] Live exchange adapter
- [ ] WebSocket market data
- [ ] Duplicate-order protection
- [ ] Partial fills
- [ ] Reconciliation
- [ ] Kill switch
- [ ] Monitoring
- [ ] VPS/Docker deployment
