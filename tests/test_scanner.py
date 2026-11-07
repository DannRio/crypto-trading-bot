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
        ]
    )

    assert args.exchange == "kucoin"
    assert args.market_type == "swap"
    assert args.symbols == "BTC/USDT:USDT"
