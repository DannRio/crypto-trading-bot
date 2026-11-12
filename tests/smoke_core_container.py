"""Installed-image lifecycle acceptance, with no test-framework dependency.

Run inside the installed image: python /tests/smoke_core_container.py
Only installed application/runtime dependencies and the standard library are
used. Every market provider is deterministic; all network calls are forbidden.
The real runtime, service, broker, SQLite journal, and process lease are used.
"""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, RLock
import time
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

from trading_bot.core.enums import MarketRegime, Signal, SetupStatus
from trading_bot.paper.health import paper_health
from trading_bot.paper.journal import PaperJournal
from trading_bot.paper.locking import PaperAccountInUseError, writer_active
from trading_bot.paper.runtime import BotSettings, PaperRuntime
from trading_bot.paper.service import PaperTradingService
from trading_bot.planning import TradeSetup


SYMBOL = "BTC/USDT"
START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def trade_setup():
    return TradeSetup(
        signal=Signal.LONG, status=SetupStatus.CONFIRMED, score=80,
        reference_entry=100, entry_low=99, entry_high=101, stop_loss=95,
        take_profit_1=105, take_profit_2=110,
        reward_to_risk_1=1, reward_to_risk_2=2,
        position_size=2, notional_value=200, target_risk_amount=10,
        actual_risk_amount=10, reasons=("deterministic packaged-runtime smoke",),
        event_id="packaged-runtime-event-1", cancel_price=95,
    )


class MarketFixture:
    def __init__(self):
        self.lock = RLock()
        self.stage = 0

    def advance(self):
        with self.lock:
            self.stage += 1

    def now(self):
        with self.lock:
            return START + timedelta(hours=400 + self.stage)

    def candles(self, **kwargs):
        with self.lock:
            count = 401 + self.stage
        rows = []
        for index in range(count):
            prices = (96, 96, 94, 94) if index >= 401 else (100, 101, 99, 100)
            rows.append({"timestamp": START + timedelta(hours=index),
                         **dict(zip(("open", "high", "low", "close"), prices)), "volume": 1000})
        return pd.DataFrame(rows)

    def preflight(self, **kwargs):
        checked = self.now()
        return {"allowed": True, "setup": trade_setup(), "plan": None, "liquidity": None,
                "reasons": [], "warnings": [], "checked_at": checked.isoformat(),
                "expires_at": (checked + timedelta(minutes=2)).isoformat(),
                "snapshots": {"ticker": {"last": 100, "source": "local fixture"}},
                "historical_backtest_eligible": False}


class FixtureScanner:
    def __init__(self, **kwargs):
        pass

    def scan(self, symbols, *, cancel=None):
        return [SimpleNamespace(symbol=symbol, timeframe="1h", error=None,
                    assessment=SimpleNamespace(status=SetupStatus.CONFIRMED, signal=Signal.LONG,
                                               strategy="trend_continuation_v2"),
                    setup=trade_setup(), structure=SimpleNamespace(regime=MarketRegime.BULL_TREND),
                    confirmations=()) for symbol in symbols]


def wait_for(runtime, predicate, description, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = runtime.status()
        if predicate(status):
            return status
        if status["state"] == "error":
            raise AssertionError(f"Runtime failed while {description}: {status['last_error']}")
        Event().wait(.01)
    raise AssertionError(f"Timed out while {description}: {runtime.status()}")


def run_acceptance():
    market = MarketFixture()
    settings = BotSettings(symbols=(SYMBOL,), fee_rate=0, slippage_bps=0)

    def create_service(*, config, journal):
        return PaperTradingService(config=replace(config, interval_seconds=.02), journal=journal,
            data_fetcher=market.candles, scanner_factory=FixtureScanner,
            utc_now=market.now, entry_preflight=market.preflight)

    with TemporaryDirectory(prefix="core-runtime-smoke-") as directory:
        path = Path(directory) / "account.db"
        runtime = PaperRuntime(path, service_factory=create_service, now=market.now)
        try:
            runtime.start(settings)
            wait_for(runtime, lambda state: (state.get("account") or {}).get("pending_orders") == 1,
                     "creating the first simulated entry")
            require(writer_active(path) is True, "Running worker did not own its writer lease")
            require(paper_health(path, now=market.now())["healthy"], "Successful worker was not healthy")
            try:
                extra = PaperJournal(path)
            except PaperAccountInUseError:
                pass
            else:
                extra.close()
                raise AssertionError("A second writer opened an active account")
            runtime.stop()
            wait_for(runtime, lambda state: not state["active"] and state["state"] == "stopped", "stopping with a pending order")
            require(writer_active(path) is False, "Stop did not release its writer lease")
            with PaperJournal.open_readonly(path) as journal:
                require(len(journal.active_orders()) == 1, "Stop lost its pending entry")
                first_order_id = journal.active_orders().iloc[0]["order_id"]

            runtime.start(settings)
            wait_for(runtime, lambda state: state["health"] == "ok" and
                     (state.get("account") or {}).get("pending_orders") == 1, "restoring the pending entry")
            market.advance()
            wait_for(runtime, lambda state: (state.get("account") or {}).get("open_positions") == 1,
                     "filling the entry from a fully available candle")
            market.advance()
            wait_for(runtime, lambda state: (state.get("account") or {}).get("realized_net_pnl") == -10,
                     "closing the position at its simulated stop")
            runtime.stop()
            wait_for(runtime, lambda state: not state["active"] and state["state"] == "stopped", "saving the closed trade")
            with PaperJournal.open_readonly(path) as journal:
                trades = journal.all_trades()
                require(len(trades) == 1, "Terminal trade was missing or duplicated")
                require(trades.iloc[0]["order_id"] == first_order_id, "Restart changed the entry identity")
                require(float(trades.iloc[0]["net_pnl"]) == -10, "Journal P&L was incorrect")
                require(journal.active_orders().empty, "Closed entry remained active")

            runtime.start(settings)
            recovered = wait_for(runtime, lambda state: state["health"] == "ok" and
                                 (state.get("account") or {}).get("realized_net_pnl") == -10,
                                 "restoring realized P&L from the same journal")
            require(recovered["account"]["equity"] == 9990, "Restart changed the account equity")
            require(recovered["account"]["open_positions"] == 0, "Restart recreated a closed position")
            require(recovered["account"]["pending_orders"] == 0, "Restart duplicated an old setup event")
            runtime.stop()
            wait_for(runtime, lambda state: not state["active"] and state["state"] == "stopped", "finishing the final stop")
            require(writer_active(path) is False, "Final stop left account ownership behind")
            require(paper_health(path, now=market.now())["status"] == "stopped", "Stopped worker remained healthy")
            with PaperJournal.open_readonly(path) as journal:
                require(len(journal.all_trades()) == 1, "Restart applied a terminal candle more than once")
            return {"result": "passed", "runtime_starts": 3, "completed_trades": 1,
                    "realized_net_pnl": -10, "equity_after_restart": 9990,
                    "writer_collision_rejected": True, "lease_released": True,
                    "public_network_used": False}
        finally:
            require(runtime.shutdown(timeout=10), "Smoke worker failed to stop cleanly")


if __name__ == "__main__":
    with patch("socket.getaddrinfo", side_effect=AssertionError("Network is forbidden in this smoke test")), \
         patch("socket.socket.connect", side_effect=AssertionError("Network is forbidden in this smoke test")):
        print(json.dumps(run_acceptance(), sort_keys=True))
