"""Canonical exchange identities shared by live data and stored history."""


def canonical_exchange_id(exchange_id: str, market_type: str = "spot") -> str:
    venue = str(exchange_id).strip().lower()
    if venue == "kucoin" and market_type in {"swap", "future"}:
        return "kucoinfutures"
    if venue == "kucoinfutures" and market_type == "spot":
        raise ValueError("KuCoin Futures cannot supply a spot market.")
    return venue
