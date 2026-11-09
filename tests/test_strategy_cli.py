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



def test_paper_cli_accepts_portfolio_risk_v2_options():
    args = paper_parser().parse_args(
        [
            "daemon",
            "--max-open-risk-percent",
            "2.5",
            "--max-gross-notional-percent",
            "150",
            "--max-correlated-risk-percent",
            "1.5",
            "--correlation-threshold",
            "0.8",
            "--max-daily-loss-percent",
            "1.5",
            "--max-weekly-loss-percent",
            "4",
            "--max-drawdown-percent",
            "8",
            "--correlation-lookback",
            "200",
            "--min-correlation-samples",
            "100",
        ]
    )

    assert args.max_open_risk_percent == 2.5
    assert args.max_gross_notional_percent == 150
    assert args.max_correlated_risk_percent == 1.5
    assert args.correlation_threshold == 0.8
    assert args.max_daily_loss_percent == 1.5
    assert args.max_drawdown_percent == 8


def test_paper_cli_accepts_guarded_risk_reset_command():
    args = paper_parser().parse_args(
        [
            "risk-reset",
            "--confirm",
            "RESET",
            "--max-state-age-minutes",
            "5",
        ]
    )

    assert args.command == "risk-reset"
    assert args.confirm == "RESET"
    assert args.max_state_age_minutes == 5
