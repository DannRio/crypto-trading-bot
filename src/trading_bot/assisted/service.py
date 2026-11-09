from hashlib import sha256

import pandas as pd

from trading_bot.assisted.broker import (
    BrokerAdapter,
    DryRunBrokerAdapter,
)
from trading_bot.assisted.models import (
    ExecutionReceipt,
    IntentStatus,
    QueueReport,
    TradeIntent,
)
from trading_bot.assisted.store import (
    IntentStore,
    canonical_json,
    new_intent_id,
    plan_hash,
)
from trading_bot.core.enums import SetupStatus
from trading_bot.data import timeframe_to_milliseconds


def _utc_timestamp(value) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        return timestamp.tz_localize("UTC")
    return timestamp.tz_convert("UTC")


def _intent_plan(result) -> dict:
    setup = result.setup
    assessment = result.assessment
    if setup is None or assessment is None:
        raise ValueError("Confirmed scan result has no trade plan.")

    confirmations = [
        {
            "timeframe": item.timeframe,
            "regime": (
                item.regime.value
                if item.regime is not None
                else None
            ),
            "aligned": item.aligned,
        }
        for item in result.confirmations
    ]
    trigger = result.entry_trigger
    entry_trigger = None
    if trigger is not None:
        entry_trigger = {
            "timeframe": trigger.timeframe,
            "confirmed": trigger.confirmed,
            "regime": (
                trigger.regime.value
                if trigger.regime is not None
                else None
            ),
            "rsi": trigger.rsi,
            "close": trigger.close,
            "ema_50": trigger.ema_50,
            "reason": trigger.reason,
        }

    return {
        "reference_entry": setup.reference_entry,
        "entry_low": setup.entry_low,
        "entry_high": setup.entry_high,
        "stop_loss": setup.stop_loss,
        "take_profit_1": setup.take_profit_1,
        "take_profit_2": setup.take_profit_2,
        "reward_to_risk_1": setup.reward_to_risk_1,
        "reward_to_risk_2": setup.reward_to_risk_2,
        "position_size": setup.position_size,
        "notional_value": setup.notional_value,
        "target_risk_amount": setup.target_risk_amount,
        "actual_risk_amount": setup.actual_risk_amount,
        "reasons": list(setup.reasons),
        "primary_regime": (
            result.structure.regime.value
            if result.structure is not None
            else None
        ),
        "higher_timeframes": confirmations,
        "entry_trigger": entry_trigger,
    }


def _fingerprint(payload: dict) -> str:
    return sha256(
        canonical_json(payload).encode("utf-8")
    ).hexdigest()


class AssistedTradingService:
    """Local approval workflow. Default broker is dry-run only."""

    def __init__(
        self,
        store: IntentStore,
        broker: BrokerAdapter | None = None,
        expiry_bars: float = 1.0,
    ):
        if expiry_bars <= 0:
            raise ValueError("expiry_bars must be positive.")
        self.store = store
        self.broker = broker or DryRunBrokerAdapter()
        self.expiry_bars = expiry_bars

    def build_intent(
        self,
        result,
        now=None,
    ) -> TradeIntent:
        if result.error:
            raise ValueError("Cannot stage a scan result with an error.")
        if (
            result.assessment is None
            or result.assessment.status != SetupStatus.CONFIRMED
            or result.setup is None
        ):
            raise ValueError(
                "Only CONFIRMED scan results with a trade plan "
                "can become assisted intents."
            )
        if result.signal_time is None:
            raise ValueError(
                "Scan result has no closed-candle signal timestamp."
            )

        signal_time = _utc_timestamp(result.signal_time)
        created_at = (
            pd.Timestamp.now(tz="UTC")
            if now is None
            else _utc_timestamp(now)
        )
        timeframe_ms = timeframe_to_milliseconds(
            result.timeframe
        )
        expires_at = signal_time + pd.Timedelta(
            milliseconds=(
                timeframe_ms * self.expiry_bars
            )
        )
        if created_at >= expires_at:
            raise ValueError(
                "Setup is stale; its assisted approval window has expired."
            )

        plan = _intent_plan(result)
        immutable_hash = plan_hash(plan)
        assessment = result.assessment

        fingerprint_payload = {
            "exchange_id": result.exchange_id,
            "market_type": result.market_type,
            "symbol": result.symbol,
            "timeframe": result.timeframe,
            "strategy": assessment.strategy,
            "signal": assessment.signal.value,
            "signal_time": signal_time.isoformat(),
            "plan_hash": immutable_hash,
        }

        intent = TradeIntent(
            intent_id=new_intent_id(),
            fingerprint=_fingerprint(fingerprint_payload),
            plan_hash=immutable_hash,
            created_at=created_at,
            signal_time=signal_time,
            expires_at=expires_at,
            exchange_id=result.exchange_id,
            market_type=result.market_type,
            symbol=result.symbol,
            timeframe=result.timeframe,
            strategy=assessment.strategy,
            signal=assessment.signal.value,
            score=assessment.score,
            status=IntentStatus.PENDING_APPROVAL,
            plan=plan,
        )

        self.broker.validate_intent(intent)
        return intent

    def queue_scan_results(
        self,
        results,
        now=None,
    ) -> QueueReport:
        queued = 0
        duplicates = 0
        stale = 0
        unsupported = 0
        ignored = 0

        for result in results:
            if (
                result.error
                or result.assessment is None
                or result.assessment.status != SetupStatus.CONFIRMED
                or result.setup is None
            ):
                ignored += 1
                continue

            try:
                intent = self.build_intent(
                    result,
                    now=now,
                )
            except ValueError as exc:
                message = str(exc).lower()
                if "stale" in message or "expired" in message:
                    stale += 1
                elif (
                    "does not support" in message
                    or "not executable" in message
                ):
                    unsupported += 1
                else:
                    raise
                continue

            if self.store.queue(intent):
                queued += 1
            else:
                duplicates += 1

        return QueueReport(
            queued=queued,
            duplicates=duplicates,
            stale=stale,
            unsupported=unsupported,
            ignored=ignored,
        )

    def pending(
        self,
        limit: int = 100,
        now=None,
    ) -> list[TradeIntent]:
        return self.store.list(
            statuses=(IntentStatus.PENDING_APPROVAL,),
            limit=limit,
            expire_at=now,
        )

    def approve(
        self,
        intent_id: str,
        actor: str,
        note: str = "",
        execute: bool = True,
        decided_at=None,
    ) -> tuple[TradeIntent, ExecutionReceipt | None]:
        approved = self.store.approve(
            intent_id,
            actor=actor,
            note=note,
            decided_at=decided_at,
        )
        if not execute:
            return approved, None

        self.broker.validate_intent(approved)
        receipt = self.broker.execute(approved)
        executed = self.store.record_execution(receipt)
        return executed, receipt

    def reject(
        self,
        intent_id: str,
        actor: str,
        note: str = "",
        decided_at=None,
    ) -> TradeIntent:
        return self.store.reject(
            intent_id,
            actor=actor,
            note=note,
            decided_at=decided_at,
        )
