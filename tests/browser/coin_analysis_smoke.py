"""Standalone Analyze Coin checks with synthetic reports and no exchange access.

Install Playwright and Chromium, then run:
    python tests/browser/coin_analysis_smoke.py

PLAYWRIGHT_CHROMIUM_EXECUTABLE can select an existing Chromium executable.
All API calls are intercepted. Only a failure screenshot is written, to
coin-analysis-smoke-failure.png. These fixtures are not trading signals.
"""

from datetime import datetime, timedelta, timezone
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
from threading import Thread
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright


ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 11, 12, 16, tzinfo=timezone.utc)
UNTRUSTED_TEXT = '<img src=x onerror="window.__providerInjected=true">'

# Buffer a completed response before explicitly releasing it. This exercises
# revision checks even when cancellation cannot stop an already completed reply.
FETCH_GATE = """() => {
  window.__smokeHold = {};
  window.__smokeRelease = {};
  const original = window.fetch.bind(window);
  window.fetch = (...args) => {
    const path = new URL(args[0], location.href).pathname;
    const response = original(...args);
    if (!window.__smokeHold[path]) return response;
    return response.then(async received => {
      const body = await received.text();
      const buffered = new Response(body, {
        status: received.status, headers: received.headers
      });
      return new Promise(resolve => {
        window.__smokeRelease[path] = () => {
          delete window.__smokeHold[path];
          delete window.__smokeRelease[path];
          resolve(buffered);
        };
      });
    });
  };
}"""


def iso(value):
    return value.isoformat().replace("+00:00", "Z")


def candles(count, minutes, bearish=False):
    result = []
    for i in range(count):
        candle = dict(timestamp=iso(NOW - timedelta(minutes=minutes * (count - i))),
                      open=99 + i * 0.025, high=100 + i * 0.025,
                      low=98.7 + i * 0.025, close=99.3 + i * 0.025,
                      volume=2000 + i * 10, ema_50=99 + i * 0.02,
                      ema_200=98.8 + i * 0.015)
        if bearish:
            opening = 102 - i * 0.025
            candle.update(open=opening, high=opening + 0.3, low=opening - 1,
                          close=opening - 0.3, ema_50=opening + 0.2,
                          ema_200=opening + 0.5)
        result.append(candle)
    return result


