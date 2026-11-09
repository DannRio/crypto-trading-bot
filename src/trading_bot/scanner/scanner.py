from collections.abc import Callable

from trading_bot.analysis import (
    add_indicators,
    analyze_market_structure,
)
from trading_bot.core.enums import SetupStatus
from trading_bot.data import get_ohlcv, validate_ohlcv
from trading_bot.decision import DecisionEngine
from trading_bot.scanner.models import ScanResult
from trading_bot.scanner.multi_timeframe import (
    EntryTrigger,
    TimeframeConfirmation,
    apply_confirmation_gate,
    apply_entry_trigger_gate,
    evaluate_entry_trigger,
    regime_alignment,
    validate_confirmation_timeframes,
    validate_entry_timeframe,
)
from trading_bot.strategies import (
    DEFAULT_STRATEGIES,
    normalize_strategy_names,
)


class MarketScanner:
    def __init__(
        self,
        exchange_id: str = "kraken",
        market_type: str = "spot",
        timeframe: str = "1h",
        confirmation_timeframes: tuple[str, ...] = (),
        entry_timeframe: str | None = None,
        candle_limit: int = 500,
        account_equity: float = 10_000,
        risk_percent: float = 0.01,
        min_confirmed_score: int = 70,
        strategy_names: tuple[str, ...] = DEFAULT_STRATEGIES,
        data_fetcher: Callable = get_ohlcv,
    ):
        self.exchange_id = exchange_id
        self.market_type = market_type
        self.timeframe = timeframe
        self.confirmation_timeframes = (
            validate_confirmation_timeframes(
                timeframe,
                confirmation_timeframes,
            )
        )
        self.entry_timeframe = validate_entry_timeframe(
            timeframe,
            entry_timeframe,
        )
        self.candle_limit = candle_limit
        self.account_equity = account_equity
        self.risk_percent = risk_percent
        self.min_confirmed_score = min_confirmed_score
        self.strategy_names = normalize_strategy_names(
            strategy_names
        )
        self.data_fetcher = data_fetcher

    def _fetch_closed(
        self,
        symbol: str,
        timeframe: str,
    ):
        df = self.data_fetcher(
            symbol=symbol,
            timeframe=timeframe,
            limit=self.candle_limit,
            exchange_id=self.exchange_id,
            market_type=self.market_type,
        )
        if df.empty or len(df) < 2:
            return None, None, "No usable market data received."

        closed = df.iloc[:-1].copy()
        quality = validate_ohlcv(
            closed,
            timeframe=timeframe,
            min_rows=220,
        )
        if not quality.valid:
            return closed, quality, "; ".join(quality.issues)

        return closed, quality, None

    def _confirmation_context(
        self,
        symbol: str,
        signal,
    ) -> tuple[TimeframeConfirmation, ...]:
        confirmations: list[TimeframeConfirmation] = []

        for timeframe in self.confirmation_timeframes:
            try:
                closed, quality, error = self._fetch_closed(
                    symbol,
                    timeframe,
                )
                if error or closed is None:
                    confirmations.append(
                        TimeframeConfirmation(
                            timeframe=timeframe,
                            regime=None,
                            aligned=None,
                            data_quality=quality,
                            error=error,
                        )
                    )
                    continue

                data = add_indicators(closed)
                structure = analyze_market_structure(data)
                confirmations.append(
                    TimeframeConfirmation(
                        timeframe=timeframe,
                        regime=structure.regime,
                        aligned=regime_alignment(
                            signal,
                            structure.regime,
                        ),
                        data_quality=quality,
                    )
                )
            except Exception as exc:
                confirmations.append(
                    TimeframeConfirmation(
                        timeframe=timeframe,
                        regime=None,
                        aligned=None,
                        data_quality=None,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )

        return tuple(confirmations)

    def _entry_trigger_context(
        self,
        symbol: str,
        signal,
    ) -> EntryTrigger | None:
        if self.entry_timeframe is None:
            return None

        try:
            closed, quality, error = self._fetch_closed(
                symbol,
                self.entry_timeframe,
            )
            if error or closed is None:
                return EntryTrigger(
                    timeframe=self.entry_timeframe,
                    confirmed=None,
                    regime=None,
                    rsi=None,
                    close=None,
                    ema_50=None,
                    data_quality=quality,
                    reason="Lower-timeframe entry trigger is unavailable.",
                    error=error,
                )

            data = add_indicators(closed)
            structure = analyze_market_structure(data)
            return evaluate_entry_trigger(
                signal=signal,
                timeframe=self.entry_timeframe,
                data=data,
                regime=structure.regime,
                data_quality=quality,
            )
        except Exception as exc:
            return EntryTrigger(
                timeframe=self.entry_timeframe,
                confirmed=None,
                regime=None,
                rsi=None,
                close=None,
                ema_50=None,
                data_quality=None,
                reason="Lower-timeframe entry trigger is unavailable.",
                error=f"{type(exc).__name__}: {exc}",
            )

    def scan_symbol(self, symbol: str) -> ScanResult:
        try:
            closed, quality, error = self._fetch_closed(
                symbol,
                self.timeframe,
            )
            if error or closed is None:
                return ScanResult(
                    self.exchange_id,
                    self.market_type,
                    symbol,
                    self.timeframe,
                    None,
                    None,
                    None,
                    quality,
                    error,
                    confirmation_timeframes=self.confirmation_timeframes,
                )

            data = add_indicators(closed)
            decision = DecisionEngine(
                account_equity=self.account_equity,
                risk_percent=self.risk_percent,
                min_confirmed_score=self.min_confirmed_score,
                strategy_names=self.strategy_names,
            ).evaluate(data)

            confirmations = self._confirmation_context(
                symbol,
                decision.assessment.signal,
            )
            final_assessment, aligned = apply_confirmation_gate(
                decision.assessment,
                confirmations,
            )
            entry_trigger = self._entry_trigger_context(
                symbol,
                final_assessment.signal,
            )
            final_assessment = apply_entry_trigger_gate(
                final_assessment,
                entry_trigger,
            )

            setup = decision.setup
            if final_assessment.status != SetupStatus.CONFIRMED:
                setup = None

            return ScanResult(
                self.exchange_id,
                self.market_type,
                symbol,
                self.timeframe,
                final_assessment,
                decision.structure,
                setup,
                quality,
                None,
                primary_assessment=decision.assessment,
                confirmation_timeframes=self.confirmation_timeframes,
                confirmations=confirmations,
                multi_timeframe_aligned=aligned,
                candidates=decision.candidates,
                entry_timeframe=self.entry_timeframe,
                entry_trigger=entry_trigger,
                signal_time=data.iloc[-1]["timestamp"],
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
                confirmation_timeframes=self.confirmation_timeframes,
                entry_timeframe=self.entry_timeframe,
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
