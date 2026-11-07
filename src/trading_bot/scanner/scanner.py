from collections.abc import Callable

from trading_bot.analysis import add_indicators
from trading_bot.core.enums import SetupStatus
from trading_bot.data import get_ohlcv, validate_ohlcv
from trading_bot.decision import DecisionEngine
from trading_bot.scanner.models import ScanResult


class MarketScanner:
    def __init__(
        self,
        exchange_id: str = "kraken",
        market_type: str = "spot",
        timeframe: str = "1h",
        candle_limit: int = 500,
        account_equity: float = 10_000,
        risk_percent: float = 0.01,
        min_confirmed_score: int = 70,
        data_fetcher: Callable = get_ohlcv,
    ):
        self.exchange_id = exchange_id
        self.market_type = market_type
        self.timeframe = timeframe
        self.candle_limit = candle_limit
        self.account_equity = account_equity
        self.risk_percent = risk_percent
        self.min_confirmed_score = min_confirmed_score
        self.data_fetcher = data_fetcher

    def scan_symbol(self, symbol: str) -> ScanResult:
        try:
            df = self.data_fetcher(
                symbol=symbol,
                timeframe=self.timeframe,
                limit=self.candle_limit,
                exchange_id=self.exchange_id,
                market_type=self.market_type,
            )
            if df.empty or len(df) < 2:
                return ScanResult(
                    self.exchange_id,
                    self.market_type,
                    symbol,
                    self.timeframe,
                    None,
                    None,
                    None,
                    None,
                    "No usable market data received.",
                )

            closed = df.iloc[:-1].copy()
            quality = validate_ohlcv(
                closed,
                timeframe=self.timeframe,
                min_rows=220,
            )
            if not quality.valid:
                return ScanResult(
                    self.exchange_id,
                    self.market_type,
                    symbol,
                    self.timeframe,
                    None,
                    None,
                    None,
                    quality,
                    "; ".join(quality.issues),
                )

            data = add_indicators(closed)
            decision = DecisionEngine(
                account_equity=self.account_equity,
                risk_percent=self.risk_percent,
                min_confirmed_score=self.min_confirmed_score,
            ).evaluate(data)

            return ScanResult(
                self.exchange_id,
                self.market_type,
                symbol,
                self.timeframe,
                decision.assessment,
                decision.structure,
                decision.setup,
                quality,
            )
        except Exception as exc:
            return ScanResult(
                self.exchange_id,
                self.market_type,
                symbol,
                self.timeframe,
                None,
                None,
                None,
                None,
                f"{type(exc).__name__}: {exc}",
            )

    def scan(self, symbols: list[str]) -> list[ScanResult]:
        results = [self.scan_symbol(symbol) for symbol in symbols]
        status_rank = {
            SetupStatus.CONFIRMED: 0,
            SetupStatus.FORMING: 1,
            SetupStatus.NO_TRADE: 2,
        }
        return sorted(
            results,
            key=lambda result: (
                status_rank.get(
                    result.assessment.status
                    if result.assessment
                    else SetupStatus.NO_TRADE,
                    3,
                ),
                -result.score,
                result.symbol,
            ),
        )
