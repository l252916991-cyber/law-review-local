"""Offline unit tests for the capacity smoke harness (no sockets, no real case data)."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from scripts.capacity_smoke import (
    Counter,
    Endpoint,
    Result,
    _new_client,
    build_endpoints,
    build_model_endpoint,
    parse_ps_output,
    peak_summary,
    percentile,
    render_markdown,
    summarize,
    summarize_by_endpoint,
)


class PercentileTests(unittest.TestCase):
    def test_nearest_rank(self) -> None:
        values = [float(value) for value in range(1, 101)]
        self.assertEqual(percentile(values, 50), 50.0)
        self.assertEqual(percentile(values, 95), 95.0)
        self.assertEqual(percentile(values, 100), 100.0)

    def test_empty_and_small_inputs(self) -> None:
        self.assertEqual(percentile([], 95), 0.0)
        self.assertEqual(percentile([7.5], 95), 7.5)

    def test_rejects_invalid_percent(self) -> None:
        with self.assertRaises(ValueError):
            percentile([1.0], 0)


class SummaryTests(unittest.TestCase):
    def test_summarize_counts_errors(self) -> None:
        results = [
            Result("read-load", "cases-list", 200, 10.0),
            Result("read-load", "cases-list", 500, 20.0),
            Result("read-load", "search", 0, 30.0, "ReadTimeout"),
        ]
        summary = summarize(results)
        self.assertEqual(summary["requests"], 3)
        self.assertEqual(summary["errors"], 2)
        self.assertAlmostEqual(summary["error_rate"], 0.6667, places=4)
        self.assertEqual(summary["p50_ms"], 20.0)
        self.assertEqual(summary["status_counts"]["200"], 1)
        self.assertEqual(summary["status_counts"]["500"], 1)
        self.assertEqual(summary["status_counts"]["client_error:ReadTimeout"], 1)
        self.assertEqual(list(summary["status_counts"]), ["200", "500", "client_error:ReadTimeout"])

    def test_summarize_empty(self) -> None:
        summary = summarize([])
        self.assertEqual(summary["requests"], 0)
        self.assertEqual(summary["error_rate"], 0.0)
        self.assertEqual(summary["p95_ms"], 0.0)

    def test_grouping_by_endpoint(self) -> None:
        results = [
            Result("read-load", "cases-list", 200, 10.0),
            Result("read-load", "search", 200, 100.0),
            Result("read-load", "search", 200, 300.0),
        ]
        grouped = summarize_by_endpoint(results)
        self.assertEqual(list(grouped), ["cases-list", "search"])
        self.assertEqual(grouped["search"]["requests"], 2)
        self.assertEqual(grouped["search"]["p95_ms"], 300.0)


class EndpointTests(unittest.TestCase):
    def test_client_accepts_empty_base_url_without_network(self) -> None:
        with patch("httpx.Client.request", side_effect=AssertionError("client construction must not send a request")):
            client = _new_client(1.0)
        try:
            self.assertEqual(str(client.base_url), "")
        finally:
            client.close()

    def test_page_endpoint_requires_document(self) -> None:
        names = {endpoint.name for endpoint in build_endpoints(7, None, 1)}
        self.assertNotIn("page-text", names)
        names = {endpoint.name for endpoint in build_endpoints(7, 42, 3)}
        self.assertIn("page-text", names)
        page = next(endpoint for endpoint in build_endpoints(7, 42, 3) if endpoint.name == "page-text")
        self.assertEqual(page.path, "/api/documents/42/pages/3")

    def test_case_endpoints_are_case_scoped(self) -> None:
        for endpoint in build_endpoints(9, None, 1):
            if endpoint.name in {"cases-list", "dashboard"}:
                continue
            self.assertIn("/9", endpoint.path)

    def test_model_endpoint_uses_llm(self) -> None:
        endpoint = build_model_endpoint(3)
        self.assertEqual(endpoint.method, "POST")
        self.assertEqual(endpoint.path, "/api/cases/3/chat")
        self.assertTrue(endpoint.body and endpoint.body["use_llm"])


class SamplingTests(unittest.TestCase):
    def test_parse_ps_output(self) -> None:
        self.assertEqual(parse_ps_output("  12.5 204800\n"), {"cpu_percent": 12.5, "rss_mb": 200.0})
        self.assertIsNone(parse_ps_output(""))
        self.assertIsNone(parse_ps_output("not a number"))

    def test_peak_summary(self) -> None:
        samples = [
            {"server_cpu_percent": 10.0, "server_rss_mb": 100.0, "wal_mb": 1.0, "disk_free_gb": 50.0, "inflight": 1},
            {"server_cpu_percent": 90.0, "server_rss_mb": 300.0, "wal_mb": 8.5, "disk_free_gb": 49.0, "inflight": 50},
        ]
        peaks = peak_summary(samples)
        self.assertEqual(peaks["server_cpu_percent_peak"], 90.0)
        self.assertEqual(peaks["server_rss_mb_peak"], 300.0)
        self.assertEqual(peaks["wal_mb_peak"], 8.5)
        self.assertEqual(peaks["disk_free_gb_min"], 49.0)
        self.assertEqual(peaks["inflight_peak"], 50)

    def test_counter_tracks_peak(self) -> None:
        counter = Counter()
        counter.enter()
        counter.enter()
        counter.leave()
        self.assertEqual(counter.snapshot(), 1)
        self.assertEqual(counter.peak, 2)


class MarkdownTests(unittest.TestCase):
    def _result(self, model_load: dict[str, object] | None) -> dict[str, object]:
        read_results = [
            Result("read-load", "cases-list", 200, 12.0),
            Result("read-load", "search", 200, 480.0),
        ]
        baseline_results = [Result("baseline", "cases-list", 200, 8.0)]
        return {
            "environment": {
                "started_at": "2026-09-09 12:00:00+0800",
                "platform": "macOS-test",
                "python": "3.14.5",
                "cpu_count": 10,
                "base_url": "http://127.0.0.1:8791",
                "case_id": 1,
                "readers": 50,
                "read_seconds": 60.0,
                "model_concurrency": 5,
                "model_requests": 15,
            },
            "baseline": summarize(baseline_results),
            "read_load": summarize(read_results),
            "read_by_endpoint": summarize_by_endpoint(read_results),
            "model_load": model_load,
            "peaks": peak_summary([{"server_cpu_percent": 55.0, "wal_mb": 3.0}]),
            "recovery": {"recovered": True, "seconds": 1.5, "threshold_ms": 8.0},
        }

    def test_markdown_reports_numbers_and_caveat(self) -> None:
        markdown = render_markdown(self._result(summarize([Result("model-load", "chat-model", 200, 4200.0)])))
        self.assertIn("不构成 SLA", markdown)
        self.assertIn("| search |", markdown)
        self.assertIn("480.0ms", markdown)
        self.assertIn("4200.0ms", markdown)
        self.assertIn("恢复到基线 p95 以内并连续 5 次通过：是", markdown)

    def test_markdown_marks_skipped_model_phase(self) -> None:
        markdown = render_markdown(self._result(None))
        self.assertIn("未执行", markdown)


if __name__ == "__main__":
    unittest.main()
