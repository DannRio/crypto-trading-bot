from __future__ import annotations

import json
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, TypeVar


@dataclass(frozen=True)
class IndicatorConfig:
    ema_fast: int = 20
    ema_mid: int = 50
    ema_slow: int = 200
    slope_lookback: int = 5
    rsi_period: int = 14
    atr_period: int = 14
    adx_period: int = 14
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    volume_period: int = 20
    donchian_period: int = 20
    swing_period: int = 10
    volatility_window: int = 100


@dataclass(frozen=True)
class SignalConfig:
    bullish_score: float = 35.0
    strong_bullish_score: float = 62.0
    bearish_score: float = -35.0
    strong_bearish_score: float = -62.0
    range_score_band: float = 18.0
    minimum_trend_adx: float = 16.0
    strong_trend_adx: float = 23.0
    minimum_consensus: float = 0.62
    minimum_volume_ratio: float = 1.05
    maximum_overextension_atr: float = 2.20
    maximum_breakout_body_atr: float = 1.80
    maximum_atr_percent: float = 0.12
    extreme_volatility_percentile: float = 0.98
    pullback_touch_atr: float = 0.35
    pullback_depth_atr: float = 0.85
    pullback_long_rsi_min: float = 43.0
    pullback_long_rsi_max: float = 69.0
    pullback_short_rsi_min: float = 31.0
    pullback_short_rsi_max: float = 57.0


@dataclass(frozen=True)
class RiskConfig:
    risk_percent: float = 0.50
    maximum_risk_percent: float = 2.00
    maximum_notional_multiple: float = 1.00
    minimum_stop_atr: float = 1.10
    maximum_stop_atr: float = 3.50
    structural_stop_buffer_atr: float = 0.25
    entry_zone_atr: float = 0.15
    breakout_entry_buffer_atr: float = 0.05
    take_profit_1_r: float = 1.00
    take_profit_2_r: float = 2.00
    take_profit_3_r: float = 3.00
    take_profit_1_fraction: float = 0.25
    take_profit_2_fraction: float = 0.35
    trailing_atr_multiple: float = 2.00
    max_holding_bars: int = 96
    fee_bps: float = 10.0
    slippage_bps: float = 5.0


