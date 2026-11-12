"""Exercise the paper dashboard with stateful fixtures and no exchange access.

Install Playwright and Chromium, then run:
    python tests/browser/dashboard_smoke.py

PLAYWRIGHT_CHROMIUM_EXECUTABLE can select an existing Chromium executable.
All API requests are intercepted. A failure screenshot is saved as
dashboard-smoke-failure.png. Fixture prices and results are not market signals.
"""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
from threading import Thread
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect, sync_playwright


ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 11, 12, 16, tzinfo=timezone.utc)
UNTRUSTED_TEXT = '<img src=x onerror="window.__providerInjected=true">'
CONFIG = dict(exchange="kucoin", quote="USDT", symbols=[], max_markets=10,
              equity=10_000, risk_percent=0.25, notional=1_000,
              fee_rate=0.001, slippage_bps=5)

# Hold one completed response, not the underlying network request. Releasing it
# after a newer action exercises revisions even when abort cannot cancel a reply.
FETCH_GATE = """() => {
  window.__smokeHold = {};
  window.__smokeRelease = {};
  const original = window.fetch.bind(window);
  window.fetch = (...args) => {
    const path = new URL(args[0], location.href).pathname;
    const held = Boolean(window.__smokeHold[path]);
    delete window.__smokeHold[path];
    const response = original(...args);
    if (!held) return response;
    return response.then(async received => {
      const body = await received.text();
      const buffered = new Response(body, {
        status: received.status, headers: received.headers
      });
      return new Promise(resolve => {
        window.__smokeRelease[path] = () => {
          delete window.__smokeRelease[path];
          resolve(buffered);
        };
      });
    });
  };
}"""


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


def synthetic_paper():
    return dict(
        journal="SYNTHETIC fixture journal",
        metadata={},
        universe=dict(status="READY", symbols=["BTC/USDT", "ETH/USDT"]),
        heartbeat=dict(status="ok", last_cycle_result="ok",
                       last_cycle_finished_at="2026-11-12T15:59:00Z"),
        summary=dict(pending_orders=1, open_positions=1, realized_net_pnl=-40),
        risk_state=dict(last_equity=9_960, evaluated_at="2026-11-12T15:59:00Z",
                        halted=False, halt_reason=None),
        active_orders=[
            dict(order_id="fixture-open", status="FILLED", symbol="BTC/USDT",
                 signal="LONG", fill_price=101, stop_loss=95, effective_stop=102.5,
                 take_profit_1=105, take_profit_2=110, position_size=4,
                 remaining_size=2, tp1_hit=True, filled_at="2026-11-12T13:00:00Z"),
            dict(order_id="fixture-pending", status="PENDING", symbol="ETH/USDT",
                 signal="LONG", entry_low=99, entry_high=101, stop_loss=95,
                 take_profit_1=105, take_profit_2=110, position_size=5,
                 bars_remaining=2),
        ],
        recent_signals=[
            dict(symbol="BTC/USDT", action="SKIPPED",
                 reason="Synthetic open-position decision. " + UNTRUSTED_TEXT,
                 created_at="2026-11-12T15:59:00Z")
        ],
        recent_trades=[
            dict(symbol="SOL/USDT", signal="LONG", entry_price=100,
                 exit_price=96, net_pnl=-40, fees=2, r_multiple=-0.8,
                 exit_reason="STOP_LOSS", exit_time="2026-11-12T12:00:00Z")
        ],
        checkpoints=[],
    )


