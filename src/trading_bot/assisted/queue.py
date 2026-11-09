from hashlib import sha256

import pandas as pd

from trading_bot.assisted.models import (
    IntentStatus,
    QueueReport,
    TradeIntent,
)
from trading_bot.assisted.store import (
    IntentStore,
    new_intent_id,
    plan_hash,
)
from trading_bot.core.enums import Signal, SetupStatus
from trading_bot.data import timeframe_to_milliseconds


def _timestamp(value) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        return timestamp.tz_localize("UTC")
    return timestamp.tz_convert("UTC")


def _fingerprint(result) -> str:
    assessment = result.assessment
    raw = "|".join(
        [
            result.exchange_id,
            result.market_type,
            result.symbol,
            result.timeframe,
            assessment.strategy,
            assessment.signal.value,
            _timestamp(result.signal_time).isoformat(),
        ]
    )
    return sha256(raw.encode("utf-8")).hexdigest()


def intent_from_scan_result(
    result,
    expires_after_bars: int = 3,
    created_at=None,
) -> TradeIntent:
    if expires_after_bars <= 0:
        raise ValueError("expires_after_bars must be positive.")
    if result.error:
        raise ValueError("Cannot queue a scan result with an error.")
    if result.assessment is None or result.setup is None:
        raise ValueError("Scan result has no executable trade setup.")
    if (
        result.assessment.status
        != SetupStatus.CONFIRMED
    ):
        raise ValueError(
            "Only CONFIRMED scan results can become trade intents."
        )
    if result.signal_time is None:
        raise ValueError(
            "Trade intent requires the scanner signal timestamp."
        )

    signal_time = _timestamp(result.signal_time)
    now = (
        pd.Timestamp.now(tz="UTC")
        if created_at is None
        else _timestamp(created_at)
    )
    expiry_ms = (
        timeframe_to_milliseconds(result.timeframe)
        * expires_after_bars
    )
    expires_at = signal_time + pd.Timedelta(
        milliseconds=expiry_ms
    )

    setup = result.setup
    confirmations = []
    for item in getattr(result, "confirmations", ()):
        confirmations.append(
            {
                "timeframe": item.timeframe,
                "regime": (
                    item.regime.value
                    if item.regime is not None
                    else None
                ),
                "aligned": item.aligned,
                "error": item.error,
            }
        )

    plan = {
        "exchange_id": result.exchange_id,
        "market_type": result.market_type,
        "symbol": result.symbol,
        "timeframe": result.timeframe,
        "strategy": result.assessment.strategy,
        "signal": result.assessment.signal.value,
        "score": result.assessment.score,
        "signal_time": signal_time.isoformat(),
        "expires_at": expires_at.isoformat(),
        "regime": (
            result.structure.regime.value
            if result.structure is not None
            else None
        ),
        "multi_timeframe_aligned": getattr(
            result,
            "multi_timeframe_aligned",
            None,
        ),
        "confirmations": confirmations,
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
        "reasons": list(result.assessment.reasons),
    }

    return TradeIntent(
        intent_id=new_intent_id(),
        fingerprint=_fingerprint(result),
        plan_hash=plan_hash(plan),
        created_at=now,
        signal_time=signal_time,
        expires_at=expires_at,
        exchange_id=result.exchange_id,
        market_type=result.market_type,
        symbol=result.symbol,
        timeframe=result.timeframe,
        strategy=result.assessment.strategy,
        signal=result.assessment.signal.value,
        score=result.assessment.score,
        status=IntentStatus.PENDING_APPROVAL,
        plan=plan,
    )


def queue_scan_results(
    store: IntentStore,
    results,
    expires_after_bars: int = 3,
    now=None,
) -> QueueReport:
    timestamp = (
        pd.Timestamp.now(tz="UTC")
        if now is None
        else _timestamp(now)
    )
    queued = 0
    duplicates = 0
    stale = 0
    unsupported = 0
    ignored = 0

    store.expire_stale(timestamp)

    for result in results:
        if (
            result.error
            or result.assessment is None
            or result.setup is None
            or result.assessment.status
            != SetupStatus.CONFIRMED
        ):
            ignored += 1
            continue

        if (
            result.market_type == "spot"
            and result.assessment.signal == Signal.SHORT
        ):
            unsupported += 1
            continue

        intent = intent_from_scan_result(
            result,
            expires_after_bars=expires_after_bars,
            created_at=timestamp,
        )
        if intent.expires_at <= timestamp:
            stale += 1
            continue

        if store.queue(intent):
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
