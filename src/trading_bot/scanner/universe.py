from dataclasses import dataclass, field, replace
import math
import re

from trading_bot.exchanges import CCXTClient


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
    r"(?:[235]L|[235]S|UP|DOWN|BULL|BEAR)$",
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

    def __post_init__(self):
        if self.max_markets <= 0:
            raise ValueError("max_markets must be positive.")
        if self.min_quote_volume < 0:
            raise ValueError("min_quote_volume cannot be negative.")
        if (
            self.max_spread_bps is not None
            and self.max_spread_bps <= 0
        ):
            raise ValueError("max_spread_bps must be positive.")
        if not self.quote_currencies:
            raise ValueError("At least one quote currency is required.")
        if self.min_history_candles < 0:
            raise ValueError("min_history_candles cannot be negative.")
        if self.max_history_probes <= 0:
            raise ValueError("max_history_probes must be positive.")
        if (
            self.min_history_candles > 0
            and self.max_history_probes < self.max_markets
        ):
            raise ValueError(
                "max_history_probes must be at least max_markets "
                "when history validation is enabled."
            )
        if not self.history_timeframe.strip():
            raise ValueError("history_timeframe cannot be empty.")


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
    except (TypeError, ValueError):
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
        return base_volume * last

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

    midpoint = (bid + ask) / 2
    return ((ask - bid) / midpoint) * 10_000


def _market_type_matches(
    market: dict,
    market_type: str,
) -> bool:
    if market.get("type") == market_type:
        return True
    return bool(market.get(market_type))


def _is_leveraged_base(base: str) -> bool:
    return bool(LEVERAGED_BASE_PATTERN.search(base.upper()))


class DynamicMarketUniverse:
    def __init__(
        self,
        exchange_id: str,
        market_type: str,
        client: CCXTClient | None = None,
    ):
        self.exchange_id = exchange_id
        self.market_type = market_type
        self.client = client or CCXTClient(
            exchange_id=exchange_id,
            market_type=market_type,
        )

    def _fetch_tickers(
        self,
        symbols: list[str],
    ) -> tuple[dict, tuple[str, ...]]:
        warnings: list[str] = []

        try:
            tickers = self.client.fetch_tickers(symbols)
            return tickers or {}, ()
        except Exception as exc:
            warnings.append(
                "Bulk ticker discovery unavailable; falling back to "
                "rate-limited per-market ticker requests."
            )
            tickers = {}
            for symbol in symbols:
                try:
                    ticker = self.client.fetch_ticker(symbol)
                    if ticker:
                        tickers[symbol] = ticker
                except Exception:
                    continue
            if not tickers:
                raise RuntimeError(
                    "Could not fetch ticker liquidity for any "
                    "eligible market."
                ) from exc
            return tickers, tuple(warnings)

    def _history_validated(
        self,
        candidates: list[MarketCandidate],
        config: UniverseConfig,
        rejected: dict[str, int],
        warnings: list[str],
    ) -> list[MarketCandidate]:
        if config.min_history_candles <= 0:
            return candidates[: config.max_markets]

        selected: list[MarketCandidate] = []
        probes = 0

        for candidate in candidates:
            if len(selected) >= config.max_markets:
                break
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
                    limit=config.min_history_candles,
                )
            except Exception:
                rejected["history_unavailable"] += 1
                continue

            history_count = len(candles or [])
            if history_count < config.min_history_candles:
                rejected["insufficient_history"] += 1
                continue

            selected.append(
                replace(
                    candidate,
                    history_candles=history_count,
                )
            )

        return selected

    def select(
        self,
        config: UniverseConfig | None = None,
    ) -> UniverseSelection:
        config = config or UniverseConfig(
            market_type=self.market_type,
        )
        if config.market_type != self.market_type:
            raise ValueError(
                "UniverseConfig market_type must match selector market_type."
            )

        markets = self.client.load_markets()
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
        }

        eligible: dict[str, dict] = {}
        quotes = {
            quote.upper()
            for quote in config.quote_currencies
        }

        for market in markets.values():
            if not _market_type_matches(
                market,
                config.market_type,
            ):
                rejected["market_type"] += 1
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
            if (
                config.exclude_stablecoin_bases
                and base in STABLE_BASES
            ):
                rejected["stable_base"] += 1
                continue

            if (
                config.exclude_leveraged_tokens
                and _is_leveraged_base(base)
            ):
                rejected["leveraged_token"] += 1
                continue

            eligible[str(symbol)] = market

        tickers, ticker_warnings = self._fetch_tickers(
            list(eligible)
        )
        warnings = list(ticker_warnings)

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
            )
        )
        return UniverseSelection(
            candidates=selected,
            total_markets=total_markets,
            eligible_markets=len(eligible),
            warnings=tuple(warnings),
            rejected=rejected,
        )
