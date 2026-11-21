from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping

import numpy as np
import pandas as pd

from .config import RegimeBotConfig
from .indicators import build_features
from .models import (
    EntryPlan,
    MarketRegime,
    MarketSignal,
    PositionSide,
    ScoreComponent,
    SetupType,
    SignalAction,
    TimeframeState,
)
from .validation import (
    DataValidationError,
    ensure_unique_timeframes,
    prepare_ohlcv,
    timeframe_to_timedelta,
)


_REQUIRED_FEATURES = {
    "timestamp",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "ema_fast",
    "ema_mid",
    "ema_slow",
    "ema_mid_slope",
    "ema_slow_slope",
    "rsi",
    "atr",
    "atr_percent",
    "volatility_percentile",
    "adx",
    "plus_di",
    "minus_di",
    "macd_histogram",
    "volume_ratio",
    "donchian_high",
    "donchian_low",
    "donchian_mid",
    "swing_high",
    "swing_low",
    "body_atr",
    "distance_fast_ema_atr",
}


@dataclass(frozen=True)
class _SetupDetection:
    setup_type: SetupType
    trigger_price: float
    actionable: bool
    reasons: tuple[str, ...]


class RegimeSignalEngine:
    """Explainable multi-timeframe market-regime and trade-plan engine.

    The engine produces deterministic signals from closed OHLCV candles. Its
    confidence field is a normalized rule score, not a calibrated probability.
    """

    def __init__(self, config: RegimeBotConfig | None = None):
        self.config = config or RegimeBotConfig()
        self.config.validate()

    def prepare_feature_frames(
        self,
        frames: Mapping[str, pd.DataFrame],
        execution_timeframe: str | None = None,
    ) -> dict[str, pd.DataFrame]:
        normalized = self._normalize_frames(frames)
        execution = self._resolve_execution_timeframe(normalized, execution_timeframe)
        self._validate_timeframe_hierarchy(tuple(normalized), execution)
        return {
            timeframe: build_features(prepare_ohlcv(frame), self.config.indicators)
            for timeframe, frame in normalized.items()
        }

    def analyze(
        self,
        *,
        symbol: str,
        frames: Mapping[str, pd.DataFrame],
        execution_timeframe: str | None = None,
        equity: float = 10_000.0,
        risk_percent: float | None = None,
        maximum_notional_multiple: float | None = None,
    ) -> MarketSignal:
        normalized = self._normalize_frames(frames)
        execution = self._resolve_execution_timeframe(normalized, execution_timeframe)
        self._validate_timeframe_hierarchy(tuple(normalized), execution)
        warnings: list[str] = []
        prepared: dict[str, pd.DataFrame] = {}
        rejected_contexts: list[str] = []
        execution_candle_time: datetime | None = None

        for timeframe, data in normalized.items():
            try:
                clean = prepare_ohlcv(data)
            except DataValidationError as exc:
                if timeframe == execution:
                    return self._safe_no_trade(
                        symbol=symbol,
                        execution_timeframe=execution,
                        generated_at=datetime.now(timezone.utc),
                        warnings=(f"Execution timeframe rejected: {exc}",),
                    )
                rejected_contexts.append(timeframe)
                warnings.append(f"Context timeframe {timeframe} rejected: {exc}")
                continue
            if timeframe == execution and not clean.empty:
                execution_candle_time = clean.iloc[-1]["timestamp"].to_pydatetime()
            if len(clean) < self.config.minimum_bars:
                message = (
                    f"{timeframe} has {len(clean)} candles; "
                    f"{self.config.minimum_bars} are required."
                )
                if timeframe == execution:
                    candle_time = clean.iloc[-1]["timestamp"].to_pydatetime()
                    return self._safe_no_trade(
                        symbol=symbol,
                        execution_timeframe=execution,
                        generated_at=candle_time,
                        warnings=(message,),
                    )
                rejected_contexts.append(timeframe)
                warnings.append(f"Context timeframe rejected: {message}")
                continue
            prepared[timeframe] = build_features(clean, self.config.indicators)

        if rejected_contexts:
            return self._safe_no_trade(
                symbol=symbol,
                execution_timeframe=execution,
                generated_at=execution_candle_time or datetime.now(timezone.utc),
                warnings=tuple(
                    warnings
                    + [
                        "Analysis requires every requested context timeframe; "
                        f"unusable: {sorted(rejected_contexts)}."
                    ]
                ),
            )

        if execution not in prepared:
            return self._safe_no_trade(
                symbol=symbol,
                execution_timeframe=execution,
                generated_at=datetime.now(timezone.utc),
                warnings=tuple(warnings + ["No usable execution timeframe data."]),
            )

        return self.analyze_prepared(
            symbol=symbol,
            feature_frames=prepared,
            execution_timeframe=execution,
            equity=equity,
            risk_percent=risk_percent,
            maximum_notional_multiple=maximum_notional_multiple,
            inherited_warnings=tuple(warnings),
        )

    def analyze_prepared(
        self,
        *,
        symbol: str,
        feature_frames: Mapping[str, pd.DataFrame],
        execution_timeframe: str,
        equity: float = 10_000.0,
        risk_percent: float | None = None,
        maximum_notional_multiple: float | None = None,
        inherited_warnings: tuple[str, ...] = (),
    ) -> MarketSignal:
        execution = execution_timeframe.strip().lower()
        self._validate_timeframe_hierarchy(tuple(feature_frames), execution)
        if execution not in feature_frames:
            raise ValueError(f"Execution timeframe {execution!r} is not available.")
        if equity <= 0 or not math.isfinite(equity):
            raise ValueError("Equity must be a positive finite number.")

        usable_frames: dict[str, pd.DataFrame] = {}
        warnings = list(inherited_warnings)
        incomplete_timeframes: list[str] = []
        for timeframe, frame in feature_frames.items():
            timeframe = timeframe.strip().lower()
            missing = _REQUIRED_FEATURES.difference(frame.columns)
            if missing:
                raise ValueError(
                    f"Prepared frame {timeframe} is missing features: {sorted(missing)}"
                )
            if frame.empty:
                incomplete_timeframes.append(timeframe)
                warnings.append(f"{timeframe} has no prepared candles.")
                continue
            latest_missing = frame.iloc[-1][list(_REQUIRED_FEATURES)].isna()
            if bool(latest_missing.any()):
                incomplete_timeframes.append(timeframe)
                missing_latest = sorted(latest_missing[latest_missing].index.tolist())
                warnings.append(
                    f"{timeframe} latest candle has incomplete features: {missing_latest}."
                )
                continue
            complete = frame.dropna(subset=list(_REQUIRED_FEATURES)).copy()
            if len(complete) < 2:
                incomplete_timeframes.append(timeframe)
                warnings.append(f"{timeframe} indicator warm-up is incomplete.")
                continue
            usable_frames[timeframe] = complete

        if incomplete_timeframes:
            execution_frame = feature_frames.get(execution)
            if execution_frame is not None and not execution_frame.empty:
                generated_at = execution_frame.iloc[-1]["timestamp"].to_pydatetime()
            else:
                generated_at = datetime.now(timezone.utc)
            return self._safe_no_trade(
                symbol=symbol,
                execution_timeframe=execution,
                generated_at=generated_at,
                warnings=tuple(
                    warnings
                    + [
                        "Analysis requires complete latest features for every requested "
                        f"timeframe; unusable: {sorted(incomplete_timeframes)}."
                    ]
                ),
            )

        if execution not in usable_frames:
            return self._safe_no_trade(
                symbol=symbol,
                execution_timeframe=execution,
                generated_at=datetime.now(timezone.utc),
                warnings=tuple(warnings + ["Execution features are unavailable."]),
            )

        states = {
            timeframe: self._classify_timeframe(timeframe, frame)
            for timeframe, frame in usable_frames.items()
        }
        ordered_timeframes = sorted(states, key=lambda value: timeframe_to_timedelta(value))
        ordered_states = tuple(states[value] for value in ordered_timeframes)
        weights = self._timeframe_weights(ordered_timeframes)
        aggregate_score = float(
            sum(weights[timeframe] * states[timeframe].score for timeframe in ordered_timeframes)
        )
        aggregate_adx = float(
            sum(weights[timeframe] * states[timeframe].adx for timeframe in ordered_timeframes)
        )
        regime = self._regime_from_score(aggregate_score, aggregate_adx)
        consensus = self._consensus(states, weights, aggregate_score)
        confidence = float(
            np.clip(
                sum(
                    weights[timeframe] * states[timeframe].confidence
                    for timeframe in ordered_timeframes
                )
                * (0.70 + 0.30 * consensus),
                0.0,
                100.0,
            )
        )

        execution_frame = usable_frames[execution]
        latest = execution_frame.iloc[-1]
        decision_time = (
            latest["timestamp"] + timeframe_to_timedelta(execution)
        ).to_pydatetime()
        reasons: list[str] = [
            f"Weighted regime score is {aggregate_score:.1f}/100 across "
            f"{len(ordered_states)} timeframe(s).",
            f"Directional timeframe consensus is {consensus * 100:.0f}%.",
        ]
        highest_timeframe = ordered_timeframes[-1]
        execution_state = states[execution]
        highest_state = states[highest_timeframe]
        hard_timeframe_conflict = (
            execution_state.score * highest_state.score < 0
            and abs(execution_state.score) > self.config.signals.range_score_band
            and abs(highest_state.score) > self.config.signals.range_score_band
        )
        if hard_timeframe_conflict:
            reasons.append("No entry: execution and context timeframes are not sufficiently aligned.")
            return MarketSignal.create(
                symbol=symbol,
                generated_at=decision_time,
                execution_timeframe=execution,
                action=SignalAction.NO_TRADE,
                regime=regime,
                score=round(aggregate_score, 2),
                confidence=round(confidence, 2),
                consensus=round(consensus, 4),
                setup_type=SetupType.NONE,
                trigger_price=None,
                entry_plan=None,
                timeframe_states=ordered_states,
                reasons=tuple(reasons),
                warnings=tuple(warnings),
            )

        if regime in {MarketRegime.RANGE, MarketRegime.TRANSITION}:
            reasons.append("No entry: the market is ranging or transitioning, not directionally stable.")
            return MarketSignal.create(
                symbol=symbol,
                generated_at=decision_time,
                execution_timeframe=execution,
                action=SignalAction.NO_TRADE,
                regime=regime,
                score=round(aggregate_score, 2),
                confidence=round(confidence, 2),
                consensus=round(consensus, 4),
                setup_type=SetupType.NONE,
                trigger_price=None,
                entry_plan=None,
                timeframe_states=ordered_states,
                reasons=tuple(reasons),
                warnings=tuple(warnings),
            )

        direction = PositionSide.LONG if aggregate_score > 0 else PositionSide.SHORT
        conflict = (
            direction == PositionSide.LONG
            and (execution_state.score < -self.config.signals.range_score_band or highest_state.score < 0)
        ) or (
            direction == PositionSide.SHORT
            and (execution_state.score > self.config.signals.range_score_band or highest_state.score > 0)
        )

        if consensus < self.config.signals.minimum_consensus or conflict:
            reasons.append("No entry: execution and context timeframes are not sufficiently aligned.")
            return MarketSignal.create(
                symbol=symbol,
                generated_at=decision_time,
                execution_timeframe=execution,
                action=SignalAction.NO_TRADE,
                regime=regime,
                score=round(aggregate_score, 2),
                confidence=round(confidence, 2),
                consensus=round(consensus, 4),
                setup_type=SetupType.NONE,
                trigger_price=None,
                entry_plan=None,
                timeframe_states=ordered_states,
                reasons=tuple(reasons),
                warnings=tuple(warnings),
            )

        detection = self._detect_setup(execution_frame, direction)
        reasons.extend(detection.reasons)
        extreme_volatility = (
            execution_state.atr_percent > self.config.signals.maximum_atr_percent
            or (
                execution_state.volatility_percentile
                >= self.config.signals.extreme_volatility_percentile
                and float(latest["body_atr"]) > self.config.signals.maximum_breakout_body_atr
            )
        )
        blocked = execution_state.overextended or extreme_volatility
        if execution_state.overextended:
            warnings.append("Entry blocked: price is too far from the fast EMA relative to ATR.")
        if extreme_volatility:
            warnings.append("Entry blocked: current volatility is outside configured risk limits.")

        action = (
            SignalAction.LONG
            if direction == PositionSide.LONG
            else SignalAction.SHORT
        )
        watch_action = (
            SignalAction.WATCH_LONG
            if direction == PositionSide.LONG
            else SignalAction.WATCH_SHORT
        )
        entry_plan: EntryPlan | None = None
        final_action = action if detection.actionable and not blocked else watch_action

        if final_action in {SignalAction.LONG, SignalAction.SHORT}:
            try:
                entry_plan = self._build_entry_plan(
                    direction=direction,
                    setup_type=detection.setup_type,
                    trigger_price=detection.trigger_price,
                    latest=latest,
                    equity=equity,
                    risk_percent=risk_percent,
                    maximum_notional_multiple=maximum_notional_multiple,
                )
            except ValueError as exc:
                final_action = watch_action
                warnings.append(f"Entry plan rejected: {exc}")

        if final_action in {SignalAction.WATCH_LONG, SignalAction.WATCH_SHORT}:
            reasons.append("Regime is directional, but the execution trigger or risk plan is not confirmed.")

        return MarketSignal.create(
            symbol=symbol,
            generated_at=decision_time,
            execution_timeframe=execution,
            action=final_action,
            regime=regime,
            score=round(aggregate_score, 2),
            confidence=round(confidence, 2),
            consensus=round(consensus, 4),
            setup_type=detection.setup_type,
            trigger_price=round(detection.trigger_price, 12),
            entry_plan=entry_plan,
            timeframe_states=ordered_states,
            reasons=tuple(reasons),
            warnings=tuple(warnings),
        )

    @staticmethod
    def _normalize_frames(
        frames: Mapping[str, pd.DataFrame],
    ) -> dict[str, pd.DataFrame]:
        if not frames:
            raise DataValidationError("At least one timeframe is required.")
        normalized: dict[str, pd.DataFrame] = {}
        for raw_timeframe, frame in frames.items():
            timeframe = str(raw_timeframe).strip().lower()
            timeframe_to_timedelta(timeframe)
            if timeframe in normalized:
                raise DataValidationError(
                    f"Duplicate timeframe after normalization: {timeframe}"
                )
            normalized[timeframe] = frame
        ensure_unique_timeframes(normalized)
        return normalized

    @staticmethod
    def _validate_timeframe_hierarchy(
        timeframes: tuple[str, ...],
        execution_timeframe: str,
    ) -> None:
        execution = execution_timeframe.strip().lower()
        execution_delta = timeframe_to_timedelta(execution)
        invalid = sorted(
            timeframe
            for timeframe in timeframes
            if timeframe.strip().lower() != execution
            and timeframe_to_timedelta(timeframe) <= execution_delta
        )
        if invalid:
            raise ValueError(
                "Context timeframes must be higher than the execution timeframe; "
                f"invalid values: {invalid}."
            )

    def _resolve_execution_timeframe(
        self,
        frames: Mapping[str, pd.DataFrame],
        requested: str | None,
    ) -> str:
        if requested:
            value = requested.strip().lower()
            if value not in frames:
                raise ValueError(f"Execution timeframe {value!r} is not present in frames.")
            timeframe_to_timedelta(value)
            return value
        return min(frames, key=lambda value: timeframe_to_timedelta(value))

    def _classify_timeframe(self, timeframe: str, frame: pd.DataFrame) -> TimeframeState:
        row = frame.iloc[-1]
        components: list[ScoreComponent] = []
        score = 0.0

        fast = float(row["ema_fast"])
        mid = float(row["ema_mid"])
        slow = float(row["ema_slow"])
        close = float(row["close"])
        adx_value = float(row["adx"])
        plus_di = float(row["plus_di"])
        minus_di = float(row["minus_di"])
        rsi_value = float(row["rsi"])

        if fast > mid > slow:
            value = 30.0
            explanation = "Fast, medium, and slow EMAs are stacked bullishly."
        elif fast < mid < slow:
            value = -30.0
            explanation = "Fast, medium, and slow EMAs are stacked bearishly."
        elif fast > mid and close > slow:
            value = 12.0
            explanation = "Short-term EMA structure is bullish above the slow trend filter."
        elif fast < mid and close < slow:
            value = -12.0
            explanation = "Short-term EMA structure is bearish below the slow trend filter."
        else:
            value = 0.0
            explanation = "EMA structure is mixed."
        components.append(ScoreComponent("ema_structure", value, explanation))
        score += value

        price_value = sum(
            5.0 if close > level else -5.0
            for level in (fast, mid, slow)
        )
        components.append(
            ScoreComponent(
                "price_location",
                price_value,
                "Price location versus the fast, medium, and slow EMAs.",
            )
        )
        score += price_value

        mid_slope = float(row["ema_mid_slope"])
        slow_slope = float(row["ema_slow_slope"])
        mid_slope_value = 8.0 if mid_slope > 0 else -8.0 if mid_slope < 0 else 0.0
        slow_slope_value = 7.0 if slow_slope > 0 else -7.0 if slow_slope < 0 else 0.0
        slope_value = mid_slope_value + slow_slope_value
        components.append(
            ScoreComponent(
                "trend_slope",
                slope_value,
                "Direction of medium- and slow-EMA slopes.",
            )
        )
        score += slope_value

        directional_value = 0.0
        if adx_value >= self.config.signals.minimum_trend_adx:
            directional_value = 15.0 if plus_di > minus_di else -15.0
            if adx_value >= self.config.signals.strong_trend_adx:
                directional_value += 5.0 if plus_di > minus_di else -5.0
        components.append(
            ScoreComponent(
                "directional_strength",
                directional_value,
                f"ADX {adx_value:.1f} with +DI/-DI direction.",
            )
        )
        score += directional_value

        macd_histogram = float(row["macd_histogram"])
        momentum_value = 8.0 if macd_histogram > 0 else -8.0 if macd_histogram < 0 else 0.0
        if rsi_value >= 55.0:
            momentum_value += 7.0
        elif rsi_value <= 45.0:
            momentum_value -= 7.0
        components.append(
            ScoreComponent(
                "momentum",
                momentum_value,
                f"MACD histogram and RSI {rsi_value:.1f} momentum state.",
            )
        )
        score += momentum_value

        structure_value = 0.0
        if close > float(row["donchian_mid"]):
            structure_value += 5.0
        else:
            structure_value -= 5.0
        if close > float(row["donchian_high"]):
            structure_value += 5.0
        elif close < float(row["donchian_low"]):
            structure_value -= 5.0
        components.append(
            ScoreComponent(
                "market_structure",
                structure_value,
                "Price location inside or outside the prior Donchian range.",
            )
        )
        score += structure_value

        score = float(np.clip(score, -100.0, 100.0))
        regime = self._regime_from_score(score, adx_value)
        ordered_alignment = fast > mid > slow or fast < mid < slow
        confidence = float(
            np.clip(
                abs(score) * 0.68
                + min(adx_value, 50.0) * 0.75
                + (10.0 if ordered_alignment else 0.0),
                0.0,
                100.0,
            )
        )
        distance = float(row["distance_fast_ema_atr"])
        overextended = abs(distance) > self.config.signals.maximum_overextension_atr

        return TimeframeState(
            timeframe=timeframe,
            candle_time=row["timestamp"].to_pydatetime(),
            regime=regime,
            score=round(score, 2),
            confidence=round(confidence, 2),
            adx=round(adx_value, 2),
            rsi=round(rsi_value, 2),
            atr=round(float(row["atr"]), 12),
            atr_percent=round(float(row["atr_percent"]), 8),
            volatility_percentile=round(float(row["volatility_percentile"]), 4),
            volume_ratio=round(float(row["volume_ratio"]), 4),
            distance_from_fast_ema_atr=round(distance, 4),
            overextended=overextended,
            score_components=tuple(components),
        )

    def _regime_from_score(self, score: float, adx_value: float) -> MarketRegime:
        s = self.config.signals
        if score >= s.strong_bullish_score and adx_value >= s.strong_trend_adx:
            return MarketRegime.STRONG_BULLISH
        if score >= s.bullish_score and adx_value >= s.minimum_trend_adx:
            return MarketRegime.BULLISH
        if score <= s.strong_bearish_score and adx_value >= s.strong_trend_adx:
            return MarketRegime.STRONG_BEARISH
        if score <= s.bearish_score and adx_value >= s.minimum_trend_adx:
            return MarketRegime.BEARISH
        if abs(score) <= s.range_score_band and adx_value < s.strong_trend_adx:
            return MarketRegime.RANGE
        return MarketRegime.TRANSITION

    def _timeframe_weights(self, timeframes: list[str]) -> dict[str, float]:
        raw = np.arange(1, len(timeframes) + 1, dtype=float)
        raw /= raw.sum()
        return {timeframe: float(weight) for timeframe, weight in zip(timeframes, raw)}

    @staticmethod
    def _consensus(
        states: Mapping[str, TimeframeState],
        weights: Mapping[str, float],
        aggregate_score: float,
    ) -> float:
        if aggregate_score == 0:
            return 0.0
        direction = 1 if aggregate_score > 0 else -1
        aligned = 0.0
        for timeframe, state in states.items():
            state_direction = 1 if state.score > 0 else -1 if state.score < 0 else 0
            if state_direction == direction:
                aligned += weights[timeframe]
        return float(np.clip(aligned, 0.0, 1.0))

    def _detect_setup(
        self,
        frame: pd.DataFrame,
        direction: PositionSide,
    ) -> _SetupDetection:
        latest = frame.iloc[-1]
        previous = frame.iloc[-2]
        s = self.config.signals
        atr_value = float(latest["atr"])
        volume_ok = float(latest["volume_ratio"]) >= s.minimum_volume_ratio
        body_ok = float(latest["body_atr"]) <= s.maximum_breakout_body_atr

        if direction == PositionSide.LONG:
            breakout_level = float(latest["donchian_high"])
            breakout = (
                float(latest["close"]) > breakout_level
                and float(previous["close"]) <= breakout_level
                and volume_ok
                and body_ok
            )
            pullback = (
                float(latest["ema_fast"]) > float(latest["ema_mid"]) > float(latest["ema_slow"])
                and float(latest["low"])
                <= float(latest["ema_fast"]) + s.pullback_touch_atr * atr_value
                and float(latest["low"])
                >= float(latest["ema_mid"]) - s.pullback_depth_atr * atr_value
                and float(latest["close"]) > float(latest["ema_fast"])
                and float(latest["close"]) > float(latest["open"])
                and float(latest["close"]) > float(previous["high"])
                and s.pullback_long_rsi_min <= float(latest["rsi"]) <= s.pullback_long_rsi_max
            )
            if breakout:
                return _SetupDetection(
                    SetupType.BREAKOUT,
                    breakout_level + self.config.risk.breakout_entry_buffer_atr * atr_value,
                    True,
                    ("Long breakout confirmed above the prior range with volume.",),
                )
            if pullback:
                return _SetupDetection(
                    SetupType.PULLBACK,
                    float(previous["high"]),
                    True,
                    ("Bullish pullback reclaimed the fast EMA and prior candle high.",),
                )
            trigger = max(float(previous["high"]), breakout_level)
            return _SetupDetection(
                SetupType.NONE,
                trigger,
                False,
                ("Bullish regime detected; waiting for a pullback reclaim or volume-backed breakout.",),
            )

        breakout_level = float(latest["donchian_low"])
        breakout = (
            float(latest["close"]) < breakout_level
            and float(previous["close"]) >= breakout_level
            and volume_ok
            and body_ok
        )
        pullback = (
            float(latest["ema_fast"]) < float(latest["ema_mid"]) < float(latest["ema_slow"])
            and float(latest["high"])
            >= float(latest["ema_fast"]) - s.pullback_touch_atr * atr_value
            and float(latest["high"])
            <= float(latest["ema_mid"]) + s.pullback_depth_atr * atr_value
            and float(latest["close"]) < float(latest["ema_fast"])
            and float(latest["close"]) < float(latest["open"])
            and float(latest["close"]) < float(previous["low"])
            and s.pullback_short_rsi_min <= float(latest["rsi"]) <= s.pullback_short_rsi_max
        )
        if breakout:
            return _SetupDetection(
                SetupType.BREAKOUT,
                breakout_level - self.config.risk.breakout_entry_buffer_atr * atr_value,
                True,
                ("Short breakout confirmed below the prior range with volume.",),
            )
        if pullback:
            return _SetupDetection(
                SetupType.PULLBACK,
                float(previous["low"]),
                True,
                ("Bearish pullback rejected the fast EMA and prior candle low.",),
            )
        trigger = min(float(previous["low"]), breakout_level)
        return _SetupDetection(
            SetupType.NONE,
            trigger,
            False,
            ("Bearish regime detected; waiting for a pullback rejection or volume-backed breakdown.",),
        )

    def _build_entry_plan(
        self,
        *,
        direction: PositionSide,
        setup_type: SetupType,
        trigger_price: float,
        latest: pd.Series,
        equity: float,
        risk_percent: float | None,
        maximum_notional_multiple: float | None,
    ) -> EntryPlan:
        r = self.config.risk
        requested_risk = r.risk_percent if risk_percent is None else float(risk_percent)
        if not (0.0 < requested_risk <= r.maximum_risk_percent):
            raise ValueError(
                f"risk_percent must be above 0 and at most {r.maximum_risk_percent:.2f}."
            )
        notional_multiple = (
            r.maximum_notional_multiple
            if maximum_notional_multiple is None
            else float(maximum_notional_multiple)
        )
        if notional_multiple <= 0 or not math.isfinite(notional_multiple):
            raise ValueError("maximum_notional_multiple must be positive and finite.")

        atr_value = float(latest["atr"])
        if atr_value <= 0 or not math.isfinite(atr_value):
            raise ValueError("ATR is unavailable or invalid.")
        close = float(latest["close"])
        total_cost_rate = (r.fee_bps + r.slippage_bps) / 10_000.0

        if direction == PositionSide.LONG:
            raw_entry = close
            reference = raw_entry * (1.0 + r.slippage_bps / 10_000.0)
            if setup_type == SetupType.BREAKOUT:
                entry_low = max(trigger_price, close - 0.05 * atr_value)
                entry_high = close + r.entry_zone_atr * atr_value
            else:
                entry_low = max(float(latest["ema_fast"]), close - r.entry_zone_atr * atr_value)
                entry_high = close + 0.05 * atr_value
            entry_high = max(entry_high, reference)
            if setup_type == SetupType.BREAKOUT:
                breakout_level = trigger_price - r.breakout_entry_buffer_atr * atr_value
                structural = max(breakout_level, float(latest["ema_fast"]))
            else:
                structural = max(float(latest["swing_low"]), float(latest["ema_mid"]))
            structural_stop = structural - r.structural_stop_buffer_atr * atr_value
            stop = min(structural_stop, reference - r.minimum_stop_atr * atr_value)
            distance = reference - stop
            direction_multiplier = 1.0
        else:
            raw_entry = close
            reference = raw_entry * (1.0 - r.slippage_bps / 10_000.0)
            if setup_type == SetupType.BREAKOUT:
                entry_low = close - r.entry_zone_atr * atr_value
                entry_high = min(trigger_price, close + 0.05 * atr_value)
            else:
                entry_low = close - 0.05 * atr_value
                entry_high = min(float(latest["ema_fast"]), close + r.entry_zone_atr * atr_value)
            entry_low = min(entry_low, reference)
            if setup_type == SetupType.BREAKOUT:
                breakout_level = trigger_price + r.breakout_entry_buffer_atr * atr_value
                structural = min(breakout_level, float(latest["ema_fast"]))
            else:
                structural = min(float(latest["swing_high"]), float(latest["ema_mid"]))
            structural_stop = structural + r.structural_stop_buffer_atr * atr_value
            stop = max(structural_stop, reference + r.minimum_stop_atr * atr_value)
            distance = stop - reference
            direction_multiplier = -1.0

        if distance <= 0 or not math.isfinite(distance):
            raise ValueError("Stop-loss distance is invalid.")
        stop_atr = distance / atr_value
        if stop_atr > r.maximum_stop_atr:
            raise ValueError(
                f"Required structural stop is {stop_atr:.2f} ATR; maximum is {r.maximum_stop_atr:.2f} ATR."
            )

        target_risk = equity * requested_risk / 100.0
        estimated_cost_per_unit = (reference + stop) * total_cost_rate
        total_risk_per_unit = distance + estimated_cost_per_unit
        units_by_risk = target_risk / total_risk_per_unit
        maximum_notional = equity * notional_multiple
        units_by_notional = maximum_notional / reference
        quantity = min(units_by_risk, units_by_notional)
        if quantity <= 0 or not math.isfinite(quantity):
            raise ValueError("Calculated position size is invalid.")

        notional = quantity * reference
        round_trip_cost = quantity * estimated_cost_per_unit
        actual_risk = quantity * distance + round_trip_cost
        target1 = reference + direction_multiplier * r.take_profit_1_r * distance
        target2 = reference + direction_multiplier * r.take_profit_2_r * distance
        target3 = reference + direction_multiplier * r.take_profit_3_r * distance

        return EntryPlan(
            side=direction,
            setup_type=setup_type,
            trigger_price=round(trigger_price, 12),
            entry_low=round(min(entry_low, entry_high), 12),
            entry_high=round(max(entry_low, entry_high), 12),
            reference_entry=round(reference, 12),
            stop_loss=round(stop, 12),
            take_profit_1=round(target1, 12),
            take_profit_2=round(target2, 12),
            take_profit_3=round(target3, 12),
            risk_per_unit=round(distance, 12),
            target_risk_amount=round(target_risk, 8),
            actual_risk_amount=round(actual_risk, 8),
            position_size=round(quantity, 12),
            notional_value=round(notional, 8),
            estimated_round_trip_cost=round(round_trip_cost, 8),
            reward_to_risk_1=r.take_profit_1_r,
            reward_to_risk_2=r.take_profit_2_r,
            reward_to_risk_3=r.take_profit_3_r,
            move_stop_to_break_even_after=r.take_profit_1_r,
            trailing_stop_after=r.take_profit_2_r,
            trailing_atr_multiple=r.trailing_atr_multiple,
            max_holding_bars=r.max_holding_bars,
        )

    def _safe_no_trade(
        self,
        *,
        symbol: str,
        execution_timeframe: str,
        generated_at: datetime,
        warnings: tuple[str, ...],
    ) -> MarketSignal:
        if generated_at.tzinfo is None:
            generated_at = generated_at.replace(tzinfo=timezone.utc)
        return MarketSignal.create(
            symbol=symbol,
            generated_at=generated_at,
            execution_timeframe=execution_timeframe,
            action=SignalAction.NO_TRADE,
            regime=MarketRegime.TRANSITION,
            score=0.0,
            confidence=0.0,
            consensus=0.0,
            setup_type=SetupType.NONE,
            trigger_price=None,
            entry_plan=None,
            timeframe_states=(),
            reasons=("Analysis was rejected safely because required data was unavailable.",),
            warnings=warnings,
        )
