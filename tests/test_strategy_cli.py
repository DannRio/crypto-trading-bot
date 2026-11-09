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
