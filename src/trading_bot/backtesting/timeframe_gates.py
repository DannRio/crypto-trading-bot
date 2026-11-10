"""Chronologically correct historical scanner gates.

Candle timestamps denote candle OPENS. At the close of a primary candle,
only secondary candles whose CLOSE time has passed may influence a decision.
"""
from dataclasses import replace

import pandas as pd

from trading_bot.analysis import add_indicators, analyze_market_structure
from trading_bot.data import timeframe_to_milliseconds, validate_ohlcv
from trading_bot.scanner.multi_timeframe import (
    TimeframeConfirmation,
    apply_confirmation_gate,
    apply_entry_trigger_gate,
    evaluate_entry_trigger,
    regime_alignment,
    validate_confirmation_timeframes,
    validate_entry_timeframe,
)


class HistoricalTimeframeGates:
    def __init__(
        self,
        primary_timeframe: str,
        secondary_data: dict[str, pd.DataFrame],
        confirmation_timeframes: tuple[str, ...] = (),
        entry_timeframe: str | None = None,
    ):
        self.primary_timeframe = primary_timeframe
        self.confirmation_timeframes = validate_confirmation_timeframes(
            primary_timeframe, confirmation_timeframes
        )
        self.entry_timeframe = validate_entry_timeframe(
            primary_timeframe, entry_timeframe
        )
        self.prepared = {}
        requested = set(self.confirmation_timeframes)
        if self.entry_timeframe:
            requested.add(self.entry_timeframe)
        missing = requested.difference(secondary_data)
        if missing:
            raise ValueError("Missing historical timeframe data: " + ", ".join(sorted(missing)))
        for timeframe in requested:
            frame = secondary_data[timeframe].copy()
            frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
            frame = frame.sort_values("timestamp").reset_index(drop=True)
            if frame["timestamp"].duplicated().any():
                raise ValueError(f"Duplicate candles in {timeframe} history")
            frame = add_indicators(frame)
            self.prepared[timeframe] = (
                frame,
                frame["timestamp"] + pd.to_timedelta(
                    timeframe_to_milliseconds(timeframe), unit="ms"
                ),
            )

    def _closed_snapshot(self, timeframe: str, decision_close: pd.Timestamp):
        frame, close_times = self.prepared[timeframe]
        n = int(close_times.searchsorted(decision_close, side="right"))
        # Keep all past history to maintain consistent EMA warm-up.
        if n < 220:
            return None, None
        closed = frame.iloc[:n]
        quality = validate_ohlcv(closed.tail(500), timeframe=timeframe, min_rows=220)
        if not quality.valid or quality.missing_intervals:
            return None, quality
        return closed, quality

    def apply(self, decision, signal_open_time):
        """Return a new decision with scanner-identical gates, no future candles."""
        if not self.confirmation_timeframes and not self.entry_timeframe:
            return decision
        close_time = pd.Timestamp(signal_open_time)
        if close_time.tzinfo is None:
            close_time = close_time.tz_localize("UTC")
        else:
            close_time = close_time.tz_convert("UTC")
        close_time += pd.to_timedelta(
            timeframe_to_milliseconds(self.primary_timeframe), unit="ms"
        )

        confirmations = []
        for timeframe in self.confirmation_timeframes:
            closed, quality = self._closed_snapshot(timeframe, close_time)
            if closed is None:
                confirmations.append(TimeframeConfirmation(
                    timeframe=timeframe, regime=None, aligned=None,
                    data_quality=quality, error="Missing or invalid closed historical candles.",
                ))
                continue
            structure = analyze_market_structure(closed)
            confirmations.append(TimeframeConfirmation(
                timeframe=timeframe, regime=structure.regime,
                aligned=regime_alignment(decision.assessment.signal, structure.regime),
                data_quality=quality,
            ))

        assessment, _ = apply_confirmation_gate(
            decision.assessment, tuple(confirmations)
        )
        if self.entry_timeframe and assessment.signal.value != "NO_TRADE":
            closed, quality = self._closed_snapshot(self.entry_timeframe, close_time)
            if closed is None:
                # Missing LTF bars must never be interpreted as confirmation.
                from trading_bot.scanner.multi_timeframe import EntryTrigger
                trigger = EntryTrigger(
                    timeframe=self.entry_timeframe, confirmed=None, regime=None,
                    rsi=None, close=None, ema_50=None, data_quality=quality,
                    reason="Lower-timeframe historical data unavailable.",
                    error="Missing or invalid closed historical candles.",
                )
            else:
                structure = analyze_market_structure(closed)
                trigger = evaluate_entry_trigger(
                    signal=assessment.signal,
                    timeframe=self.entry_timeframe,
                    data=closed,
                    regime=structure.regime,
                    data_quality=quality,
                )
            assessment = apply_entry_trigger_gate(assessment, trigger)

        from trading_bot.core.enums import SetupStatus
        return replace(
            decision,
            assessment=assessment,
            setup=decision.setup if assessment.status == SetupStatus.CONFIRMED else None,
        )
