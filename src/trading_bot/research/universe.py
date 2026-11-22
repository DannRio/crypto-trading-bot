"""Frozen, auditable current-market selections for repeatable research batches."""
from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re

from trading_bot.scanner.universe import UniverseConfig, UniverseSelection


SELECTION_WARNING = (
    "This is a current exchange universe, not historical membership. "
    "Backtests on these assets are subject to survivorship and selection bias. "
    "A frozen list makes the selection reproducible; it does not remove that bias."
)
_SYMBOL = re.compile(r"^[A-Z0-9._-]+/[A-Z0-9._-]+(?::[A-Z0-9._-]+)?$")
_EXCHANGE = re.compile(r"^[a-z][a-z0-9_-]{0,49}$")


def _digest(document: dict) -> str:
    payload = {key: value for key, value in document.items() if key != "snapshot_sha256"}
    return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                             allow_nan=False).encode("utf-8")).hexdigest()


def save_universe_snapshot(
    path: str | Path,
    exchange_id: str,
    config: UniverseConfig,
    selection: UniverseSelection,
    selected_at: datetime | None = None,
) -> dict:
    """Write a new immutable snapshot. Existing experiments are never overwritten."""
    stamp = selected_at or datetime.now(timezone.utc)
    if stamp.tzinfo is None:
        raise ValueError("Universe selection time must include a timezone.")
    document = {
        "schema_version": 1,
        "exchange_id": exchange_id,
        "market_type": config.market_type,
        "selected_at_utc": stamp.astimezone(timezone.utc).isoformat(),
        "selection_basis": "current_exchange_universe",
        "warning": SELECTION_WARNING,
        "filters": asdict(config),
        "symbols": selection.symbols,
        "selection": asdict(selection),
    }
    # JSON normalization makes tuple/list representation independent of caller.
    document = json.loads(json.dumps(document, allow_nan=False))
    document["snapshot_sha256"] = _digest(document)
    _validate(document)
    payload = json.dumps(document, indent=2, allow_nan=False) + "\n"
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("x", encoding="utf-8") as output:
        output.write(payload)
    return document


def _validate(document: dict) -> None:
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise ValueError("Unsupported universe snapshot schema; expected version 1.")
    if not _EXCHANGE.fullmatch(str(document.get("exchange_id", ""))):
        raise ValueError("Universe snapshot has an invalid exchange_id.")
    if document.get("market_type") not in {"spot", "swap", "future"}:
        raise ValueError("Universe snapshot has an invalid market_type.")
    symbols = document.get("symbols")
    if (not isinstance(symbols, list) or not symbols or len(symbols) > 500
            or any(not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol) for symbol in symbols)
            or len(set(symbols)) != len(symbols)):
        raise ValueError("Universe snapshot requires 1–500 distinct CCXT symbols.")
    try:
        stamp = datetime.fromisoformat(document["selected_at_utc"])
        if stamp.tzinfo is None:
            raise ValueError("missing timezone")
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Universe snapshot has an invalid selection timestamp.") from exc
    if document.get("selection_basis") != "current_exchange_universe":
        raise ValueError("Universe snapshot must identify current exchange selection.")
    if not isinstance(document.get("filters"), dict) or not isinstance(document.get("selection"), dict):
        raise ValueError("Universe snapshot is missing filter or selection provenance.")
    candidates = document["selection"].get("candidates", [])
    if not isinstance(candidates, list) or any(not isinstance(item, dict) for item in candidates):
        raise ValueError("Universe snapshot contains invalid candidate provenance.")
    if [candidate.get("symbol") for candidate in candidates] != symbols:
        raise ValueError("Universe snapshot symbols do not match its candidate provenance.")
    if document["filters"].get("market_type") != document["market_type"]:
        raise ValueError("Universe snapshot market type and filters disagree.")
    if document.get("snapshot_sha256") != _digest(document):
        raise ValueError("Universe snapshot checksum mismatch; use the original frozen file.")


def load_universe_snapshot(path: str | Path) -> dict:
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    _validate(document)
    return document
