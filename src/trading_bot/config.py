"""Shared, credential-free scan/paper profiles. CLI flags override a profile."""
import argparse
from copy import deepcopy
from dataclasses import dataclass
import json
import math
from pathlib import Path
import sys


@dataclass(frozen=True)
class ConfigurationProfile:
    name: str
    description: str
    settings: dict
    schema_version: int = 1


# Starter research configurations, not optimized or validated trading systems.
_LIQUID = {
    "auto_universe": True, "symbols": "", "quote_currencies": "USDT",
    "max_markets": 30, "min_quote_volume": 5_000_000,
    "max_spread_bps": 40.0, "min_history_candles": 250,
    "max_history_probes": 100, "max_ticker_requests": 200,
    "timeframe": "1h", "confirm_timeframes": "4h", "entry_timeframe": "15m",
    "limit": 500, "equity": 10_000, "risk_percent": 0.5, "min_score": 70,
    "strategies": "trend_continuation_v2", "fee_rate": 0.001,
    "slippage_bps": 5.0, "interval_seconds": 60.0,
    "universe_refresh_seconds": 900.0,
    "max_open_risk_percent": 3.0, "max_symbol_risk_percent": 1.0,
    "max_gross_notional_percent": 100.0, "max_positions": 5,
    "max_pending_orders": 10, "max_correlated_risk_percent": 2.0,
    "allow_missing_correlation_data": False,
}
_PROFILES = {
    "liquid-spot": ConfigurationProfile(
        "liquid-spot", "Up to 30 liquid spot assets; long-only research and paper signals.",
        dict(_LIQUID, market_type="spot"),
    ),
    "broad-spot": ConfigurationProfile(
        "broad-spot", "Up to 50 spot assets with a lower volume threshold; exploratory coverage.",
        dict(_LIQUID, market_type="spot", max_markets=50,
             min_quote_volume=1_000_000, max_spread_bps=60.0, risk_percent=0.25),
    ),
    "liquid-perps": ConfigurationProfile(
        "liquid-perps", "Up to 30 linear perpetual markets; long/short research and paper signals.",
        dict(_LIQUID, market_type="swap"),
    ),
}

_STRING_FIELDS = {
    "exchange", "market_type", "symbols", "quote_currencies", "timeframe",
    "confirm_timeframes", "entry_timeframe", "strategies",
}
_BOOLEAN_FIELDS = {"auto_universe", "allow_missing_correlation_data"}
_INTEGER_RANGES = {
    "max_markets": (1, 100), "min_history_candles": (0, 5000),
    "max_history_probes": (1, 500), "max_ticker_requests": (1, 5000),
    "limit": (250, 1000), "min_score": (0, 100), "entry_expiry_bars": (1, 1000),
    "correlation_lookback": (2, 10000), "min_correlation_samples": (2, 10000),
    "max_positions": (1, 100), "max_pending_orders": (1, 1000),
}
_NUMBER_RANGES = {
    "min_quote_volume": (0, None), "max_spread_bps": (0, 10000),
    "equity": (0, 100_000_000), "risk_percent": (0, 100),
    "fee_rate": (0, 0.05), "slippage_bps": (0, 500),
    "interval_seconds": (0, None), "universe_refresh_seconds": (0, None),
    "max_open_risk_percent": (0, 100), "max_symbol_risk_percent": (0, 100),
    "max_gross_notional_percent": (0, None), "max_correlated_risk_percent": (0, 100),
    "correlation_threshold": (0, 1), "max_daily_loss_percent": (0, 100),
    "max_weekly_loss_percent": (0, 100), "max_drawdown_percent": (0, 100),
}
_ZERO_ALLOWED = {"min_quote_volume", "fee_rate", "slippage_bps"}


def validate_profile_settings(settings: dict) -> None:
    if not isinstance(settings, dict) or not settings:
        raise ValueError("Profile settings must be a non-empty JSON object.")
    allowed = _STRING_FIELDS | _BOOLEAN_FIELDS | _INTEGER_RANGES.keys() | _NUMBER_RANGES.keys()
    unknown = set(settings) - allowed
    if unknown:
        raise ValueError("Unknown profile setting(s): " + ", ".join(sorted(unknown)))
    for name, value in settings.items():
        if name in _STRING_FIELDS:
            if not isinstance(value, str):
                raise ValueError(f"{name} must be a string (comma-separated for lists).")
            if name not in {"symbols", "confirm_timeframes", "entry_timeframe"} and not value.strip():
                raise ValueError(f"{name} cannot be empty.")
        elif name in _BOOLEAN_FIELDS:
            if type(value) is not bool:
                raise ValueError(f"{name} must be a JSON boolean.")
        elif name in _INTEGER_RANGES:
            low, high = _INTEGER_RANGES[name]
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"{name} must be an integer between {low} and {high}.")
        else:
            if name == "max_spread_bps" and value is None:
                continue
            low, high = _NUMBER_RANGES[name]
            if (type(value) not in {int, float} or not math.isfinite(value)
                    or value < low or (value == low and name not in _ZERO_ALLOWED)
                    or (high is not None and value > high)):
                raise ValueError(f"Invalid finite numeric value for {name}.")
    if settings.get("market_type", "spot") not in {"spot", "swap", "future"}:
        raise ValueError("market_type must be spot, swap or future.")


def builtin_profiles() -> list[ConfigurationProfile]:
    return [deepcopy(profile) for profile in _PROFILES.values()]


def load_profile(name_or_path: str) -> ConfigurationProfile:
    if name_or_path in _PROFILES:
        return deepcopy(_PROFILES[name_or_path])
    path = Path(name_or_path).expanduser()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not load profile {name_or_path}: {exc}") from exc
    if not isinstance(raw, dict) or set(raw) - {"schema_version", "name", "description", "settings"}:
        raise ValueError("A profile must contain schema_version, name, description and settings only.")
    if type(raw.get("schema_version")) is not int or raw["schema_version"] != 1:
        raise ValueError("Unsupported profile schema_version (expected 1).")
    if not isinstance(raw.get("name"), str) or not raw["name"].strip():
        raise ValueError("Profile name must be a non-empty string.")
    if not isinstance(raw.get("description", ""), str):
        raise ValueError("Profile description must be a string.")
    validate_profile_settings(raw.get("settings"))
    return ConfigurationProfile(raw["name"], raw.get("description", ""), raw["settings"])


def apply_profile(args: argparse.Namespace, argv: list[str] | None = None) -> argparse.Namespace:
    """Apply a shared profile only to settings supported by this CLI command."""
    tokens = list(sys.argv[1:] if argv is None else argv)
    explicit = {
        token.split("=", 1)[0][2:].replace("-", "_")
        for token in tokens if token.startswith("--")
    }
    explicit.update(name[3:] for name in tuple(explicit) if name.startswith("no_"))
    if getattr(args, "profile", None):
        profile = load_profile(args.profile)
        for name, value in profile.settings.items():
            if hasattr(args, name) and name not in explicit:
                if name == "auto_universe" and "symbols" in explicit:
                    continue
                setattr(args, name, value)
    if hasattr(args, "auto_universe"):
        if "symbols" in explicit and "auto_universe" not in explicit:
            args.auto_universe = False
        elif args.auto_universe is None:
            args.auto_universe = not bool(args.symbols.strip())
        if not args.auto_universe and not args.symbols.strip():
            raise ValueError("Manual mode requires --symbols; otherwise use --auto-universe.")
    return args