def synthetic_report(query="QNT", action="LONG_READY", report_id="fixture-qnt",
                     market_type="spot", expires_at=None):
    """A stable contract fixture, deliberately marked as synthetic evidence."""
    base = "ETH" if query.upper().startswith("ETH") else "QNT"
    bearish = action == "SHORT_READY"
    regime = "BEAR_TREND" if bearish else "BULL_TREND"
    latest_setup = candles(60, 60, bearish)[-1]
    symbol = f"{base}/USDT" + (":USDT" if market_type == "swap" else "")
    # Deliberately off the periodic refresh boundary; the exact deadline matters.
    deadline = expires_at or iso(NOW + timedelta(seconds=103))
    plan = dict(
        signal="SHORT" if action == "SHORT_READY" else "LONG",
        status="CONFIRMED" if action.endswith("READY") else "FORMING",
        score=78, entry_low=99, entry_high=101, reference_entry=100,
        stop_loss=95, take_profit_1=105, take_profit_2=110,
        reward_to_risk_1=1, reward_to_risk_2=2, net_reward_to_risk=1.38,
        position_size=4.35, notional_value=435, target_risk_amount=25,
        actual_risk_amount=25, estimated_loss_at_stop=25,
        reference_price_risk_amount=21.75, blended_target_net_reward_to_risk=1.12,
        contract_count_estimate=43.5 if market_type == "swap" else None,
        exchange_precision_applied=False, usable_now=action.endswith("READY"),
        take_profit_1_fraction=0.5,
        move_stop_to_break_even_after_tp1=True, break_even_mode="cost",
        entry_condition="Synthetic: completed 15m confirmation inside the zone.",
        cancel_conditions=["Synthetic: setup invalidates below 95.",
                           "Do not chase beyond the entry zone."],
        expires_at=deadline, execution_supported=False,
        costs={"fee_rate_per_side": 0.001, "slippage_bps_per_side": 5,
               "limitations": ["Funding and live execution are not verified."]},
    )
    if action == "SHORT_READY":
        plan.update(stop_loss=105, take_profit_1=95, take_profit_2=90,
                    cancel_conditions=["Synthetic: setup invalidates above 105."])
    levels = {key: plan[key] for key in (
        "entry_low", "entry_high", "stop_loss", "take_profit_1", "take_profit_2"
    )}
    levels.update(support=96, resistance=108)
    return {
        "report_id": report_id, "generated_at": iso(NOW), "expires_at": deadline,
        "instrument": dict(name="Ethereum" if base == "ETH" else "Quant",
                           symbol=symbol, base=base, quote="USDT", exchange="kucoin",
                           market_type=market_type),
        "resolution": {"status": "resolved", "candidates": []},
        "decision": {
            "action": action, "bias": "SHORT" if action == "SHORT_READY" else "LONG",
            "summary": "Synthetic browser fixture; not a market signal.",
            "reasons": ["4h context and 1h setup align in this fixture."],
            "counter_evidence": ["Liquidity and execution costs can change after this snapshot."],
            "blockers": [] if action.endswith("READY") else ["Entry confirmation pending."],
            "score": 78, "win_probability": None, "validated": False,
        },
        "technical": {
            "as_of": iso(NOW),
            "chart_basis": "Validated, closed candles frozen at the 1h decision close; live quotes are separate.",
            "indicators": dict(rsi=42.8 if bearish else 57.2, atr=2.5,
                               close=latest_setup["close"], ema_50=latest_setup["ema_50"],
                               ema_200=latest_setup["ema_200"],
                               volume_ratio=1.3),
            "structure": {"regime": regime},
            "confirmations": [dict(timeframe="4h", regime=regime, aligned=True)],
            "entry_trigger": dict(timeframe="15m", regime=regime,
                                  confirmed=action.endswith("READY"),
                                  reason="Synthetic completed-candle trigger."),
            "patterns": [dict(name="pullback", status="CONFIRMED" if action.endswith("READY") else "FORMING",
                              reason="Synthetic setup pattern, not a measured edge.")],
        },
        "charts": [dict(timeframe=timeframe, candles=candles(count, minutes, bearish), levels=levels,
                        as_of=iso(NOW), level_timeframe="1h")
                   for timeframe, count, minutes in (("4h", 40, 240), ("1h", 60, 60), ("15m", 80, 15))],
        "market_context": {
            "status": "ok", "benchmark": "BTC/USDT", "as_of": iso(NOW),
            "asset_return_24h_pct": 3.25, "benchmark_return_24h_pct": 1.1,
            "relative_return_24h_pp": 2.15, "correlation_1h_100": 0.72,
            "benchmark_regime": "BULL_TREND",
            "interpretation": "Synthetic relative performance is context, not a forecast.",
            "candles": [{"close": 999999}],  # Audit-only data must not enter the price chart.
        },
        "derivatives": {
            "status": "partial", "costs_included_in_plan": False,
            "funding": {
                "fundingRate": 0.0001,
                "fundingTimestamp": int((NOW + timedelta(hours=4)).timestamp() * 1000),
                "nextFundingTimestamp": int((NOW + timedelta(hours=8)).timestamp() * 1000),
                "markPrice": 100.1, "indexPrice": 100,
                "source_timestamp": int((NOW - timedelta(seconds=30)).timestamp() * 1000),
                "retrieved_at": iso(NOW), "freshness": "Synthetic provider observation time.",
            },
            "open_interest": {
                "openInterestAmount": 200000, "openInterestValue": 20000000,
                "source_timestamp": None, "retrieved_at": iso(NOW),
                "freshness": "Provider observation time is unavailable.",
            },
            "coverage_gaps": ["Future funding, margin and liquidation are not modeled."],
        } if market_type == "swap" else {"status": "not_applicable"},
        "plan": plan,
        "liquidity": {
            "status": "pass", "notional": 1000, "spread_bps": 2.4, "quote_volume": 20_000_000,
            "bid_depth_quote": 200_000, "ask_depth_quote": 180_000,
            "buy_fill": {"slippage_bps": 1.2}, "sell_fill": {"slippage_bps": 1.5},
            "as_of": iso(NOW), "reasons": ["Synthetic book covers the requested size."],
            "freshness": {
                "order_book": {"timestamp": iso(NOW - timedelta(seconds=1)), "source": "exchange", "age_seconds": 1},
                "ticker": {"timestamp": iso(NOW - timedelta(seconds=2)), "source": "exchange", "age_seconds": 2},
                "quote_volume": {"timestamp": iso(NOW), "source": "receipt", "age_seconds": 0},
            },
            "quote_volume_source": {"source_method": "fetch_tickers", "freshness_key": "quote_volume",
                                    "observation_time_known": False},
        },
        "fundamentals": {
            "status": "partial", "identity": {"name": "Quant", "coingecko_id": "quant-network"},
            "metrics": dict(market_cap_usd=1_000_000_000, circulating_supply=12_000_000,
                            total_supply=15_000_000, circulating_to_total_ratio=0.8),
            "review_required": False,
            "project_urls": ["https://quant.network/", "javascript:alert('unsafe')"],
            "events": [dict(title=UNTRUSTED_TEXT, category="announcement", severity="info",
                            summary="Provider text remains literal.",
                            source_url="javascript:window.__providerInjected=true",
                            published_at=iso(NOW - timedelta(hours=1)), event_at=None)],
            "sources": [dict(provider="CoinGecko", url="https://www.coingecko.com/en/coins/quant-network",
                             status="ok", as_of=iso(NOW - timedelta(minutes=2)),
                             retrieved_at=iso(NOW), cached=False),
                        dict(provider="Unsafe fixture link", url="javascript:alert(1)",
                             status="unavailable", detail="Untrusted links are not activated.")],
            "risk_flags": ["Synthetic context has incomplete event coverage."],
            "coverage_gaps": ["A verified future unlock schedule is unavailable."],
        },
        "warnings": ["Synthetic fixture; model performance is not calibrated."],
        "journal": {"saved": True},
    }


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


