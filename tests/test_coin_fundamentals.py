"""Deterministic identity, provenance and failure-boundary tests for evidence.

No test calls a live provider or requires exchange/API credentials.
"""

from __future__ import annotations

import copy
import io
import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

from trading_bot.analysis.fundamentals import (
    ANNOUNCEMENTS_URL,
    COIN_LIST_URL,
    PROTOCOLS_URL,
    EvidenceError,
    FundamentalService,
    JsonHttpClient,
    _download_json,
    safe_public_url,
)


NOW = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)


def iso(value=NOW):
    return value.isoformat().replace("+00:00", "Z")


def coin_payload(**updates):
    data = {
        "id": "quant-network", "symbol": "qnt", "name": "Quant",
        "last_updated": iso(NOW - timedelta(minutes=1)),
        "market_data": {
            "market_cap": {"usd": 1_000_000},
            "fully_diluted_valuation": {"usd": 1_200_000},
            "circulating_supply": 10_000, "total_supply": 12_000,
            "max_supply": 15_000,
        },
        "links": {"homepage": ["https://quant.network/"],
                  "whitepaper": "https://quant.network/whitepaper.pdf"},
        "public_notice": "",
    }
    data.update(updates)
    return data


def announcement(title, *, identifier="1", description="", published=None, url=None):
    return {
        "annId": identifier, "annTitle": title, "annDesc": description,
        "cTime": int((published or (NOW - timedelta(hours=1))).timestamp() * 1000),
        "annUrl": url or f"https://www.kucoin.com/announcement/{identifier}",
    }


