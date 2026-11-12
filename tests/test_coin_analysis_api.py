"""Read-only API contract and rejection paths; no provider/network calls."""

from copy import deepcopy
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from fastapi.testclient import TestClient

from trading_bot.web import app as web


REPORT_ID = "e165739a-4ad2-4387-9b14-544802578bdd"


class FakeJournal:
    def history(self, limit):
        return [{"report_id": REPORT_ID, "symbol": "BTC/USDT", "action": "WAIT"}][:limit]

    def get(self, report_id):
        return {"report_id": report_id, "decision": {"action": "WAIT"}} if report_id == REPORT_ID else None


class CoinAnalysisApiTests(TestCase):
    def setUp(self):
        self.requests = []
        self.report = {"report_id": REPORT_ID, "decision": {"action": "WAIT", "win_probability": None},
                       "resolution": {"status": "resolved"}, "journal": {"saved": True}}

        def analyze(request):
            self.requests.append(request)
            return deepcopy(self.report)

        self.service = SimpleNamespace(analyze=analyze, journal=FakeJournal())
        self.patch = patch.object(web, "_analysis_service", return_value=self.service)
        self.get_service = self.patch.start()
        self.addCleanup(self.patch.stop)
        self.client = TestClient(web.app)

    def test_page_and_navigation_are_packaged_routes(self):
        page = self.client.get("/analyze")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Analyze", page.text)
        self.assertIn('href="/analyze"', self.client.get("/").text)
        self.get_service.assert_not_called()

    def test_defaults_and_percentage_units_reach_service(self):
        response = self.client.post("/api/analyze", json={"query": "Bitcoin Cash", "risk_percent": 0.25})
        self.assertEqual(response.status_code, 200)
        request = self.requests[0]
        self.assertEqual(request.query, "Bitcoin Cash")
        self.assertEqual(request.risk_percent, 0.25)
        self.assertEqual(request.exchange, "kucoin")
        self.assertEqual(request.market_type, "spot")
        self.assertTrue(request.include_fundamentals)
        self.assertIsNone(response.json()["decision"]["win_probability"])

    def test_swap_and_disabled_fundamentals_are_explicit(self):
        response = self.client.post("/api/analyze", json={"query": "BTC/USDT:USDT", "market_type": "swap",
            "include_fundamentals": False, "notional": 1500, "fee_rate": 0.0006, "slippage_bps": 12})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.requests[0].market_type, "swap")
        self.assertFalse(self.requests[0].include_fundamentals)
        self.assertEqual(self.requests[0].notional, 1500)

    def test_ambiguous_and_unavailable_are_structured_reports(self):
        for state in ("ambiguous", "unavailable"):
            with self.subTest(state=state):
                self.report["resolution"] = {"status": state, "candidates": []}
                self.report["decision"]["action"] = "INSUFFICIENT_DATA"
                response = self.client.post("/api/analyze", json={"query": "SAME"})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["resolution"]["status"], state)

    def test_invalid_inputs_cannot_reach_service(self):
        bad = [{"query": ""}, {"query": "  "}, {"query": "B\nTC"}, {"query": "x" * 121},
               {"query": "BTC", "exchange": "http://127.0.0.1"}, {"query": "BTC", "market_type": "future"},
               {"query": "BTC", "quote": "BTC"}, {"query": "BTC", "risk_percent": 0},
               {"query": "BTC", "risk_percent": 6}, {"query": "BTC", "notional": -1},
               {"query": "BTC", "equity": 0}, {"query": "BTC", "fee_rate": -1},
               {"query": "BTC", "slippage_bps": 501}, {"query": "BTC", "include_fundamentals": "false"},
               {"query": "BTC", "api_key": "do-not-echo-secret"}, {"query": "BTC", "equity": True}]
        for payload in bad:
            with self.subTest(payload=payload):
                response = self.client.post("/api/analyze", json=payload)
                self.assertEqual(response.status_code, 422)
                self.assertNotIn("do-not-echo-secret", response.text)
        self.get_service.assert_not_called()

    def test_nonfinite_json_values_return_safe_422(self):
        for number in ("NaN", "Infinity", "-Infinity", "1e9999"):
            response = self.client.post("/api/analyze", content='{"query":"BTC","risk_percent":' + number + '}',
                                        headers={"content-type": "application/json"})
            self.assertEqual(response.status_code, 422)
        self.get_service.assert_not_called()

    def test_failed_persistence_never_returns_a_signal_or_raw_exception(self):
        self.service.analyze = lambda _: (_ for _ in ()).throw(RuntimeError("do-not-echo-secret"))
        response = self.client.post("/api/analyze", json={"query": "BTC"})
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("do-not-echo-secret", response.text)
        self.assertNotIn("LONG_READY", response.text)

    def test_history_limits_and_report_ids(self):
        self.assertEqual(self.client.get("/api/analysis/history").status_code, 200)
        for limit in (0, -1, 501):
            self.assertEqual(self.client.get(f"/api/analysis/history?limit={limit}").status_code, 422)
        self.assertEqual(self.client.get(f"/api/analysis/reports/{REPORT_ID}").status_code, 200)
        self.assertEqual(self.client.get("/api/analysis/reports/00000000-0000-0000-0000-000000000000").status_code, 404)
        self.assertEqual(self.client.get("/api/analysis/reports/not-an-id").status_code, 422)

    def test_corrupt_journal_cannot_appear_as_verified_history(self):
        self.service.journal = SimpleNamespace(
            history=lambda _: (_ for _ in ()).throw(ValueError("digest mismatch")),
            get=lambda _: (_ for _ in ()).throw(ValueError("digest mismatch")),
        )
        self.assertEqual(self.client.get("/api/analysis/history").status_code, 503)
        self.assertEqual(self.client.get(f"/api/analysis/reports/{REPORT_ID}").status_code, 503)

