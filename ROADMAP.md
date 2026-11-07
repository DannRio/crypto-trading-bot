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

## Phase 3 — Market Analysis
- [x] EMA
- [x] RSI
- [x] ATR
- [x] Volume analysis
- [x] Support/resistance
- [x] Basic market structure
- [x] Trend/range regime detection
- [ ] Multi-timeframe confirmation

## Phase 4 — Strategy Engine
- [x] LONG / SHORT / NO_TRADE baseline
- [x] Trend-continuation setup scoring
- [x] Setup validation rules
- [ ] Breakout/retest strategy
- [ ] Range/mean-reversion strategy

## Phase 5 — Risk & Trade Planning
- [x] Risk-based position sizing
- [x] ATR stops
- [x] Structure-aware stop protection
- [x] Entry zone
- [x] TP1 / TP2 and R-multiples
- [x] Notional exposure cap
- [ ] Portfolio-wide exposure limits
- [ ] Daily loss controls

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
- [ ] Per-window parameter optimization

## Phase 7 — Market Scanner
- [x] Scan multiple symbols
- [x] Setup filtering and scoring
- [x] Trade-plan generation
- [x] Rank results
- [ ] Multiple-timeframe scan
- [ ] Alerts

## Phase 8 — Paper Trading
- [ ] Simulated broker
- [ ] Position tracking
- [ ] P&L
- [ ] Trade journal
- [ ] Performance comparison

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