class ProviderTests(unittest.TestCase):
    def service(self, *, coin=None, candidates=None, pages=None, total_pages=None,
                protocols=None, fees=None, revenue=None, include_defillama=False,
                fail=None, max_pages=2):
        calls = []
        coin = coin_payload() if coin is None else coin
        candidates = [{"id": "quant-network", "symbol": "qnt", "name": "Quant"}] if candidates is None else candidates
        pages = [[]] if pages is None else pages
        protocols = [] if protocols is None else protocols
        fees = {"gecko_id": "quant-network", "total24h": 100, "latestFetchIsOk": True} if fees is None else fees
        revenue = {"gecko_id": "quant-network", "total24h": 20, "latestFetchIsOk": True} if revenue is None else revenue

        def fetch(url, **kwargs):
            calls.append((url, kwargs))
            if fail:
                raise fail
            if url == COIN_LIST_URL:
                return candidates
            if url.startswith("https://api.coingecko.com/api/v3/coins/"):
                return coin
            if url.startswith(ANNOUNCEMENTS_URL):
                page = int(parse_qs(urlsplit(url).query)["currentPage"][0])
                return {"code": "200000", "data": {
                    "items": pages[page - 1] if len(pages) >= page else [],
                    "totalPage": total_pages if total_pages is not None else len(pages),
                }}
            if url == PROTOCOLS_URL:
                return protocols
            if url.startswith("https://api.llama.fi/summary/fees/"):
                return fees if "dataType=dailyFees" in url else revenue
            raise AssertionError("Unexpected test endpoint")

        http = JsonHttpClient(fetcher=fetch, clock=lambda: NOW, coingecko_demo_key="")
        service = FundamentalService(http=http, clock=lambda: NOW,
                                     include_defillama=include_defillama,
                                     max_announcement_pages=max_pages)
        return service, calls

    def test_collects_real_numeric_metadata_with_provenance_not_directional_score(self):
        service, calls = self.service()
        report = service.collect("QNT", name="Quant")
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["identity"]["coingecko_id"], "quant-network")
        self.assertEqual(report["metrics"]["market_cap_usd"], 1_000_000)
        self.assertAlmostEqual(report["metrics"]["circulating_to_total_ratio"], 10_000 / 12_000)
        self.assertIn("https://quant.network/", report["project_urls"])
        source = next(s for s in report["sources"] if s["provider"] == "CoinGecko")
        self.assertEqual(source["as_of"], iso(NOW - timedelta(minutes=1)))
        self.assertEqual(source["retrieved_at"], iso())
        self.assertFalse(source["cached"])
        self.assertFalse(report["historical_backtest_eligible"])
        self.assertFalse(report["review_required"])
        self.assertNotIn("score", report)
        self.assertNotIn("signal", report)
        self.assertTrue(all(c[1]["timeout"] <= 15 for c in calls))

    def test_missing_announcements_are_unknown_not_all_clear(self):
        service, _ = self.service()
        report = service.collect("QNT")
        self.assertEqual(report["events"], [])
        self.assertTrue(any("does not establish absence" in g for g in report["coverage_gaps"]))
        self.assertTrue(any("unlock" in g for g in report["coverage_gaps"]))

    def test_ticker_collision_requires_disambiguation(self):
        candidates = [
            {"id": "quant-network", "symbol": "qnt", "name": "Quant"},
            {"id": "another-quant", "symbol": "qnt", "name": "Other Quant"},
        ]
        service, calls = self.service(candidates=candidates)
        report = service.collect("QNT")
        self.assertIsNone(report["identity"])
        self.assertEqual(report["metrics"], {})
        self.assertTrue(any("ambiguous" in g for g in report["coverage_gaps"]))
        self.assertFalse(any("/coins/quant-network?" in url for url, _ in calls))

    def test_exact_project_name_disambiguates_an_identical_ticker(self):
        service, _ = self.service(candidates=[
            {"id": "quant-network", "symbol": "qnt", "name": "Quant"},
            {"id": "another-quant", "symbol": "qnt", "name": "Other Quant"},
        ])
        report = service.collect("QNT", name="Quant")
        self.assertEqual(report["identity"]["coingecko_id"], "quant-network")

    def test_conflicting_project_name_cannot_fall_back_to_a_ticker_guess(self):
        service, _ = self.service()
        report = service.collect("QNT", name="Unrelated Project")
        self.assertIsNone(report["identity"])
        self.assertEqual(report["metrics"], {})

    def test_explicit_id_skips_list_but_must_match_provider_response(self):
        service, calls = self.service(coin=coin_payload(symbol="wrong"))
        report = service.collect("QNT", coingecko_id="quant-network")
        self.assertIsNone(report["identity"])
        self.assertEqual(report["metrics"], {})
        self.assertFalse(any(url == COIN_LIST_URL for url, _ in calls))
        self.assertTrue(any("does not match" in g for g in report["coverage_gaps"]))

    def test_id_path_injection_is_not_requested(self):
        service, calls = self.service()
        report = service.collect("QNT", coingecko_id="../../bitcoin?api_key=secret")
        self.assertIsNone(report["identity"])
        self.assertFalse(any("secret" in url for url, _ in calls))
        self.assertNotIn("secret", json.dumps(report))

    def test_invalid_base_never_requests_a_provider(self):
        service, calls = self.service()
        report = service.collect("QNT/../../other")
        self.assertEqual(report["status"], "unavailable")
        self.assertFalse(calls)

    def test_stale_and_future_market_timestamps_are_explicit(self):
        for observed, expected_status in [
            (NOW - timedelta(days=2), "stale"),
            (NOW + timedelta(days=2), "partial"),
        ]:
            with self.subTest(observed=observed):
                service, _ = self.service(coin=coin_payload(last_updated=iso(observed)))
                report = service.collect("QNT")
                source = next(s for s in report["sources"] if s["provider"] == "CoinGecko")
                self.assertEqual(source["status"], expected_status)
                if expected_status == "partial":
                    self.assertIsNone(source["as_of"])

    def test_invalid_metrics_and_inconsistent_supply_do_not_become_scores(self):
        service, _ = self.service(coin=coin_payload(market_data={
            "market_cap": {"usd": float("nan")},
            "fully_diluted_valuation": {"usd": float("inf")},
            "circulating_supply": 20, "total_supply": 10, "max_supply": True,
        }))
        report = service.collect("QNT")
        self.assertNotIn("market_cap_usd", report["metrics"])
        self.assertNotIn("fully_diluted_valuation_usd", report["metrics"])
        self.assertNotIn("max_supply", report["metrics"])
        self.assertNotIn("circulating_to_total_ratio", report["metrics"])
        self.assertTrue(any("exceeds total" in g for g in report["coverage_gaps"]))

    def test_project_links_are_safe_and_not_requested(self):
        service, calls = self.service(coin=coin_payload(links={"homepage": [
            "javascript:alert(1)", "http://quant.network", "https://127.0.0.1/x",
            "https://user:password@quant.network/x", "https://quant.network/",
        ], "whitepaper": "https://quant.network/whitepaper.pdf"}))
        report = service.collect("QNT")
        self.assertEqual(report["project_urls"], ["https://quant.network/", "https://quant.network/whitepaper.pdf"])
        self.assertFalse(any(url.startswith("https://quant.network/") for url, _ in calls))

    def test_critical_official_event_requires_review(self):
        service, _ = self.service(pages=[[
            announcement("KuCoin Will Delist Quant (QNT)", description="Trading will cease."),
            announcement("KuCoin Lists QNTX", identifier="2"),
        ]])
        report = service.collect("QNT")
        self.assertTrue(report["review_required"])
        self.assertEqual(len(report["events"]), 1)
        event = report["events"][0]
        self.assertEqual(event["severity"], "high")
        self.assertEqual(event["published_at"], iso(NOW - timedelta(hours=1)))
        self.assertIsNone(event["event_at"])
        self.assertTrue(event["source_url"].startswith("https://www.kucoin.com/announcement/"))

    def test_critical_event_in_description_also_requires_review(self):
        service, _ = self.service(pages=[[
            announcement("Important Notice for Quant (QNT)", description="Deposits are suspended pending review."),
        ]])
        self.assertTrue(service.collect("QNT")["review_required"])

    def test_regular_announcement_does_not_add_a_bullish_score(self):
        service, _ = self.service(pages=[[announcement("Quant (QNT) Trading Competition")]])
        report = service.collect("QNT")
        self.assertFalse(report["review_required"])
        self.assertEqual(report["events"][0]["severity"], "info")
        self.assertEqual(report["risk_flags"], [])

    def test_common_short_ticker_requires_explicit_asset_reference(self):
        service, _ = self.service(coin=coin_payload(id="harmony", symbol="one", name="Harmony"),
                                 candidates=[{"id": "harmony", "symbol": "one", "name": "Harmony"}],
                                 pages=[[
                                     announcement("ONE Important Exchange Notice"),
                                     announcement("Harmony (ONE) Deposits Suspended", identifier="2"),
                                 ]])
        report = service.collect("ONE")
        self.assertEqual([e["id"] for e in report["events"]], ["2"])

    def test_unofficial_unsafe_or_undated_announcements_are_excluded(self):
        missing_date = announcement("QNT Hacked", identifier="5")
        missing_date.pop("cTime")
        service, _ = self.service(pages=[[
            announcement("QNT Hacked", url="https://www.kucoin.com.evil.test/announcement/x"),
            announcement("QNT Hacked", identifier="2", url="javascript:alert(1)"),
            announcement("QNT Hacked", identifier="3", url="https://www.kucoin.com/other/x"),
            announcement("QNT Hacked", identifier="4", published=NOW + timedelta(days=1)),
            missing_date,
            announcement("QNT Hacked", identifier="6", published=NOW - timedelta(days=31)),
        ]])
        report = service.collect("QNT")
        self.assertFalse(report["review_required"])
        self.assertEqual(report["events"], [])
        self.assertTrue(any("excluded" in g for g in report["coverage_gaps"]))

    def test_external_markup_is_treated_as_text(self):
        service, _ = self.service(pages=[[
            announcement("<b>Quant (QNT) Update</b><script>steal_secrets()</script>",
                         description="<p>Supply details</p><style>hide-all</style>"),
        ]])
        event = service.collect("QNT")["events"][0]
        self.assertEqual(event["title"], "Quant (QNT) Update")
        self.assertEqual(event["summary"], "Supply details")

    def test_pagination_is_bounded_and_truncation_visible(self):
        item = announcement("Quant (QNT) Update")
        service, calls = self.service(pages=[[item], [item]], total_pages=100)
        report = service.collect("QNT")
        self.assertEqual(len([url for url, _ in calls if url.startswith(ANNOUNCEMENTS_URL)]), 2)
        self.assertEqual(len(report["events"]), 1)
        self.assertTrue(report["announcement_coverage"]["truncated"])
        self.assertTrue(any("page budget" in g for g in report["coverage_gaps"]))

    def test_network_failure_is_structured_and_does_not_echo_credentials(self):
        service, _ = self.service(fail=URLError("failed https://source.test?key=private-secret"))
        report = service.collect("QNT")
        self.assertEqual(report["status"], "unavailable")
        self.assertEqual(report["metrics"], {})
        self.assertNotIn("private-secret", json.dumps(report))
        self.assertTrue(report["coverage_gaps"])
        self.assertTrue(all(s["retrieved_at"] is None for s in report["sources"]))
        self.assertTrue(all(s["attempted_at"] == iso() for s in report["sources"]))

    def test_disabled_mode_does_not_fetch(self):
        def fail_if_called(*args, **kwargs):
            raise AssertionError("No requests permitted")
        service = FundamentalService(http=JsonHttpClient(fetcher=fail_if_called), enabled=False)
        report = service.collect("QNT", now=NOW)
        self.assertEqual(report["status"], "disabled")
        self.assertFalse(report["sources"])

    def test_protocol_mapping_is_by_exact_coin_id_not_matching_ticker(self):
        service, calls = self.service(include_defillama=True, protocols=[
            {"slug": "other-quant", "symbol": "QNT", "gecko_id": "another-quant", "tvl": 1_000_000},
        ])
        report = service.collect("QNT")
        self.assertNotIn("protocol_tvl_usd", report["metrics"])
        self.assertFalse(any("/summary/fees/" in url for url, _ in calls))
        self.assertTrue(any("unique DefiLlama protocol mapping" in g for g in report["coverage_gaps"]))

    def test_duplicate_protocol_ids_do_not_double_count_child_protocols(self):
        service, _ = self.service(include_defillama=True, protocols=[
            {"slug": "quant-v1", "gecko_id": "quant-network", "tvl": 100},
            {"slug": "quant-v2", "gecko_id": "quant-network", "tvl": 200},
        ])
        self.assertNotIn("protocol_tvl_usd", service.collect("QNT")["metrics"])

    def test_protocol_metrics_keep_collection_and_time_uncertainty(self):
        service, _ = self.service(include_defillama=True, protocols=[
            {"slug": "quant", "gecko_id": "quant-network", "tvl": 1_000},
        ], revenue={"gecko_id": "quant-network", "total24h": -5, "latestFetchIsOk": True})
        report = service.collect("QNT")
        self.assertEqual(report["metrics"]["protocol_tvl_usd"], 1_000)
        self.assertEqual(report["metrics"]["protocol_fees_24h_usd"], 100)
        self.assertEqual(report["metrics"]["protocol_revenue_24h_usd"], -5)
        sources = [s for s in report["sources"] if s["provider"].startswith("DefiLlama")]
        self.assertEqual(len(sources), 3)
        self.assertTrue(all(s["status"] == "partial" and s["as_of"] is None for s in sources))

    def test_failed_or_mismatched_protocol_collection_cannot_supply_metrics(self):
        service, _ = self.service(include_defillama=True, protocols=[
            {"slug": "quant", "gecko_id": "quant-network", "tvl": 1_000},
        ], fees={"gecko_id": "quant-network", "total24h": 100, "latestFetchIsOk": False},
           revenue={"gecko_id": "another-quant", "total24h": 1_000_000})
        report = service.collect("QNT")
        self.assertNotIn("protocol_fees_24h_usd", report["metrics"])
        self.assertNotIn("protocol_revenue_24h_usd", report["metrics"])
        self.assertEqual(report["metrics"]["protocol_tvl_usd"], 1_000)


