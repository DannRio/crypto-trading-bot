from dataclasses import dataclass

import pandas as pd

from trading_bot.core.enums import Signal
from trading_bot.paper.models import PaperOrder, PaperPosition


@dataclass(frozen=True)
class PortfolioRiskConfig:
    max_open_risk_percent: float = 0.03
    max_positions: int = 5
    max_pending_orders: int = 5
    max_symbol_risk_percent: float = 0.015
    max_gross_notional_percent: float = 2.0
    max_correlated_risk_percent: float = 0.02
    correlation_threshold: float = 0.75
    require_correlation_data: bool = True
    max_daily_loss_percent: float = 0.02
    max_weekly_loss_percent: float = 0.05
    max_drawdown_percent: float = 0.10

    def __post_init__(self):
        fraction_fields = {
            "max_open_risk_percent": self.max_open_risk_percent,
            "max_symbol_risk_percent": self.max_symbol_risk_percent,
            "max_correlated_risk_percent": (
                self.max_correlated_risk_percent
            ),
            "correlation_threshold": self.correlation_threshold,
            "max_daily_loss_percent": self.max_daily_loss_percent,
            "max_weekly_loss_percent": self.max_weekly_loss_percent,
            "max_drawdown_percent": self.max_drawdown_percent,
        }
        invalid = [
            name
            for name, value in fraction_fields.items()
            if not 0 < value <= 1
        ]
        if invalid:
            raise ValueError(
                "Risk percentages/thresholds must be between 0 and 1: "
                + ", ".join(invalid)
            )
        if self.max_positions <= 0:
            raise ValueError("max_positions must be positive.")
        if self.max_pending_orders <= 0:
            raise ValueError("max_pending_orders must be positive.")
        if self.max_gross_notional_percent <= 0:
            raise ValueError(
                "max_gross_notional_percent must be positive."
            )


@dataclass(frozen=True)
class RiskCheck:
    allowed: bool
    reason: str
    code: str = "OK"


@dataclass(frozen=True)
class PortfolioRiskState:
    peak_equity: float
    day_key: str
    day_start_equity: float
    week_key: str
    week_start_equity: float
    halted: bool = False
    halt_reason: str = ""
    halted_at: pd.Timestamp | None = None


@dataclass(frozen=True)
class RiskStateUpdate:
    state: PortfolioRiskState
    triggered: bool
    reason: str
    daily_loss_pct: float
    weekly_loss_pct: float
    drawdown_pct: float


def _utc_timestamp(value) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        return timestamp.tz_localize("UTC")
    return timestamp.tz_convert("UTC")


def _day_key(timestamp: pd.Timestamp) -> str:
    return timestamp.date().isoformat()


def _week_key(timestamp: pd.Timestamp) -> str:
    iso = timestamp.isocalendar()
    return f"{int(iso.year):04d}-W{int(iso.week):02d}"


def _loss_fraction(
    baseline: float,
    equity: float,
) -> float:
    if baseline <= 0:
        return 1.0
    return max(0.0, (baseline - equity) / baseline)


def _signal_direction(signal: Signal) -> int:
    if signal == Signal.LONG:
        return 1
    if signal == Signal.SHORT:
        return -1
    return 0


