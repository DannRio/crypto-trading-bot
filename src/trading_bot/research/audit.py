"""Reproducible local-data correctness/edge audit. Never downloads or trades."""
import argparse
from dataclasses import asdict
from hashlib import sha256
import json
import math
from pathlib import Path
import subprocess

import pandas as pd

from trading_bot.backtesting import run_decision_backtest
from trading_bot.data import HistoricalDataStore
from trading_bot.planning import TradePlanConfig
from trading_bot.research.diagnostics import trade_diagnostics
from trading_bot.research.exit_ablation import run_exit_ablation


def _clean(value):
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default="data/history")
    parser.add_argument("--exchange", default="kucoin")
    parser.add_argument("--market-type", default="spot", choices=("spot", "swap", "future"))
    parser.add_argument("--symbol", default="BTC/USDT")
    parser.add_argument("--timeframe", default="1h")
    parser.add_argument("--confirm", default="4h")
    parser.add_argument("--entry", default="15m")
    parser.add_argument("--execution", default="15m")
    parser.add_argument("--start")
    parser.add_argument("--end", help="Exclusive end timestamp")
    parser.add_argument("--fee-rate", type=float, default=.001)
    parser.add_argument("--slippage-bps", type=float, default=5.)
    parser.add_argument("--all-strategies", action="store_true")
    parser.add_argument("--output", default="data/research/strategy-audit-v3")
    args = parser.parse_args(argv)
    output = Path(args.output)
    if output.exists() and any(output.iterdir()):
        parser.error("Output folder is not empty; choose a new folder to preserve prior experiments.")
    store = HistoricalDataStore(args.root)
    confirmations = tuple(x.strip() for x in args.confirm.split(",") if x.strip())
    timeframes = tuple(dict.fromkeys((args.timeframe, *confirmations,
                      *((args.entry,) if args.entry else ()), *((args.execution,) if args.execution else ()))))
    datasets, inputs = {}, {}
    for timeframe in timeframes:
        path = store.dataset_path(args.exchange, args.market_type, args.symbol, timeframe)
        if not path.is_file():
            parser.error(f"Missing saved dataset: {path}. Download matching timeframes first.")
        datasets[timeframe] = store.load(args.exchange, args.market_type, args.symbol, timeframe)
        inputs[timeframe] = {"path": str(path.resolve()), "sha256": sha256(path.read_bytes()).hexdigest(),
                            "rows": len(datasets[timeframe])}
    primary = datasets[args.timeframe].copy()
    if args.start:
        primary = primary.loc[primary.timestamp >= pd.to_datetime(args.start, utc=True)]
    if args.end:
        primary = primary.loc[primary.timestamp < pd.to_datetime(args.end, utc=True)]
    primary = primary.reset_index(drop=True)
    if len(primary) < 221:
        parser.error("At least 221 primary candles are required after date filtering.")
    common = dict(primary_timeframe=args.timeframe, confirmation_timeframes=confirmations,
                  entry_timeframe=args.entry or None, execution_timeframe=args.execution or None,
                  secondary_data={k: v for k, v in datasets.items() if k != args.timeframe},
                  fee_rate=args.fee_rate, slippage_bps=args.slippage_bps,
                  allow_short=args.market_type != "spot", initial_equity=10000, risk_percent=.01)
    output.mkdir(parents=True, exist_ok=True)
    try:
        revision = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL).strip()
        dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], text=True).strip())
    except (OSError, subprocess.CalledProcessError):
        revision, dirty = "unavailable", None
    manifest = dict(code_revision=revision, worktree_dirty=dirty, inputs=inputs, arguments=vars(args),
                    primary_first=primary.timestamp.iloc[0], primary_last=primary.timestamp.iloc[-1],
                    planner=asdict(TradePlanConfig()),
                    warning="Exploratory research, not untouched OOS. No winner selected or live execution enabled. Futures funding/liquidation are not modelled.")
    (output / "manifest.json").write_text(json.dumps(_clean(manifest), indent=2), encoding="utf-8")
    rows = []
    def save(name, result):
        result.trades.to_csv(output / f"{name}-trades.csv", index=False)
        pd.DataFrame(result.equity_curve).to_csv(output / f"{name}-equity.csv", index=False)
        diagnostic = trade_diagnostics(result.trades, result.initial_equity)
        diagnostic["execution"] = result.execution_diagnostics
        (output / f"{name}-diagnostics.json").write_text(json.dumps(_clean(diagnostic), indent=2), encoding="utf-8")
        rows.append(dict(run=name, trades=result.total_trades, return_pct=result.total_return_pct,
                         profit_factor=result.profit_factor, max_drawdown_pct=result.max_drawdown_pct,
                         average_net_r=float(result.trades.r_multiple.mean()) if not result.trades.empty else None,
                         ambiguous_bars=result.execution_diagnostics["ambiguous_bars"]))
        print(f"{name}: {result.total_trades} trades, net {result.total_return_pct:.2f}%", flush=True)
        pd.DataFrame(rows).to_csv(output / "summary.csv", index=False)
    strategies = ("trend_continuation_v2",)
    if args.all_strategies:
        strategies += ("trend_continuation", "breakout_retest", "range_mean_reversion")
    for strategy in strategies:
        save(strategy, run_decision_backtest(primary, strategy_names=(strategy,), **common))
    ablation, results = run_exit_ablation(primary, strategy_names=("trend_continuation_v2",), **common)
    ablation.to_json(output / "effective-exit-configs.json", orient="records", indent=2)
    for variant, result in results.items():
        save(f"v2-{variant}", result)
    save("v2-higher-costs", run_decision_backtest(primary, strategy_names=("trend_continuation_v2",),
         **dict(common, fee_rate=args.fee_rate * 1.5, slippage_bps=args.slippage_bps * 2)))
    save("v2-optimistic-path-sensitivity", run_decision_backtest(primary, strategy_names=("trend_continuation_v2",),
         ambiguity_policy="optimistic", **common))
    print(f"Reports saved in {output.resolve()}. These results do not establish live profitability.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
