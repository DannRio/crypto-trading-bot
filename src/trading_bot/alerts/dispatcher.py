import pandas as pd

from trading_bot.alerts.ledger import AlertLedger
from trading_bot.alerts.models import AlertEvent, AlertKind
from trading_bot.alerts.sinks import AlertSink
from trading_bot.core.enums import SetupStatus


def _utc_now(value=None) -> pd.Timestamp:
    timestamp = (
        pd.Timestamp.now(tz="UTC")
        if value is None
        else pd.Timestamp(value)
    )
    if timestamp.tzinfo is None:
        return timestamp.tz_localize("UTC")
    return timestamp.tz_convert("UTC")


def build_scan_alerts(
    results,
    include_forming: bool = False,
    include_errors: bool = True,
    created_at=None,
) -> list[AlertEvent]:
    timestamp = _utc_now(created_at)
    events: list[AlertEvent] = []

    for result in results:
        if result.error:
            if include_errors:
                events.append(
                    AlertEvent(
                        kind=AlertKind.SCAN_ERROR,
                        created_at=timestamp,
                        symbol=result.symbol,
                        timeframe=result.timeframe,
                        status="ERROR",
                        message="Market scan failed.",
                        payload={
                            "exchange": result.exchange_id,
                            "market_type": result.market_type,
                        },
                    )
                )
            continue

        assessment = result.assessment
        if assessment is None:
            continue

        if assessment.status == SetupStatus.CONFIRMED:
            kind = AlertKind.SETUP_CONFIRMED
        elif (
            assessment.status == SetupStatus.FORMING
            and include_forming
        ):
            kind = AlertKind.SETUP_FORMING
        else:
            continue

        payload = {
            "exchange": result.exchange_id,
            "market_type": result.market_type,
            "score": assessment.score,
            "strategy": assessment.strategy,
            "reason": assessment.reason,
            "multi_timeframe_aligned": (
                result.multi_timeframe_aligned
            ),
        }

        if result.setup is not None:
            payload.update(
                {
                    "entry_low": result.setup.entry_low,
                    "entry_high": result.setup.entry_high,
                    "reference_entry": result.setup.reference_entry,
                    "stop_loss": result.setup.stop_loss,
                    "take_profit_1": result.setup.take_profit_1,
                    "take_profit_2": result.setup.take_profit_2,
                    "position_size": result.setup.position_size,
                    "actual_risk_amount": (
                        result.setup.actual_risk_amount
                    ),
                }
            )

        message = (
            f"{assessment.status.value} "
            f"{assessment.signal.value} {assessment.strategy} setup "
            f"score {assessment.score}/100."
        )
        events.append(
            AlertEvent(
                kind=kind,
                created_at=timestamp,
                symbol=result.symbol,
                timeframe=result.timeframe,
                signal=assessment.signal.value,
                status=assessment.status.value,
                score=assessment.score,
                strategy=assessment.strategy,
                message=message,
                payload=payload,
            )
        )

    return events


class AlertDispatcher:
    def __init__(
        self,
        sinks: list[AlertSink],
        ledger: AlertLedger | None = None,
        cooldown_seconds: int = 3600,
    ):
        if cooldown_seconds < 0:
            raise ValueError("cooldown_seconds cannot be negative.")
        self.sinks = sinks
        self.ledger = ledger
        self.cooldown_seconds = cooldown_seconds

    def dispatch(
        self,
        events: list[AlertEvent],
    ) -> tuple[int, int]:
        sent = 0
        suppressed = 0

        for event in events:
            if (
                self.ledger is not None
                and not self.ledger.should_emit(
                    event.fingerprint,
                    event.created_at,
                    self.cooldown_seconds,
                )
            ):
                suppressed += 1
                continue

            for sink in self.sinks:
                sink.send(event)

            if self.ledger is not None:
                self.ledger.record(
                    event.fingerprint,
                    event.created_at,
                )
            sent += 1

        return sent, suppressed
