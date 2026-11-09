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