class HttpClientTests(unittest.TestCase):
    def test_cache_ttl_preserves_actual_retrieval_time_and_copies_mutable_data(self):
        current = [NOW]
        calls = []
        def fetch(url, **kwargs):
            calls.append(url)
            return {"items": [1]}
        client = JsonHttpClient(fetcher=fetch, clock=lambda: current[0], coingecko_demo_key="")
        first = client.get(PROTOCOLS_URL, ttl_seconds=60)
        first.data["items"].append(2)
        current[0] += timedelta(seconds=59)
        second = client.get(PROTOCOLS_URL, ttl_seconds=60)
        self.assertEqual(second.data, {"items": [1]})
        self.assertTrue(second.cached)
        self.assertEqual(second.retrieved_at, NOW)
        current[0] += timedelta(seconds=1)
        third = client.get(PROTOCOLS_URL, ttl_seconds=60)
        self.assertFalse(third.cached)
        self.assertEqual(third.retrieved_at, current[0])
        self.assertEqual(len(calls), 2)

    def test_clock_rollback_and_expired_failure_never_relabel_cached_data_fresh(self):
        current, calls = [NOW], []
        def fetch(url, **kwargs):
            calls.append(url)
            if len(calls) > 1:
                raise TimeoutError("private-url-or-secret")
            return {"value": 1}
        client = JsonHttpClient(fetcher=fetch, clock=lambda: current[0])
        client.get(PROTOCOLS_URL)
        current[0] -= timedelta(seconds=1)
        with self.assertRaisesRegex(EvidenceError, "network request failed"):
            client.get(PROTOCOLS_URL)
        current[0] = NOW + timedelta(seconds=301)
        with self.assertRaisesRegex(EvidenceError, "network request failed"):
            client.get(PROTOCOLS_URL)
        self.assertEqual(len(calls), 3)

    def test_cache_is_thread_safe_and_capacity_bounded(self):
        calls, lock = [], threading.Lock()
        def fetch(url, **kwargs):
            with lock:
                calls.append(url)
            return {"items": [1]}
        client = JsonHttpClient(fetcher=fetch, clock=lambda: NOW, max_cache_entries=1)
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: client.get(PROTOCOLS_URL), range(16)))
        self.assertEqual(len(calls), 1)
        self.assertEqual(sum(not r.cached for r in results), 1)
        client.get(COIN_LIST_URL)
        client.get(PROTOCOLS_URL)
        self.assertEqual(len(calls), 3)

    def test_credential_header_only_reaches_its_provider_and_limits_reach_adapter(self):
        calls = []
        def fetch(url, **kwargs):
            calls.append((url, copy.deepcopy(kwargs)))
            return {}
        client = JsonHttpClient(fetcher=fetch, clock=lambda: NOW, coingecko_demo_key="secret-key", timeout_seconds=3, max_bytes=2048)
        client.get(COIN_LIST_URL)
        client.get(PROTOCOLS_URL)
        self.assertEqual(calls[0][1]["headers"]["x-cg-demo-api-key"], "secret-key")
        self.assertNotIn("x-cg-demo-api-key", calls[1][1]["headers"])
        self.assertEqual(calls[0][1]["timeout"], 3)
        self.assertEqual(calls[0][1]["max_bytes"], 2048)
        self.assertTrue(all("secret-key" not in url for url, _ in calls))

    def test_client_rejects_unsafe_hosts_and_malformed_urls_before_fetch(self):
        calls = []
        client = JsonHttpClient(fetcher=lambda url, **kwargs: calls.append(url))
        for url in ["https://evil.test/api", "https://api.coingecko.com.evil.test/api", "https://[", "http://api.coingecko.com/api", "https://user:secret@api.coingecko.com/api", "https://api.kucoin.com/api#fragment"]:
            with self.subTest(url=url), self.assertRaises(EvidenceError):
                client.get(url)
        self.assertFalse(calls)

    def test_http_errors_never_echo_response_body_or_secret(self):
        def fetch(url, **kwargs):
            raise HTTPError(url + "?key=secret", 429, "secret body", {}, io.BytesIO(b"secret body"))
        client = JsonHttpClient(fetcher=fetch)
        with self.assertRaisesRegex(EvidenceError, "HTTP 429") as caught:
            client.get(PROTOCOLS_URL)
        self.assertNotIn("secret", str(caught.exception))

    def test_download_enforces_response_size_and_never_follows_redirects(self):
        class Response(io.BytesIO):
            headers = {}
        class Opener:
            def open(self, request, timeout):
                self.request = request
                self.timeout = timeout
                return Response(b"12345")
        opener = Opener()
        with patch("trading_bot.analysis.fundamentals.build_opener", return_value=opener) as build:
            with self.assertRaisesRegex(EvidenceError, "size limit"):
                _download_json(PROTOCOLS_URL, timeout=2, headers={"Accept": "application/json"}, max_bytes=4)
            redirect_handler = build.call_args.args[0]
            self.assertIsNone(redirect_handler.redirect_request(None, None, 302, "redirect", {}, "https://evil.test/"))
            self.assertEqual(opener.request.method, "GET")
            self.assertEqual(opener.timeout, 2)

    def test_download_parses_bounded_json_from_response(self):
        class Response(io.BytesIO):
            headers = {"Content-Length": "11"}
        class Opener:
            def open(self, request, timeout):
                return Response(b'{"value":1}')
        with patch("trading_bot.analysis.fundamentals.build_opener", return_value=Opener()):
            self.assertEqual(_download_json(PROTOCOLS_URL, timeout=2, headers={}, max_bytes=20), {"value": 1})

    def test_public_source_urls_reject_credentials_local_hosts_and_bad_schemes(self):
        for url in ["javascript:alert(1)", "https://localhost/x", "https://127.0.0.1/x", "https://169.254.169.254/latest", "https://example.local/x", "https://user:secret@example.com/x", "https://example.com:8443/x", "https://example.com/\nmalicious"]:
            with self.subTest(url=url):
                self.assertIsNone(safe_public_url(url))
        self.assertEqual(safe_public_url("https://example.com/path"), "https://example.com/path")
        self.assertIsNone(safe_public_url("https://www.kucoin.com.evil.test/announcement/x", kucoin_only=True))


if __name__ == "__main__":
    unittest.main()
