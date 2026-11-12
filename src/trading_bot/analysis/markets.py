"""Resolve a requested asset and assess the market actually being analyzed.

This module does not place orders.  Depth estimates describe one public snapshot,
not guaranteed fills.  A derivative listing is not a check of account eligibility.
"""

from datetime import datetime, timezone
import math

from trading_bot.exchanges import get_public_client
from trading_bot.exchanges.routing import canonical_exchange_id
from trading_bot.scanner.universe import STABLE_BASES, _is_leveraged_base


DEFAULT_LIQUIDITY_LIMITS = {
    "min_quote_volume": 1_000_000.0,
    "max_spread_bps": 40.0,
    "max_slippage_bps": 50.0,
    "max_age_seconds": 120.0,
    "max_future_seconds": 5.0,
    "depth_band_bps": 100.0,
}


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _key(value):
    return " ".join(str(value or "").split()).casefold()


def _safe_data(value):
    """Copy public metadata without non-JSON numbers or mutable client aliases."""
    if isinstance(value, dict):
        return {str(k): _safe_data(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_data(v) for v in value]
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if value is None or isinstance(value, (str, int, bool)):
        return value
    return None


def data_exchange_id(exchange_id, market_type):
    """KuCoin's spot and contract adapters have distinct CCXT identities."""
    exchange_id = str(exchange_id).strip().lower()
    if market_type not in {"spot", "swap"}:
        raise ValueError("Coin analysis supports spot or perpetual swap markets.")
    return canonical_exchange_id(exchange_id, market_type)


def _currency_names(currency):
    if not isinstance(currency, dict):
        return set()
    info = currency.get("info")
    info = info if isinstance(info, dict) else {}
    return {
        str(value).strip()
        for value in (currency.get("name"), info.get("fullName"), info.get("full_name"))
        if isinstance(value, str) and value.strip()
    }


def resolve_market(query, exchange_id="kucoin", market_type="spot", quote="USDT", client=None):
    """Resolve an exact name, ticker, native ID, or pair against live metadata.

    No fuzzy match is silently selected.  Multiple matches return candidates.
    Names come from the exchange's currency metadata, not a hand-maintained list.
    ``market`` is copied CCXT metadata and ``data_exchange_id`` must also be used
    for the scanner so a perpetual is never analyzed using its spot candle feed.
    """
    if not isinstance(query, str) or not query.strip() or len(query) > 120 or not query.isprintable():
        raise ValueError("Enter a coin name, ticker, or trading pair (up to 120 characters).")
    if not isinstance(quote, str) or not quote.strip() or len(quote) > 20:
        raise ValueError("A quote currency is required.")
    venue = data_exchange_id(exchange_id, market_type)
    quote = quote.strip().upper()
    result = {
        "status": "unavailable", "query": query.strip(), "exchange_id": exchange_id,
        "data_exchange_id": venue, "market_type": market_type, "quote": quote,
        "market": None, "coin_name": None, "candidates": [], "reasons": [],
        "warnings": [], "short_supported": False,
    }
    try:
        client = client or get_public_client(venue, market_type)
        markets = client.load_markets(reload=True)
    except Exception:
        result["reasons"].append("Exchange market metadata is unavailable; no instrument was selected.")
        return result
    if not isinstance(markets, dict) or not markets:
        result["reasons"].append("The exchange returned no usable market metadata.")
        return result

    raw_exchange = getattr(client, "exchange", None)
    currencies = getattr(raw_exchange, "currencies", None)
    currencies = currencies if isinstance(currencies, dict) else {}
    try:
        fetched = client.fetch_currencies()
        if isinstance(fetched, dict) and fetched:
            currencies = {**currencies, **fetched}
    except Exception:
        result["warnings"].append("Currency-name refresh was unavailable; loaded exchange metadata was used.")
    # KuCoin Futures does not expose the full spot currency-name catalog.  Only
    # use this companion on a real KuCoin adapter, never for an injected fake.
    if venue == "kucoinfutures" and getattr(raw_exchange, "id", None) == "kucoinfutures":
        try:
            names = get_public_client("kucoin", "spot").fetch_currencies()
            if isinstance(names, dict):
                currencies = {**currencies, **names}
        except Exception:
            result["warnings"].append("KuCoin's full-name catalog is unavailable; use an exact ticker or pair.")

    wanted = _key(query)
    matches = []
    available_quotes = set()
    for raw in markets.values():
        if not isinstance(raw, dict):
            continue
        actual_type = raw.get("type")
        if (actual_type is not None and actual_type != market_type) or (
            actual_type is None and raw.get(market_type) is not True
        ):
            continue
        symbol, base, currency_quote = raw.get("symbol"), raw.get("base"), raw.get("quote")
        if not all(isinstance(item, str) and item for item in (symbol, base, currency_quote)):
            continue
        currency = currencies.get(base) or currencies.get(raw.get("baseId")) or {}
        names = _currency_names(currency)
        info = raw.get("info") if isinstance(raw.get("info"), dict) else {}
        if isinstance(info.get("baseCurrencyName"), str):
            names.add(info["baseCurrencyName"])
        aliases = {_key(symbol), _key(raw.get("id")), _key(base), _key(raw.get("baseId")),
                   _key(f"{base}/{currency_quote}"), _key(f"{base}-{currency_quote}")}
        aliases.update(_key(name) for name in names)
        aliases.discard("")
        if wanted not in aliases:
            continue
        available_quotes.add(currency_quote.upper())
        if currency_quote.upper() != quote:
            continue
        name = next((name for name in sorted(names) if _key(name) != _key(base)), base)
        matches.append((raw, name, currency))

    # Defensive deduplication for adapters whose mapping has aliases for a market.
    matches = list({item[0]["symbol"]: item for item in matches}.values())
    matches.sort(key=lambda item: item[0]["symbol"])
    result["candidates"] = [
        {"symbol": raw["symbol"], "base": raw["base"], "quote": raw["quote"],
         "market_type": market_type, "name": name, "active": raw.get("active"),
         "settle": raw.get("settle"), "id": raw.get("id")}
        for raw, name, currency in matches[:20]
    ]
    if not matches:
        reason = "No exact listed name, ticker, or pair matched this market type and quote currency."
        if available_quotes:
            reason += " Matching markets use: " + ", ".join(sorted(available_quotes)) + "."
        result["reasons"].append(reason)
        return result
    if len(matches) > 1:
        result["status"] = "ambiguous"
        result["reasons"].append("The name matches multiple instruments; select an exact trading pair.")
        return result
    market, name, currency = matches[0]
    copied_market = _safe_data(market)
    known_bases = {str(value.get("base") or "").upper() for value in markets.values() if isinstance(value, dict)}
    copied_market["_analysis_leveraged_base"] = _is_leveraged_base(market["base"], known_bases)
    result.update({
        "status": "resolved", "market": copied_market, "coin_name": name,
        "currency": _safe_data(currency),
        "short_supported": market_type == "swap" and market.get("linear") is True
        and market.get("inverse") is not True and market.get("settle") == market.get("quote")
        and market.get("active") is True,
    })
    return result


def _time_ms(value):
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return None
        try:
            return _number(value.timestamp() * 1000)
        except (ValueError, OverflowError, OSError):
            return None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                return _number(parsed.timestamp() * 1000)
        except (ValueError, OverflowError, OSError):
            pass
    return _number(value)


def _iso(timestamp):
    try:
        return datetime.fromtimestamp(timestamp / 1000, timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError):
        return None


def _freshness(snapshot, label, now_ms, limits):
    exchange_time = snapshot.get("timestamp")
    if exchange_time is None:
        exchange_time = snapshot.get("datetime")
    # The caller can attach receipt time after a successful synchronous fetch.
    # A receipt never overrides a known old/invalid exchange timestamp.
    receipt = snapshot.get("_received_at", snapshot.get("received_at"))
    use_receipt = exchange_time is None
    timestamp = _time_ms(receipt if use_receipt else exchange_time)
    provenance = {
        "source": "receipt" if use_receipt else "exchange",
        "timestamp": _iso(timestamp) if timestamp is not None else None,
        "age_seconds": (now_ms - timestamp) / 1000 if timestamp is not None else None,
    }
    if timestamp is None or not provenance["timestamp"]:
        return provenance, f"{label} freshness is unknown.", None
    age = provenance["age_seconds"]
    if age < -limits["max_future_seconds"]:
        return provenance, f"{label} timestamp is in the future.", None
    if age > limits["max_age_seconds"]:
        return provenance, f"{label} is stale.", None
    warning = f"{label} has no exchange timestamp; freshness is based on bounded response receipt only." if use_receipt else None
    return provenance, None, warning


def _book_levels(raw_levels, descending, multiplier):
    if not isinstance(raw_levels, (list, tuple)) or not raw_levels or len(raw_levels) > 5000:
        raise ValueError("Order-book sides must contain a bounded, nonempty list of levels.")
    levels = []
    previous = None
    for row in raw_levels:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            raise ValueError("Malformed order-book level.")
        price, size = _number(row[0]), _number(row[1])
        if price is None or size is None or price <= 0 or size <= 0:
            raise ValueError("Order-book prices and quantities must be finite and positive.")
        if previous is not None and (price >= previous if descending else price <= previous):
            raise ValueError("Order-book levels are duplicated or out of order.")
        quantity = _number(size * multiplier)
        if quantity is None or quantity <= 0 or _number(price * quantity) is None:
            raise ValueError("Order-book amounts exceed the supported numeric range.")
        levels.append((price, quantity))
        previous = price
    return levels


def _fill(levels, quantity, midpoint, buy):
    remaining, filled, cost = quantity, 0.0, 0.0
    for price, available in levels:
        amount = min(remaining, available)
        filled += amount
        cost += amount * price
        remaining -= amount
        if remaining <= quantity * 1e-12:
            break
    if not math.isfinite(cost) or not math.isfinite(filled):
        raise ValueError("Estimated fill exceeds the supported numeric range.")
    complete = remaining <= quantity * 1e-12
    vwap = cost / filled if filled else None
    slip = ((vwap / midpoint - 1) if buy else (1 - vwap / midpoint)) * 10_000 if vwap else None
    if filled and (vwap is None or vwap <= 0 or not math.isfinite(vwap) or slip is None or not math.isfinite(slip)):
        raise ValueError("Estimated fill price or slippage exceeds the supported numeric range.")
    return {"complete": complete, "vwap": vwap, "slippage_bps": slip,
            "base_quantity": filled, "requested_base_quantity": quantity, "quote_value": cost}


def assess_liquidity(market, ticker, order_book, notional, as_of, limits=None):
    """Check both sides of a public book for a proposed quote-currency notional.

    ``as_of`` is a timezone-aware datetime, ISO timestamp, or UTC milliseconds.
    Snapshot ``timestamp`` follows CCXT milliseconds.  If an adapter omits it,
    the caller may attach ``_received_at`` immediately after the network response.
    Receipt-based freshness is disclosed and cannot override a stale source time.
    Futures quantities are contracts and require a positive ``contractSize``.
    """
    settings = dict(DEFAULT_LIQUIDITY_LIMITS)
    if limits is not None:
        if not isinstance(limits, dict) or set(limits) - settings.keys():
            raise ValueError("Unknown liquidity limit.")
        settings.update(limits)
    for name, value in settings.items():
        parsed = _number(value)
        if parsed is None or parsed < 0 or (parsed == 0 and name != "min_quote_volume"):
            raise ValueError(f"{name} must be a finite positive number.")
        settings[name] = parsed
    amount, now_ms = _number(notional), _time_ms(as_of)
    if amount is None or amount <= 0:
        raise ValueError("Analysis notional must be finite and positive.")
    if now_ms is None or not _iso(now_ms):
        raise ValueError("A valid UTC analysis timestamp is required.")
    result = {
        "status": "unavailable", "reasons": [], "warnings": [],
        "as_of": _iso(now_ms), "notional": amount, "limits": settings,
        "notional_basis": "base quantity valued at the current book midpoint",
        "spread_bps": None, "quote_volume": None, "quote_volume_source": None,
        "best_bid": None, "best_ask": None,
        "bid_depth_quote": None, "ask_depth_quote": None,
        "buy_fill": None, "sell_fill": None, "freshness": {},
    }
    if not isinstance(market, dict) or not market.get("symbol"):
        result["reasons"].append("Instrument metadata is unavailable.")
        return result
    result["quote"] = market.get("quote")
    failures, unavailable = [], []
    if market.get("active") is False:
        failures.append("The selected instrument is inactive.")
    elif market.get("active") is not True:
        unavailable.append("The instrument's active trading status is unknown.")
    base = str(market.get("base") or "").upper()
    if base in STABLE_BASES or base in {"USD1", "USDG", "USDS", "USDD"}:
        failures.append("Stablecoin bases are excluded from directional trade analysis.")
    # The resolver computes suffix exclusions against actual exchange bases.
    # Without that catalog, only unambiguous numeric leverage suffixes apply.
    if market.get("_analysis_leveraged_base") is True or _is_leveraged_base(base, set()):
        failures.append("Leveraged tokens are excluded from directional trade analysis.")
    contract = market.get("contract") is True or market.get("type") in {"swap", "future"}
    multiplier = 1.0
    if contract:
        if (market.get("type") != "swap" and market.get("swap") is not True) or market.get("expiry") is not None:
            failures.append("Only perpetual contracts are supported by this analysis profile.")
        if market.get("linear") is not True or market.get("inverse") is True or market.get("settle") != market.get("quote"):
            failures.append("Only explicitly linear, quote-settled contracts are supported.")
        multiplier = _number(market.get("contractSize"))
        if multiplier is None or multiplier <= 0:
            unavailable.append("Contract quantity multiplier is unavailable or invalid.")
    elif market.get("type") != "spot" and market.get("spot") is not True:
        unavailable.append("The selected market type is unknown.")
    if failures or unavailable:
        result["status"] = "unavailable" if unavailable else "fail"
        result["reasons"] = failures + unavailable
        return result
    if not isinstance(ticker, dict) or not isinstance(order_book, dict):
        result["reasons"].append("A ticker and order-book snapshot are required.")
        return result
    for snapshot, label in ((ticker, "Ticker"), (order_book, "Order book")):
        if snapshot.get("symbol") is not None and snapshot["symbol"] != market["symbol"]:
            unavailable.append(f"{label} belongs to a different instrument.")
        freshness, error, warning = _freshness(snapshot, label, now_ms, settings)
        result["freshness"]["ticker" if label == "Ticker" else "order_book"] = freshness
        if error:
            unavailable.append(error)
        if warning:
            result["warnings"].append(warning)
    quote_volume = _number(ticker.get("quoteVolume"))
    if quote_volume is not None and quote_volume >= 0:
        result["quote_volume_source"] = {"source_method": "fetch_ticker", "freshness_key": "ticker"}
    else:
        separate = ticker.get("_quote_volume_snapshot")
        if isinstance(separate, dict):
            if separate.get("symbol") != market["symbol"]:
                unavailable.append("Quote-volume snapshot belongs to a different instrument.")
            freshness, error, warning = _freshness(separate, "Quote volume", now_ms, settings)
            result["freshness"]["quote_volume"] = freshness
            result["quote_volume_source"] = {
                "source_method": separate.get("source_method"), "freshness_key": "quote_volume",
                "observation_time_known": freshness["source"] == "exchange" and error is None,
            }
            if error:
                unavailable.append(error)
            if warning:
                result["warnings"].append(warning)
                result["warnings"].append("Separately retrieved 24-hour volume has no known exchange observation time.")
            quote_volume = _number(separate.get("quoteVolume"))
    result["quote_volume"] = quote_volume
    if quote_volume is None or quote_volume < 0:
        unavailable.append("A finite positive 24-hour quote volume is unavailable.")
    elif quote_volume == 0:
        failures.append("The exchange reports zero 24-hour quote volume.")
    elif quote_volume < settings["min_quote_volume"]:
        failures.append("24-hour quote volume is below the configured minimum.")
    try:
        bids = _book_levels(order_book.get("bids"), True, multiplier)
        asks = _book_levels(order_book.get("asks"), False, multiplier)
        bid, ask = bids[0][0], asks[0][0]
        if ask < bid:
            raise ValueError("The order book is crossed.")
        midpoint = bid / 2 + ask / 2
        spread = (ask - bid) / midpoint * 10_000
        result.update({"best_bid": bid, "best_ask": ask, "spread_bps": spread})
        band = settings["depth_band_bps"] / 10_000
        result["bid_depth_quote"] = sum(p * q for p, q in bids if p >= bid * (1 - band))
        result["ask_depth_quote"] = sum(p * q for p, q in asks if p <= ask * (1 + band))
        if not all(math.isfinite(result[key]) for key in ("bid_depth_quote", "ask_depth_quote")):
            result["bid_depth_quote"] = result["ask_depth_quote"] = None
            raise ValueError("Order-book depth exceeds the supported numeric range.")
        quantity = amount / midpoint
        if not math.isfinite(quantity) or quantity <= 0:
            raise ValueError("The requested quantity is outside the supported numeric range.")
        result["buy_fill"] = _fill(asks, quantity, midpoint, True)
        result["sell_fill"] = _fill(bids, quantity, midpoint, False)
        if spread > settings["max_spread_bps"]:
            failures.append("Order-book spread exceeds the configured maximum.")
        for side in ("buy", "sell"):
            fill = result[f"{side}_fill"]
            if not fill["complete"]:
                failures.append(f"Visible {side}-side depth cannot fill the analysis notional.")
            elif fill["slippage_bps"] > settings["max_slippage_bps"]:
                failures.append(f"Estimated {side} slippage exceeds the configured maximum.")
    except (ValueError, TypeError, OverflowError) as exc:
        unavailable.append(str(exc))
    result["reasons"] = unavailable + failures
    result["status"] = "unavailable" if unavailable else "fail" if failures else "pass"
    return result
