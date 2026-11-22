"""Standalone browser checks with synthetic API responses; no exchange access.

Run after installing Playwright and Chromium:
    python tests/browser/dashboard_smoke.py
Only a failure screenshot is written, to dashboard-smoke-failure.png.
"""
from dataclasses import asdict
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
from threading import Thread
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from trading_bot.config import builtin_profiles

SYMBOLS = [f"{base}/USDT" for base in (
    "BTC", "ETH", "SOL", "XRP", "LINK", "ADA", "AVAX", "SUI", "JUP", "NEAR", "DOT", "ATOM",
)]

# A controlled fetch-resolution gate exercises races without timing sleeps.
# Native requests still reach the synthetic route, and release is explicit.
FETCH_GATE = """() => {
  window.__smokeHold = {};
  window.__smokeRelease = {};
  const original = window.fetch.bind(window);
  window.fetch = (...args) => {
    const path = new URL(args[0], location.href).pathname;
    const response = original(...args);
    if (!window.__smokeHold[path]) return response;
    return new Promise((resolve, reject) => {
      window.__smokeRelease[path] = () => {
        delete window.__smokeHold[path];
        delete window.__smokeRelease[path];
        response.then(resolve, reject);
      };
    });
  };
} """


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


def synthetic_scan(query):
    setup = dict(entry_low=99, entry_high=101, stop_loss=95,
                 take_profit_1=105, take_profit_2=110,
                 actual_risk_amount=25, notional_value=500)
    return {
        "exchange": query["exchange"][0], "market_type": query["market_type"][0],
        "timeframe": query["timeframe"][0], "auto_universe": True,
        "confirmation_timeframes": ["4h"], "strategies": ["trend_continuation_v2"],
        "results": [dict(symbol=symbol, status="CONFIRMED", signal="LONG", score=80,
                         strategy="trend_continuation_v2", setup=setup,
                         reason="Synthetic browser fixture; not a market signal") for symbol in SYMBOLS],
        "universe": {
            "total_markets": 90, "eligible_markets": 40,
            "rejected": {"low_volume": 7, "stale_history": 2},
            "warnings": ["Synthetic coverage fixture"],
            "candidates": [dict(symbol=symbol, quote_volume=10_000_000,
                                spread_bps=3, history_candles=250) for symbol in SYMBOLS],
        },
    }