class PortfolioRiskManager:
    def __init__(
        self,
        config: PortfolioRiskConfig | None = None,
    ):
        self.config = config or PortfolioRiskConfig()

    def initialize_state(
        self,
        account_equity: float,
        timestamp,
    ) -> PortfolioRiskState:
        if account_equity <= 0:
            raise ValueError(
                "Account equity must be positive to initialize risk state."
            )
        now = _utc_timestamp(timestamp)
        return PortfolioRiskState(
            peak_equity=account_equity,
            day_key=_day_key(now),
            day_start_equity=account_equity,
            week_key=_week_key(now),
            week_start_equity=account_equity,
        )

    def reset_state(
        self,
        account_equity: float,
        timestamp,
    ) -> PortfolioRiskState:
        return self.initialize_state(
            account_equity,
            timestamp,
        )

    def update_state(
        self,
        state: PortfolioRiskState | None,
        account_equity: float,
        timestamp,
    ) -> RiskStateUpdate:
        now = _utc_timestamp(timestamp)
        if state is None:
            state = self.initialize_state(
                account_equity,
                now,
            )

        day_key = _day_key(now)
        week_key = _week_key(now)
        day_start = (
            account_equity
            if day_key != state.day_key
            else state.day_start_equity
        )
        week_start = (
            account_equity
            if week_key != state.week_key
            else state.week_start_equity
        )
        peak = max(state.peak_equity, account_equity)

        daily_loss = _loss_fraction(
            day_start,
            account_equity,
        )
        weekly_loss = _loss_fraction(
            week_start,
            account_equity,
        )
        drawdown = _loss_fraction(
            peak,
            account_equity,
        )

        halted = state.halted
        halt_reason = state.halt_reason
        halted_at = state.halted_at
        triggered = False

        if not halted:
            if account_equity <= 0:
                halted = True
                halt_reason = "Account equity is not positive."
            elif (
                daily_loss
                >= self.config.max_daily_loss_percent
            ):
                halted = True
                halt_reason = (
                    "Daily loss limit breached: "
                    f"{daily_loss:.2%} >= "
                    f"{self.config.max_daily_loss_percent:.2%}."
                )
            elif (
                weekly_loss
                >= self.config.max_weekly_loss_percent
            ):
                halted = True
                halt_reason = (
                    "Weekly loss limit breached: "
                    f"{weekly_loss:.2%} >= "
                    f"{self.config.max_weekly_loss_percent:.2%}."
                )
            elif drawdown >= self.config.max_drawdown_percent:
                halted = True
                halt_reason = (
                    "Portfolio drawdown limit breached: "
                    f"{drawdown:.2%} >= "
                    f"{self.config.max_drawdown_percent:.2%}."
                )

            if halted:
                triggered = True
                halted_at = now

        updated = PortfolioRiskState(
            peak_equity=peak,
            day_key=day_key,
            day_start_equity=day_start,
            week_key=week_key,
            week_start_equity=week_start,
            halted=halted,
            halt_reason=halt_reason,
            halted_at=halted_at,
        )
        return RiskStateUpdate(
            state=updated,
            triggered=triggered,
            reason=halt_reason,
            daily_loss_pct=daily_loss,
            weekly_loss_pct=weekly_loss,
            drawdown_pct=drawdown,
        )

    def check_new_order(
        self,
        account_equity: float,
        symbol: str,
        signal: Signal,
        new_risk: float,
        new_notional: float,
        positions: list[PaperPosition],
        pending_orders: list[PaperOrder],
        correlations: dict[str, float] | None = None,
        risk_halted: bool = False,
    ) -> RiskCheck:
        if risk_halted:
            return RiskCheck(
                False,
                "Portfolio risk halt is active.",
                "RISK_HALTED",
            )
        if account_equity <= 0:
            return RiskCheck(
                False,
                "Account equity is not positive.",
                "INVALID_EQUITY",
            )
        if new_risk <= 0:
            return RiskCheck(
                False,
                "New order risk is not positive.",
                "INVALID_RISK",
            )
        if new_notional <= 0:
            return RiskCheck(
                False,
                "New order notional is not positive.",
                "INVALID_NOTIONAL",
            )

        if len(positions) >= self.config.max_positions:
            return RiskCheck(
                False,
                "Maximum open positions reached.",
                "MAX_POSITIONS",
            )

        if len(pending_orders) >= self.config.max_pending_orders:
            return RiskCheck(
                False,
                "Maximum pending orders reached.",
                "MAX_PENDING",
            )

        open_risk = sum(
            position.risk_amount
            for position in positions
        )
        pending_risk = sum(
            order.planned_risk
            for order in pending_orders
        )
        total_after = open_risk + pending_risk + new_risk
        max_total = (
            account_equity
            * self.config.max_open_risk_percent
        )

        if total_after > max_total:
            return RiskCheck(
                False,
                "Maximum portfolio open-risk limit would be exceeded.",
                "MAX_OPEN_RISK",
            )

        symbol_risk = sum(
            position.risk_amount
            for position in positions
            if position.symbol == symbol
        ) + sum(
            order.planned_risk
            for order in pending_orders
            if order.symbol == symbol
        ) + new_risk

        max_symbol = (
            account_equity
            * self.config.max_symbol_risk_percent
        )
        if symbol_risk > max_symbol:
            return RiskCheck(
                False,
                "Maximum per-symbol risk limit would be exceeded.",
                "MAX_SYMBOL_RISK",
            )

        gross_notional = sum(
            abs(position.entry_price * position.position_size)
            for position in positions
        ) + sum(
            abs(order.reference_entry * order.position_size)
            for order in pending_orders
        ) + abs(new_notional)

        max_gross = (
            account_equity
            * self.config.max_gross_notional_percent
        )
        if gross_notional > max_gross:
            return RiskCheck(
                False,
                "Maximum portfolio gross-notional limit would be exceeded.",
                "MAX_GROSS_NOTIONAL",
            )

        exposures = [
            (
                position.symbol,
                position.signal,
                position.risk_amount,
            )
            for position in positions
        ] + [
            (
                order.symbol,
                order.signal,
                order.planned_risk,
            )
            for order in pending_orders
        ]

        if exposures:
            correlations = correlations or {}
            missing = sorted(
                {
                    existing_symbol
                    for (
                        existing_symbol,
                        _,
                        _,
                    ) in exposures
                    if existing_symbol != symbol
                    and existing_symbol not in correlations
                }
            )
            if (
                missing
                and self.config.require_correlation_data
            ):
                return RiskCheck(
                    False,
                    "Correlation data unavailable for: "
                    + ", ".join(missing),
                    "CORRELATION_DATA_MISSING",
                )

            candidate_direction = _signal_direction(signal)
            correlated_risk = new_risk

            for (
                existing_symbol,
                existing_signal,
                existing_risk,
            ) in exposures:
                if existing_symbol == symbol:
                    correlation = 1.0
                else:
                    correlation = correlations.get(
                        existing_symbol
                    )
                    if correlation is None:
                        continue

                effective_correlation = (
                    correlation
                    * candidate_direction
                    * _signal_direction(existing_signal)
                )
                if (
                    effective_correlation
                    >= self.config.correlation_threshold
                ):
                    correlated_risk += existing_risk

            max_correlated = (
                account_equity
                * self.config.max_correlated_risk_percent
            )
            if correlated_risk > max_correlated:
                return RiskCheck(
                    False,
                    "Maximum correlated-risk limit would be exceeded.",
                    "MAX_CORRELATED_RISK",
                )

        return RiskCheck(
            True,
            "Risk checks passed.",
            "OK",
        )
