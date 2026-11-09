import pandas as pd

from trading_bot.cli import build_parser
from trading_bot.scanner import MarketScanner


def empty_fetcher(**kwargs):
    return pd.DataFrame()


def test_scanner_handles_empty_market_data():
    scanner = MarketScanner(data_fetcher=empty_fetcher)
    result = scanner.scan_symbol("BTC/USDT")

    assert result.error == "No usable market data received."
    assert result.setup is None


def test_cli_parser_accepts_universal_exchange_options():
    parser = build_parser()
    args = parser.parse_args(
        [
            "--exchange",
            "kucoin",
            "--market-type",
            "swap",
            "--symbols",
            "BTC/USDT:USDT",
            "--confirm-timeframes",
            "1h,4h",
        ]
    )

    assert args.exchange == "kucoin"
    assert args.market_type == "swap"
    assert args.symbols == "BTC/USDT:USDT"
    assert args.confirm_timeframes == "1h,4h"



def test_cli_parser_accepts_auto_universe_options():
    parser = build_parser()
    args = parser.parse_args(
        [
            "--exchange",
            "kucoin",
            "--market-type",
            "spot",
            "--auto-universe",
            "--quote-currencies",
            "USDT,USDC",
            "--max-markets",
            "12",
            "--min-quote-volume",
            "5000000",
            "--max-spread-bps",
            "25",
        ]
    )

    assert args.auto_universe is True
    assert args.quote_currencies == "USDT,USDC"
    assert args.max_markets == 12
    assert args.min_quote_volume == 5_000_000
    assert args.max_spread_bps == 25



def test_cli_parser_accepts_alert_options():
    parser = build_parser()
    args = parser.parse_args(
        [
            "--alert-sinks",
            "console,jsonl",
            "--alert-forming",
            "--alert-cooldown-minutes",
            "30",
            "--alert-jsonl",
            "data/alerts/test.jsonl",
        ]
    )

    assert args.alert_sinks == "console,jsonl"
    assert args.alert_forming is True
    assert args.alert_cooldown_minutes == 30
    assert args.alert_jsonl == "data/alerts/test.jsonl"
