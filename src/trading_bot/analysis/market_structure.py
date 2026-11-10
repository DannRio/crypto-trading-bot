from dataclasses import dataclass

import pandas as pd

from trading_bot.core.enums import MarketRegime


@dataclass(frozen=True)
class MarketStructure:
    regime: MarketRegime
    support: float
    resistance: float
    atr_pct: float
    volume_ratio: float
    higher_high: bool
    higher_low: bool
    lower_high: bool
    lower_low: bool
    high_volatility: bool


def analyze_market_structure(
    df: pd.DataFrame,
    lookback: int = 20,
    structure_window: int = 5,
    high_volatility_threshold: float = 0.04,
) -> MarketStructure:
    required = {"close", "high", "low", "volume", "ema_50", "ema_200", "atr_14"}
    missing = required.difference(df.columns)
    if missing:
        raise ValueError(f"Missing market-structure columns: {sorted(missing)}")

    minimum_rows = max(lookback + 1, structure_window * 2)
    if len(df) < minimum_rows:
        raise ValueError(f"At least {minimum_rows} candles are required.")

    latest = df.iloc[-1]
    reference = df.iloc[-(lookback + 1):-1]
    support = float(reference["low"].min())
    resistance = float(reference["high"].max())

    close = float(latest["close"])
    ema_50 = float(latest["ema_50"])
    ema_200 = float(latest["ema_200"])
    atr = float(latest["atr_14"])

    if pd.isna(ema_50) or pd.isna(ema_200) or pd.isna(atr) or close <= 0:
        regime = MarketRegime.UNKNOWN
    elif close > ema_50 > ema_200:
        regime = MarketRegime.BULL_TREND
    elif close < ema_50 < ema_200:
        regime = MarketRegime.BEAR_TREND
    else:
        # An EMA disagreement is not evidence of a tradable range.
        # Only classify as RANGE when EMA separation and recent EMA50
        # drift are small relative to ATR; otherwise the regime is
        # transitional/unknown and range entries are disabled.
        prior_ema50 = float(df.iloc[-min(len(df), 11)]["ema_50"])
        ema_spread_atr = abs(ema_50 - ema_200) / atr if atr > 0 else float("inf")
        ema_drift_atr = abs(ema_50 - prior_ema50) / atr if atr > 0 else float("inf")
        if (
            atr > 0
            and ema_spread_atr <= 1.0
            and ema_drift_atr <= 0.5
            and support <= close <= resistance
        ):
            regime = MarketRegime.RANGE
        else:
            regime = MarketRegime.UNKNOWN

    atr_pct = atr / close if close > 0 and pd.notna(atr) else 0.0
    if (
        "volume_sma_20" in df.columns
        and pd.notna(latest["volume_sma_20"])
        and float(latest["volume_sma_20"]) > 0
    ):
        volume_baseline = float(latest["volume_sma_20"])
    else:
        volume_baseline = float(reference["volume"].mean())
    volume_ratio = float(latest["volume"]) / volume_baseline if volume_baseline > 0 else 0.0

    previous_window = df.iloc[-(structure_window * 2):-structure_window]
    recent_window = df.iloc[-structure_window:]
    previous_high = float(previous_window["high"].max())
    previous_low = float(previous_window["low"].min())
    recent_high = float(recent_window["high"].max())
    recent_low = float(recent_window["low"].min())

    return MarketStructure(
        regime=regime,
        support=support,
        resistance=resistance,
        atr_pct=atr_pct,
        volume_ratio=volume_ratio,
        higher_high=recent_high > previous_high,
        higher_low=recent_low > previous_low,
        lower_high=recent_high < previous_high,
        lower_low=recent_low < previous_low,
        high_volatility=atr_pct >= high_volatility_threshold,
    )