def synthetic_backtest():
    aggregate = dict(trades=3, net_pnl=25, average_net_r=0.3, profit_factor=1.8)
    return dict(
        symbol="BTC/USDT", exchange="kucoin", market_type="spot", timeframe="1h",
        data_source="local", data_rows=9_160, evaluation_rows=8_760,
        warmup_candles=400, research_variant=False, strategies=["trend_continuation_v2"],
        confirmation_timeframes=["4h"], entry_timeframe="15m",
        execution_timeframe="",
        metrics=dict(initial_equity=10_000, final_equity=10_025, return_pct=0.25,
                     max_drawdown_pct=2.16, total_trades=3, win_rate_pct=66.67,
                     profit_factor=1.8, average_pnl=8.33),
        diagnostics=dict(
            by_year=[dict(year="2025", **aggregate)],
            by_regime=[dict(regime="BULL_TREND", **aggregate)],
            by_score_bucket=[dict(score_bucket="70-79", **aggregate)],
            gross_total=35, fees_total=6, slippage_total=4,
        ),
        execution_diagnostics=dict(ambiguous_bars=2),
        chronological_validation=[
            dict(segment="TRAIN", trades=1, return_pct=0.1, average_r=0.2,
                 profit_factor=1.4),
            dict(segment="VALIDATION", trades=1, return_pct=-0.1, average_r=-0.2,
                 profit_factor=0.8),
            dict(segment="TEST", trades=1, return_pct=0.25, average_r=0.9,
                 profit_factor=1.8),
        ],
        exit_ablation=[
            dict(exit_variant="DEFAULT", trades=3, return_pct=0.25,
                 average_net_r=0.3, profit_factor=1.8)
        ],
        mark_to_market_equity=[
            dict(timestamp="2025-01-01T00:00:00Z", equity=10_000),
            dict(timestamp="2025-06-01T00:00:00Z", equity=9_800),
            dict(timestamp="2025-12-31T23:00:00Z", equity=10_025),
        ],
        recent_trades=[
            dict(signal="LONG", entry_price=100, exit_price=110, pnl=25,
                 tp1_hit=True, exit_reason="TAKE_PROFIT_2")
        ],
        warning="Synthetic historical results only. " + UNTRUSTED_TEXT,
    )


class FixtureAPI:
    """Each explicit mutation changes the state later status reads observe."""

    def __init__(self):
        self.runtime = dict(state="stopped", health="stopped",
                            message="Synthetic worker is stopped.",
                            started_at=None, last_error=None,
                            policy_id="core-spot-v1", config=deepcopy(CONFIG))
        self.paper = synthetic_paper()
        self.capabilities = dict(mode="paper", live_orders=False)
        self.status_error = None
        self.start_error = None
        self.backtest_error = None
        self.backtest_policy_mismatch = False
        self.job_error = None
        self.job = None
        self.starts = []
        self.stops = []
        self.preparations = []
        self.backtests = []
        self.calls = []
        self.unexpected = []

    def envelope(self):
        return deepcopy(dict(runtime=self.runtime, paper=self.paper,
                             capabilities=self.capabilities))

    def route(self, route):
        request = route.request
        url = urlsplit(request.url)
        self.calls.append((request.method, url.path))
        status = 200
        if url.path == "/api/bot/status" and request.method == "GET":
            if self.status_error:
                status, body = 503, {"detail": self.status_error}
            else:
                body = self.envelope()
        elif url.path == "/api/bot/start" and request.method == "POST":
            config = request.post_data_json
            self.starts.append(config)
            if self.start_error:
                status, body = 409, {"detail": self.start_error}
            else:
                self.runtime.update(state="starting", health="starting",
                                    message="Synthetic startup is in progress.",
                                    started_at=NOW.isoformat(), last_error=None,
                                    config=deepcopy(config))
                status, body = 202, dict(runtime=deepcopy(self.runtime),
                                        capabilities=deepcopy(self.capabilities))
        elif url.path == "/api/bot/stop" and request.method == "POST":
            self.stops.append(request.post_data)
            self.runtime.update(state="stopping", health="stopping",
                                message="Synthetic worker is finishing its cycle.")
            status, body = 202, dict(runtime=deepcopy(self.runtime),
                                    capabilities=deepcopy(self.capabilities))
        elif url.path == "/api/history/prepare" and request.method == "POST":
            self.preparations.append(request.post_data_json)
            self.job = dict(job_id=f"00000000-0000-4000-8000-{len(self.preparations):012d}",
                            state="running", message="Preparing synthetic history.",
                            progress=[])
            status, body = 202, deepcopy(self.job)
        elif (self.job and url.path == "/api/history/jobs/" + self.job["job_id"]
              and request.method == "GET"):
            if self.job_error:
                status, body = 404, {"detail": self.job_error}
            else:
                body = deepcopy(self.job)
        elif url.path == "/api/backtest" and request.method == "GET":
            self.backtests.append(parse_qs(url.query, keep_blank_values=True))
            if self.backtest_error:
                status, body = 400, {"detail": self.backtest_error}
            else:
                body = synthetic_backtest()
                query = self.backtests[-1]
                body.update(symbol=query["symbol"][0], exchange=query["exchange"][0])
                body["policy"] = dict(
                    policy_id="trend-continuation-v2-4h-1h-15m-v1",
                    settings=dict(strategy_names=["trend_continuation_v2"], timeframe="1h",
                                  confirmation_timeframes=["4h"], entry_timeframe="15m",
                                  indicator_window=400, min_confirmed_score=70,
                                  risk_percent=float(query["risk_percent"][0]) / 100,
                                  notional_cap=float(query["notional"][0]),
                                  fee_rate=float(query["fee_rate"][0]),
                                  slippage_bps=float(query["slippage_bps"][0])))
                if self.backtest_policy_mismatch:
                    body["policy"]["settings"]["notional_cap"] = 999
        else:
            self.unexpected.append((request.method, url.path))
            status, body = 500, {"detail": "Unexpected synthetic endpoint"}
        route.fulfill(status=status, content_type="application/json",
                      body=json.dumps(body))