@dataclass(frozen=True)
class RegimeBotConfig:
    indicators: IndicatorConfig = field(default_factory=IndicatorConfig)
    signals: SignalConfig = field(default_factory=SignalConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    minimum_bars: int = 220

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "RegimeBotConfig":
        unknown = set(payload).difference({"indicators", "signals", "risk", "minimum_bars"})
        if unknown:
            raise ValueError(f"Unknown configuration keys: {sorted(unknown)}")

        indicators = _from_mapping(IndicatorConfig, payload.get("indicators", {}))
        signals = _from_mapping(SignalConfig, payload.get("signals", {}))
        risk = _from_mapping(RiskConfig, payload.get("risk", {}))
        minimum_bars = int(payload.get("minimum_bars", cls.minimum_bars))
        config = cls(
            indicators=indicators,
            signals=signals,
            risk=risk,
            minimum_bars=minimum_bars,
        )
        config.validate()
        return config

    @classmethod
    def from_json(cls, path: str | Path) -> "RegimeBotConfig":
        with Path(path).open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, dict):
            raise ValueError("Configuration file must contain a JSON object.")
        return cls.from_dict(payload)

    def validate(self) -> None:
        i = self.indicators
        s = self.signals
        r = self.risk

        period_values = {
            "ema_fast": i.ema_fast,
            "ema_mid": i.ema_mid,
            "ema_slow": i.ema_slow,
            "slope_lookback": i.slope_lookback,
            "rsi_period": i.rsi_period,
            "atr_period": i.atr_period,
            "adx_period": i.adx_period,
            "macd_fast": i.macd_fast,
            "macd_slow": i.macd_slow,
            "macd_signal": i.macd_signal,
            "volume_period": i.volume_period,
            "donchian_period": i.donchian_period,
            "swing_period": i.swing_period,
            "volatility_window": i.volatility_window,
        }
        if any(not isinstance(value, int) or isinstance(value, bool) or value <= 0 for value in period_values.values()):
            raise ValueError("All indicator periods must be positive integers.")
        if not (1 < i.ema_fast < i.ema_mid < i.ema_slow):
            raise ValueError("EMA periods must satisfy 1 < fast < mid < slow.")
        if not (1 < i.macd_fast < i.macd_slow):
            raise ValueError("MACD periods must satisfy 1 < fast < slow.")
        if min(i.volume_period, i.donchian_period, i.swing_period) < 2:
            raise ValueError("Volume, Donchian, and swing periods must be at least 2.")
        if i.volatility_window < 20:
            raise ValueError("volatility_window must be at least 20.")
        required_warmup = max(
            i.ema_slow + i.slope_lookback,
            i.macd_slow + i.macd_signal,
            i.adx_period * 2,
            i.volatility_window,
            i.volume_period + 1,
            i.donchian_period + 1,
            i.swing_period + 1,
        )
        if not isinstance(self.minimum_bars, int) or isinstance(self.minimum_bars, bool):
            raise ValueError("minimum_bars must be an integer.")
        if self.minimum_bars < required_warmup:
            raise ValueError(
                f"minimum_bars must be at least {required_warmup} for indicator warm-up."
            )

        if not (
            -100.0 <= s.strong_bearish_score < s.bearish_score < 0.0
            < s.bullish_score < s.strong_bullish_score <= 100.0
        ):
            raise ValueError("Regime score thresholds are not ordered correctly.")
        if not (0.0 <= s.range_score_band < min(s.bullish_score, abs(s.bearish_score))):
            raise ValueError("range_score_band must sit inside the directional thresholds.")
        if not (0.0 <= s.minimum_trend_adx <= s.strong_trend_adx <= 100.0):
            raise ValueError("ADX thresholds must satisfy 0 <= minimum <= strong <= 100.")
        if not (0.0 < s.minimum_consensus <= 1.0):
            raise ValueError("minimum_consensus must be in (0, 1].")
        if s.minimum_volume_ratio <= 0.0:
            raise ValueError("minimum_volume_ratio must be positive.")
        if min(
            s.maximum_overextension_atr,
            s.maximum_breakout_body_atr,
            s.maximum_atr_percent,
            s.pullback_touch_atr,
            s.pullback_depth_atr,
        ) <= 0.0:
            raise ValueError("Signal distance and volatility limits must be positive.")
        if not (0.0 < s.extreme_volatility_percentile <= 1.0):
            raise ValueError("extreme_volatility_percentile must be in (0, 1].")
        if not (0.0 <= s.pullback_long_rsi_min < s.pullback_long_rsi_max <= 100.0):
            raise ValueError("Long pullback RSI bounds are invalid.")
        if not (0.0 <= s.pullback_short_rsi_min < s.pullback_short_rsi_max <= 100.0):
            raise ValueError("Short pullback RSI bounds are invalid.")

        if not (0.0 < r.risk_percent <= r.maximum_risk_percent <= 10.0):
            raise ValueError("Risk percentages are invalid.")
        if r.maximum_notional_multiple <= 0.0:
            raise ValueError("maximum_notional_multiple must be positive.")
        if r.minimum_stop_atr <= 0 or r.maximum_stop_atr <= r.minimum_stop_atr:
            raise ValueError("Stop-distance ATR constraints are invalid.")
        if min(
            r.structural_stop_buffer_atr,
            r.entry_zone_atr,
            r.breakout_entry_buffer_atr,
        ) < 0.0:
            raise ValueError("Entry and structural buffers cannot be negative.")
        if r.entry_zone_atr == 0.0:
            raise ValueError("entry_zone_atr must be positive.")
        fractions = r.take_profit_1_fraction + r.take_profit_2_fraction
        if not (0.0 < r.take_profit_1_fraction < 1.0):
            raise ValueError("take_profit_1_fraction must be in (0, 1).")
        if not (0.0 < r.take_profit_2_fraction < 1.0):
            raise ValueError("take_profit_2_fraction must be in (0, 1).")
        if fractions >= 1.0:
            raise ValueError("Partial take-profit fractions must sum to less than 1.")
        if min(r.take_profit_1_r, r.take_profit_2_r, r.take_profit_3_r) <= 0:
            raise ValueError("Take-profit R multiples must be positive.")
        if not (r.take_profit_1_r < r.take_profit_2_r < r.take_profit_3_r):
            raise ValueError("Take-profit R multiples must be strictly increasing.")
        if r.trailing_atr_multiple <= 0.0:
            raise ValueError("trailing_atr_multiple must be positive.")
        if not isinstance(r.max_holding_bars, int) or isinstance(r.max_holding_bars, bool) or r.max_holding_bars <= 0:
            raise ValueError("max_holding_bars must be a positive integer.")
        if min(r.fee_bps, r.slippage_bps) < 0.0:
            raise ValueError("Fees and slippage cannot be negative.")
        if max(r.fee_bps, r.slippage_bps) > 1_000.0:
            raise ValueError("Fees and slippage above 1,000 bps are rejected as unsafe.")


T = TypeVar("T")


def _from_mapping(cls: type[T], payload: Any) -> T:
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise ValueError(f"{cls.__name__} configuration must be an object.")
    allowed = {item.name for item in fields(cls)}
    unknown = set(payload).difference(allowed)
    if unknown:
        raise ValueError(f"Unknown {cls.__name__} keys: {sorted(unknown)}")
    return cls(**payload)
