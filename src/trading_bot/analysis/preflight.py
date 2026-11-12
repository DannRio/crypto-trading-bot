"""One public-market preflight for requested coins and automated paper entries."""

from dataclasses import fields
from datetime import datetime, timedelta, timezone
import math

from trading_bot.analysis.markets import assess_liquidity
from trading_bot.core.enums import Signal, SetupStatus
from trading_bot.exchanges import get_public_client
from trading_bot.exchanges.routing import canonical_exchange_id
from trading_bot.planning import TradeSetup, revalidate_trade_plan
from trading_bot.scanner.universe import _is_leveraged_base


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def public_market_snapshot(client, symbol, now=None):
    """Keep ticker, book, and separately sourced turnover observation times."""
    clock = now or (lambda: datetime.now(timezone.utc))
    ticker, book, warnings = {}, {}, []
    for method, target in (("fetch_ticker", ticker), ("fetch_order_book", book)):
        try:
            raw = getattr(client, method)(symbol)
            received = clock().isoformat()
            if not isinstance(raw, dict):
                raise ValueError("Invalid public snapshot.")
            target.update(raw)
            target["_received_at"] = received
        except Exception:
            warnings.append(f"Public {method.removeprefix('fetch_')} retrieval failed.")
        if method == "fetch_ticker":
            volume = _number(ticker.get("quoteVolume"))
            if volume is None or volume < 0:
                try:
                    all_tickers = client.fetch_tickers([symbol])
                    received = clock().isoformat()
                    raw = all_tickers.get(symbol) if isinstance(all_tickers, dict) else None
                    if not isinstance(raw, dict):
                        raise ValueError("Volume snapshot is missing.")
                    ticker["_quote_volume_snapshot"] = {
                        "symbol": raw.get("symbol", symbol), "quoteVolume": raw.get("quoteVolume"),
                        "timestamp": raw.get("timestamp"), "datetime": raw.get("datetime"),
                        "_received_at": received, "source_method": "fetch_tickers",
                    }
                except Exception:
                    warnings.append("Separate 24-hour quote-volume retrieval failed.")
    return {"ticker": ticker, "order_book": book, "warnings": warnings}


def entry_price_blockers(plan, liquidity):
    """The public fill estimate must still be inside a valid entry zone."""
    if not plan:
        return ["No complete trade plan is available."]
    side = "buy_fill" if plan["signal"] == Signal.LONG else "sell_fill"
    price = _number((liquidity.get(side) or {}).get("vwap"))
    if price is None:
        return ["An executable-side price estimate is unavailable."]
    direction = 1 if plan["signal"] == Signal.LONG else -1
    cancel = _number(plan.get("cancel_price"))
    if (direction * (price - plan["stop_loss"]) <= 0
            or direction * (price - plan["take_profit_1"]) >= 0
            or (cancel is not None and direction * (price - cancel) <= 0)):
        return ["Price has already reached invalidation or the first target; this entry is no longer valid."]
    if not plan["entry_low"] <= price <= plan["entry_high"]:
        return ["The current executable-side price is outside the planned entry zone."]
    return []


def public_entry_preflight(*, exchange_id, market_type, symbol, setup, account_equity,
                           risk_percent, notional_cap, fee_rate, slippage_bps,
                           min_blended_net_rr=1.0, now=None, client=None):
    """Return allowed/setup/plan/liquidity/reasons and time-stamped evidence.

    ``risk_percent`` is a fraction, as in the core and paper configurations.
    This operation places no order. The returned setup is usable only with the
    same assumptions and before expires_at; history cannot reproduce this book.
    """
    clock = now or (lambda: datetime.now(timezone.utc))
    result = {"allowed": False, "setup": None, "plan": None, "liquidity": None,
              "reasons": [], "warnings": [], "snapshots": {}, "checked_at": None,
              "expires_at": None, "historical_backtest_eligible": False}
    venue = canonical_exchange_id(exchange_id, market_type)
    try:
        client = client or get_public_client(venue, market_type)
        catalog = client.load_markets(reload=True)
        market = catalog.get(symbol) if isinstance(catalog, dict) else None
        if (not isinstance(market, dict) or market.get("symbol") != symbol
                or market.get("type") != market_type):
            raise ValueError("Market identity unavailable.")
        market = dict(market)
        known_bases = {str(item.get("base") or "").upper() for item in catalog.values()
                       if isinstance(item, dict)}
        market["_analysis_leveraged_base"] = _is_leveraged_base(str(market.get("base") or ""), known_bases)
    except Exception:
        result["reasons"] = ["Exact active market metadata is unavailable for entry preflight."]
        return result
    snapshot = public_market_snapshot(client, symbol, clock)
    checked_at = clock()
    result["checked_at"] = checked_at.isoformat()
    result["snapshots"] = {key: snapshot[key] for key in ("ticker", "order_book")}
    result["warnings"] = snapshot["warnings"]
    liquidity = assess_liquidity(market, snapshot["ticker"], snapshot["order_book"], notional_cap, checked_at)
    result["liquidity"] = liquidity
    result["warnings"].extend(liquidity.get("warnings", []))
    if liquidity["status"] != "pass":
        result["reasons"].extend(liquidity.get("reasons") or ["Required liquidity checks failed."])
    plan, errors = revalidate_trade_plan(
        setup, account_equity=account_equity, risk_percent=risk_percent,
        notional_cap=notional_cap, fee_rate=fee_rate, slippage_bps=slippage_bps,
        market_type=market_type, market=market, liquidity=liquidity,
        min_target_net_rr=1, min_blended_net_rr=min_blended_net_rr,
    )
    result["plan"] = plan
    result["reasons"].extend(errors)
    if market_type == "spot" and plan and plan.get("signal") == Signal.SHORT:
        result["reasons"].append("Spot cannot open a short position in this workflow.")
    result["reasons"].extend(entry_price_blockers(plan, liquidity))
    deadline = checked_at + timedelta(seconds=120)
    for item in liquidity.get("freshness", {}).values():
        timestamp = item.get("timestamp")
        if timestamp:
            try:
                stamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                deadline = min(deadline, stamp + timedelta(seconds=120))
            except (ValueError, TypeError):
                result["reasons"].append("Market snapshot expiry could not be verified.")
    result["expires_at"] = deadline.isoformat()
    if deadline <= clock():
        result["reasons"].append("Market evidence expired during entry preflight.")
    if not result["reasons"] and plan:
        names = {item.name for item in fields(TradeSetup)}
        values = {key: value for key, value in plan.items() if key in names}
        values["signal"] = Signal(values["signal"])
        values["status"] = SetupStatus(values["status"])
        values["reasons"] = tuple(values.get("reasons") or ())
        try:
            result["setup"] = TradeSetup(**values)
            result["allowed"] = True
        except (TypeError, ValueError):
            result["reasons"].append("A complete persistable trade setup is required for entry.")
    return result
