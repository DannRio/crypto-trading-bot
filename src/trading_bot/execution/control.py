import os

from trading_bot.execution.manager import OrderManager
from trading_bot.execution.models import (
    ExecutionControlState,
    KillSwitchReport,
    ManagedOrderStatus,
)
from trading_bot.execution.store import OrderStore


LIVE_ENV_VAR = "TRADING_LIVE_EXECUTION"
LIVE_ENV_VALUE = "ENABLED"
ARM_CONFIRMATION = "ARM_LIVE"
RESET_CONFIRMATION = "RESET_HALT"


class LiveExecutionGuard:
    """Fail-closed gate for all new live order submissions."""

    def __init__(
        self,
        store: OrderStore,
        env_var: str = LIVE_ENV_VAR,
        env_value: str = LIVE_ENV_VALUE,
    ):
        self.store = store
        self.env_var = env_var
        self.env_value = env_value

    @property
    def environment_enabled(self) -> bool:
        return (
            os.getenv(self.env_var, "").strip()
            == self.env_value
        )

    def status(self) -> ExecutionControlState:
        return self.store.load_execution_control()

    def assert_can_submit(self) -> None:
        state = self.status()
        if not self.environment_enabled:
            raise RuntimeError(
                f"Live submission is disabled. Set {self.env_var}="
                f"{self.env_value} only on the intended execution host."
            )
        if state.halted:
            raise RuntimeError(
                "Live execution is halted"
                + (
                    f": {state.halt_reason}"
                    if state.halt_reason
                    else "."
                )
            )
        if not state.armed:
            raise RuntimeError(
                "Live execution is not armed in the persistent "
                "execution-control state."
            )

    def arm(
        self,
        *,
        actor: str,
        confirmation: str,
        now=None,
    ) -> ExecutionControlState:
        if confirmation != ARM_CONFIRMATION:
            raise ValueError(
                f"Arming requires confirmation token {ARM_CONFIRMATION}."
            )
        if self.store.load_execution_context() is None:
            raise RuntimeError(
                "Live execution ledger is not bound to an exchange "
                "and market type."
            )
        if not self.environment_enabled:
            raise RuntimeError(
                f"Cannot arm until {self.env_var}={self.env_value}."
            )
        ambiguous = [
            order
            for order in self.store.active_orders()
            if order.status
            in {
                ManagedOrderStatus.UNKNOWN,
                ManagedOrderStatus.SUBMITTING,
                ManagedOrderStatus.CANCEL_PENDING,
            }
        ]
        if ambiguous:
            raise RuntimeError(
                "Cannot arm with unresolved managed orders: "
                + ", ".join(
                    f"{order.order_id}={order.status.value}"
                    for order in ambiguous
                )
            )

        current = self.status()
        if current.halted:
            raise RuntimeError(
                "Execution halt is latched. Reset the halt before arming."
            )
        return self.store.update_execution_control(
            armed=True,
            halted=False,
            halt_reason="",
            updated_by=actor,
            now=now,
        )

    def disarm(
        self,
        *,
        actor: str,
        reason: str = "",
        now=None,
    ) -> ExecutionControlState:
        current = self.status()
        return self.store.update_execution_control(
            armed=False,
            halted=current.halted,
            halt_reason=(
                current.halt_reason
                if current.halted
                else reason.strip()
            ),
            updated_by=actor,
            now=now,
        )

    def trip(
        self,
        *,
        actor: str,
        reason: str,
        now=None,
    ) -> ExecutionControlState:
        reason = reason.strip()
        if not reason:
            raise ValueError(
                "Kill-switch reason is required."
            )
        return self.store.update_execution_control(
            armed=False,
            halted=True,
            halt_reason=reason,
            updated_by=actor,
            now=now,
        )

    def reset_halt(
        self,
        *,
        actor: str,
        confirmation: str,
        now=None,
    ) -> ExecutionControlState:
        if confirmation != RESET_CONFIRMATION:
            raise ValueError(
                "Reset requires confirmation token "
                f"{RESET_CONFIRMATION}."
            )
        return self.store.update_execution_control(
            armed=False,
            halted=False,
            halt_reason="",
            updated_by=actor,
            now=now,
        )


class KillSwitchService:
    """Latches a halt first, then attempts to cancel managed open orders."""

    def __init__(
        self,
        store: OrderStore,
        manager: OrderManager,
        guard: LiveExecutionGuard,
    ):
        self.store = store
        self.manager = manager
        self.guard = guard

    def trip(
        self,
        *,
        actor: str,
        reason: str,
        now=None,
        cancel_open_orders: bool = True,
    ) -> KillSwitchReport:
        self.guard.trip(
            actor=actor,
            reason=reason,
            now=now,
        )

        cancelled = 0
        skipped = 0
        errors: list[str] = []

        if cancel_open_orders:
            for order in self.store.active_orders():
                if order.status not in {
                    ManagedOrderStatus.SUBMITTED,
                    ManagedOrderStatus.PARTIALLY_FILLED,
                }:
                    skipped += 1
                    continue
                try:
                    result = self.manager.cancel(
                        order.order_id,
                        now=now,
                    )
                    if result.status == ManagedOrderStatus.CANCELLED:
                        cancelled += 1
                    else:
                        skipped += 1
                except Exception as exc:
                    errors.append(
                        f"{order.order_id}: "
                        f"{type(exc).__name__}: {exc}"
                    )

        return KillSwitchReport(
            halted=True,
            cancelled_orders=cancelled,
            skipped_orders=skipped,
            errors=len(errors),
            error_messages=tuple(errors),
        )
