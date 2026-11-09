from trading_bot.paper.cli import build_parser as paper_parser
from trading_bot.research.cli import build_parser as research_parser


def test_research_cli_accepts_strategy_candidates():
    args = research_parser().parse_args(
        [
            "backtest",
            "--exchange",
            "kucoin",
            "--symbols",
            "BTC/USDT",
            "--strategies",
            "breakout_retest,range_mean_reversion",
        ]
    )

    assert args.strategies == (
        "breakout_retest,range_mean_reversion"
    )


def test_paper_cli_accepts_strategy_candidates():
    args = paper_parser().parse_args(
        [
            "replay",
            "--exchange",
            "kucoin",
            "--symbol",
            "BTC/USDT",
            "--strategies",
            "breakout_retest",
        ]
    )

    assert args.strategies == "breakout_retest"



def test_research_cli_accepts_parameter_experiment_command():
    args = research_parser().parse_args(
        [
            "experiment",
            "--exchange",
            "kucoin",
            "--symbols",
            "BTC/USDT",
            "--train-end",
            "2025-01-01",
            "--grid",
            "configs/experiments/example.json",
            "--strategies",
            "trend_continuation",
            "--max-combinations",
            "50",
        ]
    )

    assert args.command == "experiment"
    assert args.train_end == "2025-01-01"
    assert args.max_combinations == 50



def test_paper_cli_accepts_continuous_daemon_options():
    args = paper_parser().parse_args(
        [
            "daemon",
            "--exchange",
            "kucoin",
            "--market-type",
            "spot",
            "--auto-universe",
            "--quote-currencies",
            "USDT,USDC",
            "--max-markets",
            "12",
            "--timeframe",
            "15m",
            "--confirm-timeframes",
            "1h,4h",
            "--strategies",
            "trend_continuation,breakout_retest",
            "--interval-seconds",
            "30",
            "--once",
        ]
    )

    assert args.command == "daemon"
    assert args.auto_universe is True
    assert args.quote_currencies == "USDT,USDC"
    assert args.max_markets == 12
    assert args.confirm_timeframes == "1h,4h"
    assert args.interval_seconds == 30
    assert args.once is True
