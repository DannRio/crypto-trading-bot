from dataclasses import dataclass

from trading_bot.paper.models import PaperOrder, PaperPosition


@dataclass(frozen=True)
class PortfolioRiskConfig:
    max_open_risk_percent: float = 0.03
    max_positions: int = 5
    max_pending_orders: int = 5
    max_symbol_risk_percent: float = 0.015


@dataclass(frozen=True)
class RiskCheck:
    allowed: bool
    reason: str


class PortfolioRiskManager:
    def __init__(
        self,
        config: PortfolioRiskConfig | None = None,
    ):
        self.config = config or PortfolioRiskConfig()

    def check_new_order(
        self,
        account_equity: float,
        symbol: str,
        new_risk: float,
        positions: list[PaperPosition],
        pending_orders: list[PaperOrder],
    ) -> RiskCheck:
        if account_equity <= 0:
            return RiskCheck(False, "Account equity is not positive.")
        if new_risk <= 0:
            return RiskCheck(False, "New order risk is not positive.")

        if len(positions) >= self.config.max_positions:
            return RiskCheck(False, "Maximum open positions reached.")

        if len(pending_orders) >= self.config.max_pending_orders:
            return RiskCheck(False, "Maximum pending orders reached.")

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
            )

        return RiskCheck(True, "Risk checks passed.")
