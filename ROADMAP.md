# Release status and remaining evidence

## Supported v0.19 product

- One `crypto-bot` launcher and local paper-account dashboard.
- One core spot policy: trend continuation v2, 4h/1h/15m and trailing 400 closed-candle indicators.
- Public market discovery, exact watchlists, liquidity checks and conditional cost-sized plans.
- Durable paper entries/exits, partial exits, effective stops, portfolio risk limits and explicit start/stop.
- Exclusive account writer, atomic order/trade/checkpoint transitions and restart reconciliation.
- Coin analysis with charts, expiring evidence, optional fundamental context and immutable reports.
- Explicit historical preparation with warmup/continuity validation, historical tests and advanced research tools.
- Packaged local/container startup, deterministic lifecycle acceptance, Windows ownership tests and browser checks.

These are implemented capabilities. Test coverage establishes defined software behavior on supplied fixtures; it does not establish a profitable strategy or successful trading on a particular account.

## Removed from normal operation

- Duplicate prototype scripts and the superseded backtest engine/risk-plan types.
- Independent dry-run assisted approval and execution-status dashboard/API routes.
- Multiple console entry points and a second default Compose paper service.
- Automatic selection among unrelated strategy candidates in the core workflow.
- Mandatory fresh news/fundamental-provider availability as a hidden trading gate.

Alternative strategies, advanced historical diagnostics and existing guarded execution libraries remain isolated for explicit research/development. They are not connected to automatic live trading.

## Evidence required before claiming a trading edge

- [ ] Historical data obtained and audited for the selected venue/instruments.
- [ ] Untouched chronological holdout and walk-forward results after realistic costs.
- [ ] Trade count, confidence intervals and sensitivity to costs and execution assumptions.
- [ ] Forward paper observations across multiple market conditions.
- [ ] Shared-capital portfolio simulation and current-universe selection-bias assessment.
- [ ] Historical order-book/event evidence if those checks are to be validated retrospectively.

## Separate real-money release work

- [ ] Explicit automated strategy-to-live-execution integration and operating mode.
- [ ] Account-specific permissions, precision/minimums, cost tiers and order reconciliation acceptance.
- [ ] Venue failure, ambiguous submission, partial-fill, cancellation and restart drills on the intended account/test environment.
- [ ] Deployment authentication, continuous monitoring, alerting and operator recovery procedures.
- [ ] Derivative margin/leverage/position-mode/liquidation/funding model if derivatives execution is requested.

There is no automatic promotion from a green test suite or a profitable historical window to real-money operation.
