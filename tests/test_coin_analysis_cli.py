"""CLI contract checks using injected service types, with no provider access."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from trading_bot.analysis import cli


DEFAULT_REPORT = {
    "report_id": "synthetic-cli-fixture",
    "decision": {"action": "WAIT", "summary": "Synthetic test report"},
    "journal": {"saved": True},
}


class CoinAnalysisCliTests(unittest.TestCase):
    def invoke(self, argv, *, report=None, request_error=None, service_error=None,
               journal_error=None, runtime_error=None):
        self.request_type = Mock(side_effect=request_error or (lambda **kwargs: SimpleNamespace(**kwargs)))
        self.service = Mock()
        self.service.analyze.return_value = DEFAULT_REPORT if report is None else report
        self.service.analyze.side_effect = service_error
        self.service_type = Mock(return_value=self.service)
        self.journal = object()
        self.journal_type = Mock(return_value=self.journal, side_effect=journal_error)
        stdout, stderr = StringIO(), StringIO()
        with patch.object(cli, "_runtime_types", return_value=(self.request_type, self.service_type, self.journal_type),
                          side_effect=runtime_error) as runtime, redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                code = cli.main(argv)
            except SystemExit as exc:
                code = exc.code
        self.runtime = runtime
        return code, stdout.getvalue(), stderr.getvalue()

    def test_default_request_preserves_percentage_risk_units_and_prints_full_json(self):
        code, stdout, stderr = self.invoke(["QNT"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout), DEFAULT_REPORT)
        self.assertEqual(stderr, "")
        self.assertTrue(stdout.endswith("\n"))
        self.request_type.assert_called_once_with(
            query="QNT", exchange="kucoin", market_type="spot", quote="USDT",
            equity=10_000, risk_percent=0.25, notional=1_000, fee_rate=0.001,
            slippage_bps=5, include_fundamentals=False,
        )
        self.assertEqual(self.service.analyze.call_args.args[0].risk_percent, 0.25)
        self.service_type.assert_called_once_with()
        self.journal_type.assert_not_called()

    def test_optional_fundamentals_require_explicit_enable(self):
        code, _, stderr = self.invoke(["QNT", "--fundamentals"])
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertTrue(self.service.analyze.call_args.args[0].include_fundamentals)

    def test_multiword_name_and_selected_quote_are_passed_unchanged(self):
        code, _, _ = self.invoke(["Bitcoin Cash", "--quote", "USDC", "--exchange", "kraken"])
        self.assertEqual(code, 0)
        request = self.service.analyze.call_args.args[0]
        self.assertEqual(request.query, "Bitcoin Cash")
        self.assertEqual(request.quote, "USDC")
        self.assertEqual(request.exchange, "kraken")

    def test_explicit_contract_pair_and_risk_cost_flags_are_forwarded(self):
        code, _, _ = self.invoke([
            "BTC/USDT:USDT", "--market-type", "swap", "--equity", "25000",
            "--risk-percent", "0.75", "--notional", "2000", "--fee-rate", "0.0006",
            "--slippage-bps", "8", "--no-fundamentals",
        ])
        self.assertEqual(code, 0)
        request = self.service.analyze.call_args.args[0]
        self.assertEqual(request.query, "BTC/USDT:USDT")
        self.assertEqual(request.market_type, "swap")
        self.assertEqual(request.equity, 25000)
        self.assertEqual(request.risk_percent, 0.75)
        self.assertEqual(request.notional, 2000)
        self.assertEqual(request.fee_rate, 0.0006)
        self.assertEqual(request.slippage_bps, 8)
        self.assertFalse(request.include_fundamentals)

    def test_journal_override_is_the_only_explicit_output_path(self):
        path = "data/my analysis/reports.db"
        code, _, _ = self.invoke(["QNT", "--journal", path])
        self.assertEqual(code, 0)
        self.journal_type.assert_called_once_with(path)
        self.service_type.assert_called_once_with(journal=self.journal)

    def test_valid_blocked_and_unavailable_reports_still_return_zero(self):
        for action in ("NO_TRADE", "INSUFFICIENT_DATA", "WAIT", "LONG_READY", "SHORT_READY"):
            with self.subTest(action=action):
                report = {"decision": {"action": action}, "journal": {"saved": True},
                          "fundamentals": {"status": "unavailable", "coverage_gaps": ["Test gap"]}}
                code, stdout, stderr = self.invoke(["QNT"], report=report)
                self.assertEqual(code, 0)
                self.assertEqual(json.loads(stdout), report)
                self.assertEqual(stderr, "")

    def test_unicode_and_embedded_newlines_are_valid_json_data(self):
        report = {"decision": {"action": "WAIT", "summary": 'Évidence\n"quoted"'}, "journal": {"saved": True}}
        code, stdout, _ = self.invoke(["QNT"], report=report)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout), report)
        self.assertIn("Évidence", stdout)

    def test_validation_errors_return_two_without_echoing_exception_details(self):
        code, stdout, stderr = self.invoke(["QNT"], request_error=ValueError("secret_key=private-secret"))
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("Invalid analysis inputs", stderr)
        self.assertTrue(stderr.startswith("crypto-bot analyze: error:"))
        self.assertNotIn("private-secret", stderr)
        self.service_type.assert_not_called()
        self.journal_type.assert_not_called()

    def test_nonfinite_or_nonnumeric_arguments_fail_before_runtime_loading(self):
        for flag in ("--equity", "--risk-percent", "--notional", "--fee-rate", "--slippage-bps"):
            for value in ("nan", "inf", "-inf", "not-a-number"):
                with self.subTest(flag=flag, value=value):
                    code, stdout, stderr = self.invoke(["QNT", f"{flag}={value}"])
                    self.assertEqual(code, 2)
                    self.assertEqual(stdout, "")
                    self.assertIn("must be a finite number", stderr)
                    self.runtime.assert_not_called()

    def test_missing_query_invalid_market_and_abbreviated_flags_are_rejected(self):
        for argv in ([], ["QNT", "--market-type", "margin"], ["QNT", "--risk", "0.25"]):
            with self.subTest(argv=argv):
                code, stdout, stderr = self.invoke(argv)
                self.assertEqual(code, 2)
                self.assertEqual(stdout, "")
                self.assertIn("usage:", stderr)
                self.runtime.assert_not_called()

    def test_help_is_available_without_service_imports_or_journal_initialization(self):
        code, stdout, stderr = self.invoke(["--help"])
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("crypto-bot analyze", stdout)
        self.assertIn("--fundamentals", stdout)
        self.assertIn("--no-fundamentals", stdout)
        self.assertIn("0.25 means 0.25%", " ".join(stdout.split()))
        self.assertIn("TRADING_ANALYSIS_DB", stdout)
        self.assertIn("No orders are placed", stdout)
        self.runtime.assert_not_called()

    def test_runtime_import_failure_is_sanitized(self):
        code, stdout, stderr = self.invoke(["QNT"], runtime_error=ImportError("private module location secret"))
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("installation and dependencies", stderr)
        self.assertNotIn("secret", stderr)

    def test_service_failure_is_sanitized_and_emits_no_partial_json(self):
        code, stdout, stderr = self.invoke(["QNT"], service_error=RuntimeError("https://example.test?api_key=private-secret"))
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("Analysis or journal persistence failed", stderr)
        self.assertNotIn("private-secret", stderr)
        self.assertNotIn("https://", stderr)

    def test_journal_creation_failure_is_sanitized_and_prevents_analysis(self):
        code, stdout, stderr = self.invoke(["QNT", "--journal", "private/location.db"],
                                          journal_error=PermissionError("private location or secret"))
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("journal permissions", stderr)
        self.assertNotIn("secret", stderr)
        self.service_type.assert_not_called()

    def test_explicit_unsaved_report_cannot_be_printed_as_success(self):
        code, stdout, stderr = self.invoke(["QNT"], report={
            "decision": {"action": "LONG_READY"}, "journal": {"saved": False, "error": "private-secret"},
        })
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertNotIn("private-secret", stderr)

    def test_nonfinite_or_unserializable_reports_produce_no_invalid_json(self):
        for value in (float("nan"), float("inf"), float("-inf"), object()):
            with self.subTest(value=value):
                code, stdout, stderr = self.invoke(["QNT"], report={"value": value})
                self.assertEqual(code, 1)
                self.assertEqual(stdout, "")
                self.assertIn("no valid report was printed", stderr)

    def test_report_must_be_a_json_object(self):
        code, stdout, _ = self.invoke(["QNT"], report=["not", "a", "report"])
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")


if __name__ == "__main__":
    unittest.main()