def hold_next(page, path):
    page.evaluate("path => { window.__smokeHold[path] = true; }", path)


def wait_held(page, path):
    page.wait_for_function(
        "path => typeof window.__smokeRelease[path] === 'function'", arg=path)


def release(page, path):
    page.evaluate("path => window.__smokeRelease[path]()", path)
    page.clock.run_for(10)


def refresh(page):
    page.click("#refreshStatus")


def assert_no_injection(page):
    assert page.locator("img").count() == 0
    assert page.evaluate("window.__providerInjected === undefined")


def run_checks(context, base_url, pages):
    fixture, errors = FixtureAPI(), []
    context.route("**/api/**", fixture.route)
    page = context.new_page()
    pages.append(page)
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.add_init_script(f"({FETCH_GATE})();")
    page.clock.install(time=NOW)
    page.clock.pause_at(NOW + timedelta(seconds=1))
    page.goto(base_url + "/index.html", wait_until="domcontentloaded")

    # Opening the page reads persisted state; it cannot start a bot or download.
    expect(page.locator("#runtimeState")).to_have_text("STOPPED")
    expect(page.locator("#startBot")).to_be_enabled()
    expect(page.locator("#stopBot")).to_be_disabled()
    expect(page.locator("#exchange")).to_be_enabled()
    assert not page.locator("#research").evaluate("element => element.open")
    assert not fixture.starts and not fixture.preparations and not fixture.backtests
    assert page.locator('a[href="/analyze"]').count() == 1
    for obsolete in ("#scan", "#assisted", "#assistedStatus", "#orderStatus",
                     "#executionStatus", "#profile"):
        assert page.locator(obsolete).count() == 0

    expect(page.locator("#equityLabel")).to_have_text("Recorded equity")
    expect(page.locator("#accountEquity")).to_have_text("9,960")
    expect(page.locator("#pnlLabel")).to_have_text("Closed-trade net P&L")
    expect(page.locator("#realizedPnl")).to_have_text("-40")
    expect(page.locator("#openCount")).to_have_text("1")
    expect(page.locator("#pendingCount")).to_have_text("1")
    open_cells = page.locator("#openPositions tr").first.locator("td")
    expect(open_cells.nth(3)).to_have_text("102.5")  # Effective stop after TP1.
    expect(open_cells.nth(5)).to_have_text("2")  # Remaining, not original size.
    expect(open_cells.nth(6)).to_have_text("Yes")
    expect(page.locator("#pendingOrders")).to_contain_text("99 – 101")
    expect(page.locator("#closedTrades")).to_contain_text("-40")
    expect(page.locator("#recentSignals")).to_contain_text(UNTRUSTED_TEXT)
    assert_no_injection(page)

    # A rejected request remains visible after the required status refresh.
    page.locator("#botSettings details summary").click()
    page.fill("#symbols", "btc/usdt, ETH/USDT, btc/usdt")
    page.fill("#maxMarkets", "7")
    page.fill("#notional", "1500")
    page.fill("#feeRate", "0.0023")
    page.fill("#slippageBps", "11")
    fixture.start_error = "Synthetic journal mismatch. " + UNTRUSTED_TEXT
    page.click("#startBot")
    expect(page.locator("#runtimeError")).to_contain_text("Synthetic journal mismatch")
    expect(page.locator("#startBot")).to_be_enabled()
    refresh(page)
    expect(page.locator("#runtimeError")).to_contain_text(UNTRUSTED_TEXT)
    assert fixture.starts[-1] == dict(CONFIG, symbols=["BTC/USDT", "ETH/USDT"],
                                     max_markets=7, notional=1500,
                                     fee_rate=0.0023, slippage_bps=11)
    assert_no_injection(page)

    # A stale stopped response cannot overwrite a later successful start.
    fixture.start_error = None
    hold_next(page, "/api/bot/status")
    refresh(page)
    wait_held(page, "/api/bot/status")
    hold_next(page, "/api/bot/start")
    page.click("#startBot")
    wait_held(page, "/api/bot/start")
    expect(page.locator("#startBot")).to_be_disabled()
    expect(page.locator("#exchange")).to_be_disabled()
    page.dispatch_event("#botForm", "submit")
    page.dispatch_event("#stopBot", "click")
    assert len(fixture.starts) == 2 and not fixture.stops
    release(page, "/api/bot/start")
    expect(page.locator("#runtimeState")).to_have_text("STARTING")
    release(page, "/api/bot/status")
    expect(page.locator("#runtimeState")).to_have_text("STARTING")
    expect(page.locator("#runtimeError")).to_be_hidden()

    fixture.runtime.update(state="running", health="ok",
                           message="Synthetic worker is managing paper positions.")
    fixture.paper["account"] = dict(starting_equity=10_000, equity=9_975,
                                    realized_net_pnl=-40, unrealized_pnl=15,
                                    fees_paid=2, open_positions=1,
                                    pending_orders=1, reserved_risk=25)
    page.clock.fast_forward(5_001)
    expect(page.locator("#runtimeState")).to_have_text("RUNNING")
    expect(page.locator("#startBot")).to_be_disabled()
    expect(page.locator("#stopBot")).to_be_enabled()
    expect(page.locator("#symbols")).to_be_disabled()
    expect(page.locator("#equityLabel")).to_have_text("Paper account equity")
    expect(page.locator("#accountEquity")).to_have_text("9,975")
    expect(page.locator("#unrealizedNote")).to_contain_text("15 USDT")
    expect(page.locator("#riskNote")).to_contain_text("25 USDT")

    fixture.runtime["health"] = "stale"
    refresh(page)
    expect(page.locator("#runtimeState")).to_have_text("RUNNING · STALE")
    expect(page.locator("#runtimeState")).to_have_class("badge error")
    expect(page.locator("#cycleHealth")).to_contain_text("stale")
    expect(page.locator("#stopBot")).to_be_enabled()

    hold_next(page, "/api/bot/status")
    page.click("#stopBot")
    expect(page.locator("#runtimeState")).to_have_text("STOPPING")
    wait_held(page, "/api/bot/status")
    expect(page.locator("#accountEquity")).to_have_text("9,975")
    expect(page.locator("#openPositions tr")).to_have_count(1)
    expect(page.locator("#stopBot")).to_be_disabled()
    expect(page.locator("#startBot")).to_be_disabled()
    assert len(fixture.stops) == 1 and fixture.stops[0] is None
    fixture.runtime.update(state="stopped", health="stopped",
                           message="Synthetic journal is saved; management is paused.")
    fixture.paper.pop("account")
    release(page, "/api/bot/status")
    page.clock.fast_forward(5_001)
    expect(page.locator("#runtimeState")).to_have_text("STOPPED")
    expect(page.locator("#startBot")).to_be_enabled()
    expect(page.locator("#exchange")).to_be_enabled()
    expect(page.locator("#openPositions tr")).to_have_count(1)
    expect(page.locator("#pendingOrders tr")).to_have_count(1)
    expect(page.locator("#portfolioSource")).to_contain_text("not live prices")

    # Recovery errors, risk halts, and unknown ownership remain distinct.
    fixture.runtime.update(state="error", health="error", last_error=UNTRUSTED_TEXT,
                           message="Synthetic recovery failed.")
    fixture.paper["risk_state"].update(halted=True,
                                       halt_reason="Synthetic daily loss limit.")
    refresh(page)
    expect(page.locator("#runtimeState")).to_have_text("ERROR")
    expect(page.locator("#runtimeError")).to_have_text(UNTRUSTED_TEXT)
    expect(page.locator("#riskHalt")).to_be_visible()
    expect(page.locator("#riskManagement")).to_contain_text("not being managed")
    assert_no_injection(page)
    page.fill("#feeRate", "0.0033")
    refresh(page)
    expect(page.locator("#feeRate")).to_have_value("0.0033")
    page.click("#restoreSettings")
    expect(page.locator("#feeRate")).to_have_value("0.0023")

    fixture.status_error = "Synthetic status service unavailable."
    refresh(page)
    expect(page.locator("#runtimeState")).to_have_text("STATUS UNAVAILABLE")
    expect(page.locator("#runtimeMessage")).to_contain_text("does not mean the worker stopped")
    expect(page.locator("#startBot")).to_be_disabled()
    expect(page.locator("#stopBot")).to_be_disabled()
    expect(page.locator("#exchange")).to_be_disabled()
    expect(page.locator("#accountEquity")).to_have_text("9,960")
    fixture.status_error = None
    fixture.runtime.update(state="external", health="unknown", last_error=None,
                           message="Synthetic account is owned by another process.")
    refresh(page)
    expect(page.locator("#runtimeState")).to_have_text("EXTERNAL OWNER")
    expect(page.locator("#runtimeMessage")).to_contain_text("another process")
    expect(page.locator("#startBot")).to_be_disabled()
    expect(page.locator("#stopBot")).to_be_disabled()
    fixture.runtime.update(state="stopped", health="ownership_unknown",
                           message="Synthetic account ownership cannot be checked.")
    refresh(page)
    expect(page.locator("#runtimeState")).to_have_text("OWNERSHIP UNKNOWN")
    expect(page.locator("#startBot")).to_be_disabled()
    fixture.capabilities["live_orders"] = True
    refresh(page)
    expect(page.locator("#runtimeState")).to_have_text("STATUS UNAVAILABLE")
    expect(page.locator("#runtimeError")).to_contain_text("paper-only runtime")
    fixture.capabilities["live_orders"] = False
    fixture.runtime.update(state="stopped", health="stopped",
                           message="Synthetic worker is stopped.")
    fixture.paper["risk_state"]["halted"] = False
    refresh(page)
    expect(page.locator("#runtimeState")).to_have_text("STOPPED")
    expect(page.locator("#runtimeError")).to_be_hidden()

    # History progresses as a job. Completion never triggers a backtest.
    page.locator("#research > summary").click()
    page.fill("#btSymbol", "BTC/USDT")
    page.fill("#btStart", "2025-01-01")
    page.fill("#btEnd", "2025-12-31")
    page.click("#prepareHistory")
    expect(page.locator("#prepareHistory")).to_be_disabled()
    expect(page.locator("#backtest")).to_be_disabled()
    expect(page.locator("#startBot")).to_be_disabled()
    page.wait_for_function("document.querySelector('#historyStatus').textContent.includes('Preparing synthetic')")
    assert fixture.preparations[-1] == dict(
        exchange="kucoin", market_type="spot", symbol="BTC/USDT",
        start="2025-01-01", end="2025-12-31")
    fixture.job["progress"] = [
        dict(timeframe="4h", rows=2_500, start="2024-11-01T00:00:00Z",
             end="2025-12-31T20:00:00Z")
    ]
    page.clock.fast_forward(1_001)
    expect(page.locator("#historyProgress tr")).to_have_count(1)
    expect(page.locator("#historyProgress")).to_contain_text("2,500")
    assert not fixture.backtests
    fixture.job.update(state="completed", message="Synthetic history is ready.",
                       warmup_start="2024-11-01T00:00:00Z",
                       progress=[
                           dict(timeframe=timeframe, rows=rows,
                                start="2024-11-01T00:00:00Z",
                                end="2025-12-31T23:00:00Z")
                           for timeframe, rows in (("4h", 2500), ("1h", 10000), ("15m", 40000))
                       ])
    page.clock.fast_forward(3_001)
    expect(page.locator("#backtest")).to_be_enabled()
    expect(page.locator("#historyProgress tr")).to_have_count(3)
    expect(page.locator("#btStatus")).to_contain_text("Run validation")
    assert not fixture.backtests

    page.click("#backtest")
    expect(page.locator("#backtestResults")).to_be_visible()
    expected_query = dict(
        exchange=["kucoin"], market_type=["spot"], symbol=["BTC/USDT"],
        timeframe=["1h"], data_source=["local"], start=["2025-01-01"],
        end=["2025-12-31"], equity=["10000"], risk_percent=["0.25"],
        notional=["1500"], fee_rate=["0.0023"], slippage_bps=["11"],
        min_score=["70"], strategies=["trend_continuation_v2"],
        confirm_timeframes=["4h"], entry_timeframe=["15m"],
        execution_timeframe=[""], validate_oos=["true"], compare_exits=["false"],
    )
    assert fixture.backtests[-1] == expected_query
    expect(page.locator("#metrics")).to_contain_text("0.25%")
    expect(page.locator("#btYears")).to_contain_text("2025")
    expect(page.locator("#btOos tr")).to_have_count(3)
    expect(page.locator("#equityChart polyline")).to_have_count(1)
    expect(page.locator("#btWarning")).to_contain_text(UNTRUSTED_TEXT)
    expect(page.locator("#btStatus")).to_contain_text("8,760 evaluation candles + 400 warm-up candles")
    assert_no_injection(page)
    fixture.backtest_policy_mismatch = True
    page.click("#backtest")
    expect(page.locator("#btError")).to_contain_text("matching core policy")
    expect(page.locator("#backtestResults")).to_be_hidden()
    fixture.backtest_policy_mismatch = False
    page.check("#btCompareExits")
    expect(page.locator("#backtestResults")).to_be_hidden()
    page.click("#backtest")
    expect(page.locator("#backtestResults")).to_be_visible()
    assert fixture.backtests[-1]["compare_exits"] == ["true"]
    page.locator("#backtestResults details summary").click()
    expect(page.locator("#btAblation")).to_contain_text("default")
    expect(page.locator("#costDiagnostics")).to_contain_text("fees: 6")
    expect(page.locator("#costDiagnostics")).to_contain_text("modeled slippage: 4")

    # Edited inputs invalidate even an already downloaded, delayed response.
    hold_next(page, "/api/backtest")
    page.click("#backtest")
    wait_held(page, "/api/backtest")
    page.fill("#btSymbol", "ETH/USDT")
    release(page, "/api/backtest")
    expect(page.locator("#backtestResults")).to_be_hidden()
    expect(page.locator("#btStatus")).to_contain_text("Settings changed")
    expect(page.locator("#historyProgressWrap")).to_be_hidden()
    expect(page.locator("#backtest")).to_be_enabled()
    fixture.backtest_error = "Synthetic full 15m history is missing. " + UNTRUSTED_TEXT
    page.click("#backtest")
    expect(page.locator("#btError")).to_contain_text("full 15m history is missing")
    expect(page.locator("#backtestResults")).to_be_hidden()
    expect(page.locator("#runtimeError")).to_be_hidden()
    assert_no_injection(page)

    # Partial history must display an error; there is no automatic retry POST.
    fixture.backtest_error = None
    previous_runs = len(fixture.backtests)
    page.click("#prepareHistory")
    page.wait_for_function("document.querySelector('#historyStatus').textContent.includes('Preparing synthetic')")
    fixture.job.update(state="error", message="Synthetic history is incomplete.",
                       error="Synthetic 15m coverage is incomplete. " + UNTRUSTED_TEXT,
                       progress=[dict(timeframe="4h", rows=2500,
                                      start="2024-11-01T00:00:00Z",
                                      end="2025-12-31T20:00:00Z")])
    page.clock.fast_forward(1_001)
    expect(page.locator("#btError")).to_contain_text("15m coverage is incomplete")
    expect(page.locator("#btStatus")).to_contain_text("has not run")
    expect(page.locator("#prepareHistory")).to_be_enabled()
    expect(page.locator("#backtestResults")).to_be_hidden()
    assert len(fixture.backtests) == previous_runs
    assert len(fixture.preparations) == 2
    assert_no_injection(page)

    # A server restart can lose an in-memory job; 404 must unlock explicit retry.
    page.click("#prepareHistory")
    page.wait_for_function("document.querySelector('#historyStatus').textContent.includes('Preparing synthetic')")
    fixture.job_error = "Synthetic history job was lost after a server restart."
    page.clock.fast_forward(1_001)
    expect(page.locator("#historyStatus")).to_contain_text("no longer available")
    expect(page.locator("#btError")).to_contain_text("lost after a server restart")
    expect(page.locator("#prepareHistory")).to_be_enabled()
    expect(page.locator("#exchange")).to_be_enabled()
    assert len(fixture.preparations) == 3
    assert len(fixture.backtests) == previous_runs
    fixture.job_error = None

    # Zero values are known values; absent values must not look like zero.
    fixture.paper["summary"] = dict(pending_orders=0, open_positions=0, realized_net_pnl=0)
    fixture.paper["risk_state"]["last_equity"] = 0
    fixture.paper["active_orders"] = []
    refresh(page)
    expect(page.locator("#accountEquity")).to_have_text("0")
    expect(page.locator("#realizedPnl")).to_have_text("0")
    expect(page.locator("#openCount")).to_have_text("0")
    fixture.paper["summary"] = {}
    fixture.paper["risk_state"]["last_equity"] = None
    refresh(page)
    expect(page.locator("#accountEquity")).to_have_text("—")
    expect(page.locator("#realizedPnl")).to_have_text("—")
    expect(page.locator("#pendingCount")).to_have_text("—")

    # Tables scroll within their cards on a phone, without widening the page.
    page.set_viewport_size({"width": 390, "height": 844})
    expect(page.locator("#startBot")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
    page.fill("#btSymbol", "BTC/USDT")
    page.click("#backtest")
    expect(page.locator("#backtestResults")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")

    assert len(fixture.starts) == 2 and len(fixture.stops) == 1
    assert not fixture.unexpected, f"Unexpected API requests: {fixture.unexpected}"
    assert not errors, f"Browser JavaScript errors: {errors}"
    print("PASS: paper start/stop, stale-response protection, journal portfolio, "
          "ownership/errors, explicit history/validation, safe text, and mobile layout")


def main():
    handler = partial(QuietHandler, directory=str(ROOT / "src/trading_bot/web"))
    with ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as playwright:
                launch = {"headless": True}
                if os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE"):
                    launch["executable_path"] = os.environ["PLAYWRIGHT_CHROMIUM_EXECUTABLE"]
                browser = playwright.chromium.launch(**launch)
                pages = []
                try:
                    context = browser.new_context(viewport={"width": 1440, "height": 1100})
                    context.set_default_timeout(10_000)
                    run_checks(context, f"http://127.0.0.1:{server.server_port}", pages)
                except Exception:
                    if pages and not pages[-1].is_closed():
                        pages[-1].screenshot(path=str(ROOT / "dashboard-smoke-failure.png"),
                                             full_page=True)
                    raise
                finally:
                    browser.close()
        finally:
            server.shutdown()
            thread.join(timeout=5)


if __name__ == "__main__":
    main()