def run_checks(context, base_url, pages):
    requests, unexpected, external_requests, errors = [], [], [], []
    archived = synthetic_report(report_id="fixture-archived", expires_at=iso(NOW - timedelta(minutes=1)))
    reports = {archived["report_id"]: archived}

    def route_api(route):
        path = urlsplit(route.request.url).path
        if path == "/api/analyze":
            body = route.request.post_data_json
            requests.append(body)
            query = body["query"].upper()
            if query == "OFFLINE":
                route.fulfill(status=503, json={"detail": "Synthetic provider outage."})
                return
            action = {"WAIT": "WAIT", "DERIVATIVE UNKNOWN": "WAIT", "CRITICAL EVENT": "WAIT",
                      "BEAR": "SHORT_READY", "NO TRADE": "NO_TRADE",
                      "UNAVAILABLE": "INSUFFICIENT_DATA"}.get(query, "LONG_READY")
            report = synthetic_report(body["query"], action, f"fixture-{len(requests)}",
                                      market_type=body["market_type"])
            report["liquidity"]["notional"] = body["notional"]
            report["plan"]["costs"].update(fee_rate_per_side=body["fee_rate"],
                                           slippage_bps_per_side=body["slippage_bps"])
            if query == "AMBIGUOUS":
                report.update(instrument=None, plan=None, charts=[])
                report["decision"]["action"] = "INSUFFICIENT_DATA"
                report["resolution"] = {
                    "status": "ambiguous", "reasons": ["Two synthetic markets matched."],
                    "candidates": [dict(symbol="QNT/USDT", name="Quant", base="QNT",
                                        quote="USDT", market_type="spot"),
                                   dict(symbol="QNT/USDT:USDT", name="Quant perpetual", base="QNT",
                                        quote="USDT", market_type="swap")],
                }
            if query in {"UNAVAILABLE", "NO TRADE"}:
                report["plan"] = None
            if query == "UNAVAILABLE":
                report.update(charts=[], technical={})
                report["market_context"] = {"status": "unavailable", "reason": "Synthetic benchmark data unavailable."}
                report["decision"]["blockers"] = ["Completed candle data is unavailable."]
            if query == "DERIVATIVE UNKNOWN":
                report["derivatives"] = {
                    "status": "partial", "funding": {"fundingRate": None, "source_timestamp": None,
                                                       "retrieved_at": iso(NOW)},
                    "open_interest": None, "costs_included_in_plan": False,
                    "coverage_gaps": ["Synthetic provider returned no current funding or open interest."],
                }
            if query == "BEAR" and body["market_type"] == "swap":
                report["derivatives"]["funding"]["fundingRate"] = 0
            if query == "CRITICAL EVENT":
                report["technical"]["entry_trigger"]["confirmed"] = True
                report["technical"]["patterns"][0]["status"] = "CONFIRMED"
                report["plan"]["status"] = "CONFIRMED"
                report["fundamentals"]["review_required"] = True
                report["fundamentals"]["risk_flags"] = ["Synthetic critical announcement requires source review."]
                report["fundamentals"]["events"][0].update(
                    title="Synthetic QNT trading suspension", severity="high", category="service_disruption")
                report["decision"]["blockers"] = ["Critical exchange announcement requires review."]
            if not body["include_fundamentals"]:
                report["fundamentals"] = {"status": "disabled", "coverage_gaps": ["Checks disabled."]}
            reports[report["report_id"]] = report
            response = report
        elif path == "/api/analysis/history":
            response = {"reports": [dict(report_id=item["report_id"], generated_at=item["generated_at"],
                                         expires_at=item["expires_at"], instrument=item["instrument"],
                                         action=item["decision"]["action"])
                                    for item in reversed(list(reports.values()))]}
        elif path.startswith("/api/analysis/reports/"):
            response = reports[path.rsplit("/", 1)[-1]]
        else:
            unexpected.append(path)
            route.fulfill(status=500, json={"detail": "Unexpected synthetic endpoint."})
            return
        route.fulfill(status=200, json=response)

    context.route("**/api/**", route_api)
    context.on("request", lambda request: external_requests.append(request.url)
               if not request.url.startswith(base_url + "/") else None)
    page = context.new_page()
    pages.append(page)
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.add_init_script(f"({FETCH_GATE})();")
    page.clock.install(time=NOW)
    page.goto(base_url + "/coin.html", wait_until="domcontentloaded")
    expect(page.locator("#exchange")).to_have_value("kucoin")
    expect(page.locator("#historyRows")).to_contain_text("EXPIRED")
    expect(page.locator("#result")).to_be_hidden()
    assert not requests, "Opening the page must not contact exchanges to analyze a coin"

    page.fill("#query", "Quant")
    page.locator(".assumptions summary").click()
    page.fill("#feeRate", "0.0023")
    page.fill("#slippageBps", "11")
    page.click("#analyzeButton")
    expect(page.locator("#actionBadge")).to_have_text("LONG READY")
    expect(page.locator("#instrumentName")).to_have_text("Quant")
    expect(page.locator("#instrumentMeta")).to_contain_text("12:00")  # Halifax, not UTC.
    expect(page.locator("#priceChart [data-candle]")).to_have_count(60)
    expect(page.locator("#priceChart [data-level=stop_loss]")).to_have_count(1)
    assert float(page.locator("#priceChart [data-level=stop_loss]").get_attribute("y1")) > float(
        page.locator("#priceChart [data-level=entry_low]").get_attribute("y1"))
    expect(page.locator("#planEntry")).to_have_text("99 – 101")
    expect(page.locator("#planStop")).to_have_text("95")
    expect(page.locator("#planEstimatedLoss")).to_have_text("25 USDT")
    expect(page.locator("#planFacts")).to_contain_text("21.75 USDT")
    expect(page.locator("#planFacts")).to_contain_text("Blended target net reward-to-risk")
    expect(page.locator("#planFacts")).to_contain_text("1.12 R")
    expect(page.locator("#planReadiness")).to_contain_text("entry gates passed")
    expect(page.locator("#planCosts")).to_contain_text("does not authorize or submit")
    expect(page.locator("#planCosts")).to_contain_text("0.0023")
    expect(page.locator("#planCosts")).to_contain_text("precision has not been applied")
    expect(page.locator("#exitPolicy")).to_contain_text("50% at TP1")
    expect(page.locator("#technicalAsOf")).to_contain_text("12:00")
    expect(page.locator("#chartBasis")).to_contain_text("live quotes are separate")
    expect(page.locator("#chartBasis")).to_contain_text("Levels use the 1h setup")
    expect(page.locator("#marketContextSummary")).to_contain_text("BTC/USDT")
    expect(page.locator("#marketContextFacts")).to_contain_text("+3.25%")
    expect(page.locator("#marketContextFacts")).to_contain_text("+2.15 pp")
    expect(page.locator("#marketContextFacts")).to_contain_text("0.72")
    expect(page.locator("#liquidityTime")).to_contain_text("24h volume received")
    expect(page.locator("#liquidityTime")).to_contain_text("Volume observation time: Unknown")
    volume_time = page.locator("#liquidityTime").inner_text().split("24h volume received", 1)[1].split(" · ")[0]
    assert "12:00" in volume_time, "Volume must use its own receipt time, not the ticker's time"
    expect(page.locator("#priceChart")).not_to_contain_text("999999")
    expect(page.locator("#derivativesStatus")).to_have_text("Not applicable")
    expect(page.locator("#derivativesContent")).to_be_hidden()
    expect(page.locator("#coverageGapList")).to_contain_text("unlock schedule")
    expect(page.locator("#fundamentalEvents")).to_contain_text(UNTRUSTED_TEXT)
    assert requests[-1]["fee_rate"] == 0.0023 and requests[-1]["slippage_bps"] == 11
    assert not page.locator("#result img").count()
    assert not page.locator("#result a[href^='javascript:']").count()
    assert page.evaluate("window.__providerInjected === undefined")
    page.locator("#sourcesDetails summary").click()
    expect(page.locator("#fundamentalSources a").first).to_have_attribute("rel", "noopener noreferrer")
    page.locator("#priceChart rect[tabindex='0']").focus()
    expect(page.locator("#chartCursor")).to_contain_text(" · O ")
    page.locator("#chartTabs button", has_text="15M").click()
    expect(page.locator("#priceChart [data-candle]")).to_have_count(80)

    with page.expect_download() as downloaded:
        page.click("#downloadReport")
    saved = json.loads(Path(downloaded.value.path()).read_text())
    assert saved["report_id"] == "fixture-1" and saved["decision"]["win_probability"] is None
    assert saved["plan"]["stop_loss"] == 95
    assert downloaded.value.suggested_filename == "coin-analysis-fixture-1.json"

    # An unexpired stored READY report is still clearly a saved snapshot.
    page.locator("#historyRows tr").first.locator("button").click()
    expect(page.locator("#snapshotNotice")).to_contain_text("not a new market assessment")
    expect(page.locator("#planReadiness")).to_contain_text("Run a fresh assessment")
    expect(page.locator("#formStatus")).to_have_text("Viewing a saved snapshot")

    # A changed cost or query invalidates the visible plan and its download.
    page.fill("#notional", "2000")
    expect(page.locator("#result")).to_be_hidden()
    expect(page.locator("#downloadReport")).to_be_disabled()
    page.fill("#query", "AMBIGUOUS")
    page.click("#analyzeButton")
    expect(page.locator("#resolutionCard")).to_be_visible()
    expect(page.locator("#reportBody")).to_be_hidden()
    expect(page.locator("#candidates button")).to_have_count(2)
    page.locator("#candidates button").nth(1).click()
    expect(page.locator("#resolutionCard")).to_be_hidden()
    expect(page.locator("#instrumentMeta")).to_contain_text("QNT/USDT:USDT")
    assert requests[-1]["query"] == "QNT/USDT:USDT" and requests[-1]["market_type"] == "swap"
    expect(page.locator("#fundingRate")).to_have_text("+0.01%")
    expect(page.locator("#fundingTimes")).to_contain_text("11:59")
    expect(page.locator("#fundingTimes")).to_contain_text("Retrieved:")
    expect(page.locator("#openInterestTimes")).to_contain_text("Provider observation: Unknown")
    expect(page.locator("#openInterestFacts")).to_contain_text("20,000,000")
    expect(page.locator("#derivativesGaps")).to_contain_text("Funding payments are not included")
    expect(page.locator("#planFacts")).to_contain_text("Contract count · estimate")
    expect(page.locator("#planFacts")).to_contain_text("43.5")

    page.fill("#query", "DERIVATIVE UNKNOWN")
    page.click("#analyzeButton")
    expect(page.locator("#fundingRate")).to_have_text("Unknown")
    expect(page.locator("#openInterestFacts dd")).to_have_text(["Unknown", "Unknown"])
    expect(page.locator("#fundingTimes")).to_contain_text("Provider observation: Unknown")
    expect(page.locator("#derivativesGaps")).to_contain_text("not been treated as zero")
    expect(page.locator("#planReadiness")).to_contain_text("not currently satisfied")

    page.fill("#query", "CRITICAL EVENT")
    page.click("#analyzeButton")
    expect(page.locator("#actionBadge")).to_have_text("WAIT")
    expect(page.locator("#timeframeRows")).to_contain_text("Confirmed")
    expect(page.locator("#counterEvidence")).to_contain_text("Critical exchange announcement requires review")
    expect(page.locator("#fundamentalFlags")).to_contain_text("Source review is required")
    expect(page.locator("#planReadiness")).to_contain_text("not currently satisfied")

    for query, badge in (("BEAR", "SHORT READY"), ("WAIT", "WAIT"),
                         ("NO TRADE", "NO TRADE"), ("UNAVAILABLE", "INSUFFICIENT DATA")):
        page.fill("#query", query)
        page.click("#analyzeButton")
        expect(page.locator("#actionBadge")).to_have_text(badge)
        if query == "BEAR":
            expect(page.locator("#fundingRate")).to_have_text("0%")
            expect(page.locator("#planStop")).to_have_text("105")
            expect(page.locator("#planTarget1")).to_have_text("95")
            expect(page.locator("#planTarget2")).to_have_text("90")
            assert float(page.locator("#priceChart [data-level=stop_loss]").get_attribute("y1")) < float(
                page.locator("#priceChart [data-level=entry_high]").get_attribute("y1"))
        elif query == "UNAVAILABLE":
            expect(page.locator("#marketContextSummary")).to_contain_text("benchmark data unavailable")
            expect(page.locator("#marketContextFacts dd")).to_have_text(["Unknown"] * 6)
    expect(page.locator("#planEmpty")).to_be_visible()

    # Resolve a newer report before releasing an earlier completed response.
    page.fill("#query", "QNT")
    page.evaluate("window.__smokeHold['/api/analyze'] = true")
    page.click("#analyzeButton")
    page.wait_for_function("typeof window.__smokeRelease['/api/analyze'] === 'function'")
    page.fill("#query", "ETH")
    page.evaluate("delete window.__smokeHold['/api/analyze']")
    page.click("#analyzeButton")
    expect(page.locator("#instrumentName")).to_have_text("Ethereum")
    page.evaluate("window.__smokeRelease['/api/analyze']()")
    expect(page.locator("#instrumentName")).to_have_text("Ethereum")
    expect(page.locator("#analyzeButton")).to_be_enabled()

    # Advancing the clock expires the current plan without another request.
    count_before_expiry = len(requests)
    deadline_ms = int((NOW + timedelta(seconds=103)).timestamp() * 1000)
    page.clock.fast_forward(max(1, deadline_ms - page.evaluate("Date.now()") + 1))
    expect(page.locator("#actionBadge")).to_have_text("EXPIRED")
    expect(page.locator("#snapshotNotice")).to_contain_text("historical evidence")
    expect(page.locator("#planReadiness")).to_contain_text("Expired snapshot")
    assert len(requests) == count_before_expiry
    page.locator("#historyRows tr").last.locator("button").click()
    expect(page.locator("#actionBadge")).to_have_text("EXPIRED")
    expect(page.locator("#formStatus")).to_have_text("Viewing a saved snapshot")

    page.fill("#query", "OFFLINE")
    page.click("#analyzeButton")
    expect(page.locator("#formError")).to_contain_text("Synthetic provider outage")
    expect(page.locator("#result")).to_be_hidden()
    expect(page.locator("#downloadReport")).to_be_disabled()
    expect(page.locator("#analyzeButton")).to_be_enabled()

    page.fill("#query", "UNAVAILABLE")
    page.locator("#includeFundamentals").uncheck()
    page.click("#analyzeButton")
    expect(page.locator("#chartEmpty")).to_be_visible()
    expect(page.locator("#planEmpty")).to_be_visible()
    expect(page.locator("#fundamentalSummary")).to_contain_text("disabled")
    assert requests[-1]["include_fundamentals"] is False
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "Mobile page must not overflow"

    assert not unexpected, f"Unexpected API requests: {unexpected}"
    assert not external_requests, f"Unexpected external requests: {external_requests}"
    assert not errors, f"Browser JavaScript errors: {errors}"
    print("PASS: coin report states, exact market choice, chart/plan agreement, benchmark context, "
          "funding/OI unknowns, cost-inclusive risk, source safety, download, stale response rejection, "
          "expiry, history, failure, and mobile layout")


def main():
    handler = partial(QuietHandler, directory=str(ROOT / "src/trading_bot/web"))
    with ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as playwright:
                launch = {"headless": True}
                if os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE"):
                    launch["executable_path"] = os.environ["PLAYWRIGHT_CHROMIUM_EXECUTABLE"]
                browser = playwright.chromium.launch(**launch)
                pages = []
                try:
                    context = browser.new_context(viewport={"width": 1440, "height": 1100},
                                                  accept_downloads=True)
                    context.set_default_timeout(10_000)
                    run_checks(context, f"http://127.0.0.1:{server.server_port}", pages)
                except Exception:
                    if pages and not pages[-1].is_closed():
                        pages[-1].screenshot(path=str(ROOT / "coin-analysis-smoke-failure.png"), full_page=True)
                    raise
                finally:
                    browser.close()
        finally:
            server.shutdown()
            thread.join(timeout=5)


if __name__ == "__main__":
    main()
