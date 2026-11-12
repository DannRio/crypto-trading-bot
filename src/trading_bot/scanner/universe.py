from collections.abc import Callable
from dataclasses import dataclass, field, replace
import math
import re

import pandas as pd

from trading_bot.data.validation import (
    REQUIRED_OHLCV_COLUMNS,
    _timeframe_delta,
    latest_expected_close,
    validate_ohlcv,
)
from trading_bot.exchanges import CCXTClient, get_public_client
from trading_bot.exchanges.routing import canonical_exchange_id


STABLE_BASES = frozenset(
    {
        "USDT",
        "USDC",
        "DAI",
        "TUSD",
        "FDUSD",
        "USDE",
        "PYUSD",
        "USDP",
        "FRAX",
    }
)

LEVERAGED_BASE_PATTERN = re.compile(
    r"[235][LS]$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class UniverseConfig:
    market_type: str = "spot"
    quote_currencies: tuple[str, ...] = ("USDT",)
    max_markets: int = 20
    min_quote_volume: float = 0.0
    max_spread_bps: float | None = None
    active_only: bool = True
    exclude_stablecoin_bases: bool = True
    exclude_leveraged_tokens: bool = True
    min_history_candles: int = 0
    history_timeframe: str = "1h"
    max_history_probes: int = 60
    max_ticker_requests: int = 200
    unique_base_assets: bool = True
    linear_contracts_only: bool = True

    def __post_init__(self):
        for name in ("max_markets", "max_history_probes", "max_ticker_requests"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer.")
        if (
            isinstance(self.min_quote_volume, bool)
            or not isinstance(self.min_quote_volume, (int, float))
            or _finite_float(self.min_quote_volume) is None
            or self.min_quote_volume < 0
        ):
            raise ValueError("min_quote_volume must be finite and non-negative.")
        if (
            self.max_spread_bps is not None
            and (
                isinstance(self.max_spread_bps, bool)
                or not isinstance(self.max_spread_bps, (int, float))
                or _finite_float(self.max_spread_bps) is None
                or self.max_spread_bps <= 0
            )
        ):
            raise ValueError("max_spread_bps must be finite and positive.")
        if isinstance(self.quote_currencies, str) or not self.quote_currencies or any(
            not isinstance(quote, str) or not quote.strip()
            for quote in self.quote_currencies
        ):
            raise ValueError("At least one quote currency is required.")
        if type(self.min_history_candles) is not int or self.min_history_candles < 0:
            raise ValueError("min_history_candles must be a non-negative integer.")
        if (
            self.min_history_candles > 0
            and self.max_history_probes < self.max_markets
        ):
            raise ValueError(
                "max_history_probes must be at least max_markets "
                "when history validation is enabled."
            )
        if not isinstance(self.history_timeframe, str) or not re.fullmatch(r"[1-9][0-9]*[mhdw]", self.history_timeframe):
            raise ValueError("history_timeframe must be a supported timeframe string.")
        _timeframe_delta(self.history_timeframe)
        if self.market_type not in {"spot", "swap", "future"}:
            raise ValueError("market_type must be spot, swap, or future.")
        for name in ("active_only", "exclude_stablecoin_bases", "exclude_leveraged_tokens",
                     "unique_base_assets", "linear_contracts_only"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} must be a boolean.")


@dataclass(frozen=True)
class MarketCandidate:
    symbol: str
    base: str
    quote: str
    market_type: str
    active: bool | None
    quote_volume: float
    last: float | None
    bid: float | None
    ask: float | None
    spread_bps: float | None
    history_candles: int | None = None


@dataclass(frozen=True)
class UniverseSelection:
    candidates: tuple[MarketCandidate, ...]
    total_markets: int
    eligible_markets: int
    warnings: tuple[str, ...] = ()
    rejected: dict[str, int] = field(default_factory=dict)

    @property
    def symbols(self) -> list[str]:
        return [candidate.symbol for candidate in self.candidates]


def _finite_float(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _quote_volume(ticker: dict) -> float | None:
    direct = _finite_float(ticker.get("quoteVolume"))
    if direct is not None and direct > 0:
        return direct

    base_volume = _finite_float(ticker.get("baseVolume"))
    last = _finite_float(
        ticker.get("last", ticker.get("close"))
    )
    if (
        base_volume is not None
        and base_volume > 0
        and last is not None
        and last > 0
    ):
        return _finite_float(base_volume * last)

    return None


def _spread_bps(ticker: dict) -> float | None:
    bid = _finite_float(ticker.get("bid"))
    ask = _finite_float(ticker.get("ask"))
    if (
        bid is None
        or ask is None
        or bid <= 0
        or ask <= 0
        or ask < bid
    ):
        return None

    midpoint = bid / 2 + ask / 2
    return ((ask - bid) / midpoint) * 10_000


def _market_type_matches(
    market: dict,
    market_type: str,
) -> bool:
    if market.get("type") == market_type:
        return True
    return bool(market.get(market_type))


def _is_leveraged_base(base: str, known_bases: set[str]) -> bool:
    base = base.upper()
    if LEVERAGED_BASE_PATTERN.search(base):
        return True
    # A bare suffix is not enough: JUP is an ordinary asset, not "J UP".
    return any(
        base.endswith(suffix) and base[:-len(suffix)] in known_bases
        for suffix in ("UP", "DOWN", "BULL", "BEAR")
    )


def _cancelled(cancel) -> bool:
    """Accept a threading.Event or lightweight cancellation callback."""
    return bool(cancel.is_set() if hasattr(cancel, "is_set") else cancel()) if cancel is not None else False


class DynamicMarketUniverse:
    def __init__(
        self,
        exchange_id: str,
        market_type: str,
        client: CCXTClient | None = None,
        now: Callable | None = None,
    ):
        self.exchange_id = canonical_exchange_id(exchange_id, market_type)
        self.market_type = market_type
        self.now = now or (lambda: pd.Timestamp.now(tz="UTC"))
        self.client = client or get_public_client(
            exchange_id=self.exchange_id,
            market_type=market_type,
        )

    def _fetch_tickers(
        self,
        symbols: list[str],
        max_requests: int,
        cancel=None,
    ) -> tuple[dict, tuple[str, ...]]:
        if not symbols or _cancelled(cancel):
            return {}, ()
        warnings: list[str] = []
        bulk_error = None
        try:
            tickers = self.client.fetch_tickers(symbols)
            tickers = {
                symbol: ticker for symbol, ticker in (tickers or {}).items()
                if symbol in symbols and isinstance(ticker, dict) and ticker
            }
        except Exception as exc:
            bulk_error = exc
            tickers = {}
            warnings.append(
                "Bulk ticker discovery unavailable; falling back to "
                "bounded, rate-limited per-market ticker requests."
            )
        missing = sorted(set(symbols) - tickers.keys())
        if missing and bulk_error is None:
            warnings.append("Bulk ticker response omitted markets; attempting bounded per-market requests.")
        if len(missing) > max_requests:
            warnings.append(
                f"Ticker-request limit reached: probing {max_requests} of {len(missing)} "
                "missing markets in symbol order. Liquidity ranking covers only returned tickers."
            )
        for symbol in missing[:max_requests]:
            if _cancelled(cancel):
                break
            try:
                ticker = self.client.fetch_ticker(symbol)
                if isinstance(ticker, dict) and ticker:
                    tickers[symbol] = ticker
            except Exception:
                continue
        if not tickers and bulk_error is not None and not _cancelled(cancel):
            raise RuntimeError(
                "Could not fetch ticker liquidity for any eligible market."
            ) from bulk_error
        return tickers, tuple(warnings)

    def _history_validated(
        self,
        candidates: list[MarketCandidate],
        config: UniverseConfig,
        rejected: dict[str, int],
        warnings: list[str],
        cancel=None,
    ) -> list[MarketCandidate]:
        selected: list[MarketCandidate] = []
        selected_bases: set[str] = set()
        probes = 0
        as_of = pd.Timestamp(self.now())
        as_of = as_of.tz_localize("UTC") if as_of.tzinfo is None else as_of.tz_convert("UTC")
        delta = _timeframe_delta(config.history_timeframe)

        for candidate in candidates:
            if _cancelled(cancel):
                break
            if len(selected) >= config.max_markets:
                break
            if config.unique_base_assets and candidate.base.upper() in selected_bases:
                rejected["duplicate_base"] += 1
                continue
            if config.min_history_candles <= 0:
                selected.append(candidate)
                selected_bases.add(candidate.base.upper())
                continue
            if probes >= config.max_history_probes:
                warnings.append(
                    "History-probe limit reached before the universe "
                    "could be fully populated."
                )
                break

            probes += 1
            try:
                candles = self.client.fetch_ohlcv(
                    candidate.symbol,
                    timeframe=config.history_timeframe,
                    limit=config.min_history_candles + 1,
                )
            except Exception:
                rejected["history_unavailable"] += 1
                continue

            try:
                # Do not normalize/sort/deduplicate here: corrupt input must fail preflight.
                frame = pd.DataFrame(candles or [], columns=REQUIRED_OHLCV_COLUMNS)
                frame["timestamp"] = pd.to_datetime(
                    pd.to_numeric(frame["timestamp"], errors="coerce"),
                    unit="ms", utc=True, errors="coerce",
                )
                quality = validate_ohlcv(frame, timeframe=config.history_timeframe, min_rows=0)
                if not quality.valid or quality.missing_intervals:
                    rejected["invalid_history"] += 1
                    continue
                if (frame["timestamp"] > as_of).any():
                    rejected["invalid_history"] += 1
                    continue
                closed = frame.loc[frame["timestamp"] + delta <= as_of]
            except (TypeError, ValueError, OverflowError):
                rejected["invalid_history"] += 1
                continue
            history_count = len(closed)
            if history_count < config.min_history_candles:
                rejected["insufficient_history"] += 1
                continue
            expected = latest_expected_close(as_of, config.history_timeframe) - delta
            if closed["timestamp"].iloc[-1] != expected:
                rejected["stale_history"] += 1
                continue

            selected.append(
                replace(
                    candidate,
                    history_candles=history_count,
                )
            )
            selected_bases.add(candidate.base.upper())

        return selected

    def select(
        self,
        config: UniverseConfig | None = None,
        *,
        cancel=None,
    ) -> UniverseSelection:
        config = config or UniverseConfig(
            market_type=self.market_type,
        )
        if config.market_type != self.market_type:
            raise ValueError(
                "UniverseConfig market_type must match selector market_type."
            )

        # The shared public client retains metadata between scans. Discovery is
        # the refresh boundary so new listings and inactive markets are visible.
        if _cancelled(cancel):
            return UniverseSelection((), 0, 0, ("Universe selection cancelled.",))
        markets = self.client.load_markets(reload=True)
        if _cancelled(cancel):
            return UniverseSelection((), len(markets), 0, ("Universe selection cancelled.",))
        total_markets = len(markets)
        rejected = {
            "market_type": 0,
            "quote": 0,
            "inactive": 0,
            "missing_symbol": 0,
            "stable_base": 0,
            "leveraged_token": 0,
            "missing_volume": 0,
            "low_volume": 0,
            "missing_spread": 0,
            "wide_spread": 0,
            "history_unavailable": 0,
            "insufficient_history": 0,
            "invalid_history": 0,
            "stale_history": 0,
            "duplicate_base": 0,
            "unsupported_contract": 0,
            "missing_base": 0,
        }

        eligible: dict[str, dict] = {}
        quotes = {
            quote.strip().upper()
            for quote in config.quote_currencies
        }
        known_bases = {
            str(market.get("base") or "").upper()
            for market in markets.values()
            if market.get("base")
        }

        for market in markets.values():
            if not _market_type_matches(
                market,
                config.market_type,
            ):
                rejected["market_type"] += 1
                continue

            if config.market_type != "spot" and config.linear_contracts_only:
                if (
                    market.get("linear") is not True
                    or market.get("inverse") is True
                    or market.get("settle") != market.get("quote")
                ):
                    rejected["unsupported_contract"] += 1
                    continue

            quote = str(market.get("quote") or "").upper()
            if quote not in quotes:
                rejected["quote"] += 1
                continue

            if (
                config.active_only
                and market.get("active") is False
            ):
                rejected["inactive"] += 1
                continue

            symbol = market.get("symbol")
            if not symbol:
                rejected["missing_symbol"] += 1
                continue

            base = str(market.get("base") or "").upper()
            if not base:
                rejected["missing_base"] += 1
                continue
            if (
                config.exclude_stablecoin_bases
                and base in STABLE_BASES
            ):
                rejected["stable_base"] += 1
                continue

            if (
                config.exclude_leveraged_tokens
                and _is_leveraged_base(base, known_bases)
            ):
                rejected["leveraged_token"] += 1
                continue

            eligible[str(symbol)] = market

        tickers, ticker_warnings = self._fetch_tickers(
            sorted(eligible), config.max_ticker_requests, cancel=cancel,
        )
        warnings = list(ticker_warnings)
        if len(quotes) > 1:
            warnings.append(
                "Quote volumes use each market's quote currency; they are not FX-normalized. "
                "Use a single quote currency for directly comparable liquidity thresholds and ranking."
            )

        candidates: list[MarketCandidate] = []
        for symbol, market in eligible.items():
            ticker = tickers.get(symbol) or {}
            quote_volume = _quote_volume(ticker)
            if quote_volume is None or quote_volume <= 0:
                rejected["missing_volume"] += 1
                continue
            if quote_volume < config.min_quote_volume:
                rejected["low_volume"] += 1
                continue

            spread_bps = _spread_bps(ticker)
            if config.max_spread_bps is not None:
                if spread_bps is None:
                    rejected["missing_spread"] += 1
                    continue
                if spread_bps > config.max_spread_bps:
                    rejected["wide_spread"] += 1
                    continue

            candidates.append(
                MarketCandidate(
                    symbol=symbol,
                    base=str(market.get("base") or ""),
                    quote=str(market.get("quote") or ""),
                    market_type=str(
                        market.get("type")
                        or config.market_type
                    ),
                    active=market.get("active"),
                    quote_volume=quote_volume,
                    last=_finite_float(
                        ticker.get(
                            "last",
                            ticker.get("close"),
                        )
                    ),
                    bid=_finite_float(ticker.get("bid")),
                    ask=_finite_float(ticker.get("ask")),
                    spread_bps=spread_bps,
                )
            )

        candidates.sort(
            key=lambda item: (
                -item.quote_volume,
                item.spread_bps
                if item.spread_bps is not None
                else float("inf"),
                item.symbol,
            )
        )

        selected = tuple(
            self._history_validated(
                candidates,
                config,
                rejected,
                warnings,
                cancel=cancel,
            )
        )
        if _cancelled(cancel):
            selected = ()
            warnings.append("Universe selection cancelled.")
        return UniverseSelection(
            candidates=selected,
            total_markets=total_markets,
            eligible_markets=len(eligible),
            warnings=tuple(warnings),
            rejected=rejected,
        )