def run_checks(context, base_url, pages):
    scans, unexpected, errors = [], [], []
    fixtures = {
        "/api/profiles": {"profiles": [asdict(profile) for profile in builtin_profiles()]},
        "/api/assisted/intents": {"mode": "DRY_RUN", "intents": []},
        "/api/orders/status": {"mode": "DRY_RUN", "orders": []},
        "/api/execution/status": {"environment_enabled": False, "control": {},
                                  "context": None, "active_orders": []},
        "/api/paper/status": {
            "journal": "SYNTHETIC browser fixture", "risk_state": None,
            "summary": {"pending_orders": 0, "open_positions": 0, "realized_net_pnl": 0},
            "checkpoints": [], "recent_signals": [], "active_orders": [], "recent_trades": [],
            "heartbeat": {"status": "IDLE"}, "universe": {"status": "READY", "symbols": []},
        },
        "/api/assisted/stage": {"created": True},
    }

    def route_api(route):
        url = urlsplit(route.request.url)
        if url.path == "/api/scan":
            query = parse_qs(url.query, keep_blank_values=True)
            scans.append(query)
            body = synthetic_scan(query)
        elif url.path in fixtures:
            body = fixtures[url.path]
        else:
            unexpected.append(url.path)
            route.fulfill(status=500, content_type="application/json",
                          body=json.dumps({"detail": "Unexpected synthetic endpoint"}))
            return
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    context.route("**/api/**", route_api)

    def new_page(hold_profiles=False):
        page = context.new_page()
        pages.append(page)
        page.on("pageerror", lambda error: errors.append(str(error)))
        gate = f"({FETCH_GATE})();"
        if hold_profiles:
            gate += "window.__smokeHold['/api/profiles'] = true;"
        page.add_init_script(gate)
        page.goto(base_url + "/index.html", wait_until="domcontentloaded")
        return page

    page = new_page()
    expect(page.locator("#profile")).to_have_value("liquid-spot")
    expect(page.locator("#assistedStatus")).to_contain_text("DRY_RUN")
    expect(page.locator("#orderStatus")).to_contain_text("DRY_RUN")
    expect(page.locator("#executionStatus")).to_contain_text("DISARMED")
    expect(page.locator("#paperStatus")).to_contain_text("SYNTHETIC")
    assert not scans, "Page load must not discover or scan markets automatically"

    page.select_option("#profile", "broad-spot")
    expect(page.locator("#maxMarkets")).to_have_value("50")
    assert not scans, "Selecting a preset must not start a scan"
    page.click("#scan")
    expect(page.locator("#results tr")).to_have_count(len(SYMBOLS))
    expect(page.locator("#results button")).to_have_count(len(SYMBOLS))
    expect(page.locator("#universeStatus")).to_contain_text("12 selected")
    expect(page.locator("#universeSummary")).to_contain_text("low volume: 7")
    expect(page.locator("#scannedSymbols option")).to_have_count(len(SYMBOLS))
    assert page.locator("#scannedSymbols option").evaluate_all("items => items.map(x => x.value)") == SYMBOLS
    assert scans[-1]["max_markets"] == ["50"]

    page.fill("#feeRate", "0.0023")
    expect(page.locator("#results tr")).to_have_count(0)
    expect(page.locator("#results button")).to_have_count(0)
    page.fill("#slippageBps", "11")
    page.click("#scan")
    expect(page.locator("#results tr")).to_have_count(len(SYMBOLS))
    with page.expect_request(lambda request: urlsplit(request.url).path == "/api/assisted/stage") as staged:
        page.locator("#results button").first.click()
    payload = staged.value.post_data_json
    assert payload["symbol"] == SYMBOLS[0]
    assert payload["fee_rate"] == 0.0023 and payload["slippage_bps"] == 11
    assert scans[-1]["fee_rate"] == ["0.0023"] and scans[-1]["slippage_bps"] == ["11"]

    page.fill("#maxMarkets", "17")
    expect(page.locator("#results tr")).to_have_count(0)
    page.evaluate("window.__smokeHold['/api/scan'] = true")
    page.click("#scan")
    page.wait_for_function("typeof window.__smokeRelease['/api/scan'] === 'function'")
    page.fill("#maxMarkets", "18")
    page.evaluate("window.__smokeRelease['/api/scan']()")
    expect(page.locator("#scan")).to_be_enabled()
    expect(page.locator("#results tr")).to_have_count(0)
    expect(page.locator("#results button")).to_have_count(0)
    expect(page.locator("#scanStatus")).to_contain_text("Settings changed")

    scan_count = len(scans)
    delayed = new_page(hold_profiles=True)
    delayed.wait_for_function("typeof window.__smokeRelease['/api/profiles'] === 'function'")
    delayed.fill("#maxMarkets", "19")
    delayed.evaluate("window.__smokeRelease['/api/profiles']()")
    expect(delayed.locator("#profile option")).to_have_count(len(builtin_profiles()) + 1)
    expect(delayed.locator("#maxMarkets")).to_have_value("19")
    expect(delayed.locator("#profile")).to_have_value("")
    assert len(scans) == scan_count, "Delayed profile loading must not initiate a market scan"
    assert not unexpected, f"Unexpected API requests: {unexpected}"
    assert not errors, f"Browser JavaScript errors: {errors}"
    print("PASS: synthetic dashboard coverage, presets, staging costs, stale-scan rejection, and delayed-profile edits")


def main():
    handler = partial(QuietHandler, directory=str(ROOT / "src/trading_bot/web"))
    with ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                pages = []
                try:
                    context = browser.new_context(viewport={"width": 1440, "height": 1100})
                    context.set_default_timeout(10_000)
                    run_checks(context, f"http://127.0.0.1:{server.server_port}", pages)
                except Exception:
                    if pages and not pages[-1].is_closed():
                        pages[-1].screenshot(path=str(ROOT / "dashboard-smoke-failure.png"), full_page=True)
                    raise
                finally:
                    browser.close()
        finally:
            server.shutdown()
            thread.join(timeout=5)


if __name__ == "__main__":
    main()
