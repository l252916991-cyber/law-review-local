"""Usage panel: the payload separates unknown from zero and the renderer keeps that split."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app.db import db_scope, init_db, now, transaction
from app.llm_usage import record_llm_call
from app.main import app

ROOT = Path(__file__).resolve().parents[1]


class UsagePanelPayloadTests(unittest.TestCase):
    """The API must hand the page the sample sizes behind every aggregate."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="lexvault-usage-panel-")
        self.addCleanup(self.directory.cleanup)
        scope = db_scope(Path(self.directory.name) / "law_review.db")
        scope.__enter__()
        self.addCleanup(scope.__exit__, None, None, None)
        init_db(seed=True)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def _metrics(self, case_id: int) -> dict:
        response = self.client.get(f"/api/cases/{case_id}/platform-metrics")
        self.assertEqual(response.status_code, 200)
        return response.json()["llm_usage"]

    def test_partial_usage_reports_the_known_subset_and_its_coverage(self):
        record_llm_call(case_id=1, kind="chat", model="local-a", usage={"prompt_tokens": 10, "completion_tokens": 4}, latency_ms=120, ttft_ms=40)
        record_llm_call(case_id=1, kind="chat", model="local-a", usage=None, latency_ms=300)
        record_llm_call(case_id=1, kind="critic", model="local-b", usage={"prompt_tokens": 20, "completion_tokens": 6}, latency_ms=200, ttft_ms=60, status="failed")

        usage = self._metrics(1)
        self.assertEqual(usage["calls"], 3)
        self.assertEqual(usage["failed"], 1)
        self.assertEqual(usage["input_tokens"], 30)
        self.assertEqual(usage["input_tokens_known_calls"], 2)
        self.assertEqual(usage["output_tokens_known_calls"], 2)
        self.assertEqual(usage["ttft_known_calls"], 2)
        self.assertEqual(usage["ttft_p50_ms"], 50.0)
        self.assertEqual(usage["latency_p50_ms"], 200.0)
        # Groups must carry their own denominators: a single summed column would
        # let a browser read "30 tokens" as the complete total for three calls.
        chat = next(row for row in usage["by_kind"] if row["kind"] == "chat")
        self.assertEqual(chat["calls"], 2)
        self.assertEqual(chat["input_tokens_known_calls"], 1)
        self.assertEqual(chat["ttft_known_calls"], 1)
        models = {row["model"]: row for row in usage["by_model"]}
        self.assertEqual(set(models), {"local-a", "local-b"})
        self.assertEqual(models["local-b"]["failed"], 1)

    def test_a_case_without_calls_reports_no_measurements_rather_than_zero(self):
        case_id = self.client.post("/api/cases", json={"title": "空用量案件"}).json()["id"]
        usage = self._metrics(case_id)
        self.assertEqual(usage["calls"], 0)
        self.assertEqual(usage["failed"], 0)
        self.assertIsNone(usage["input_tokens"])
        self.assertIsNone(usage["output_tokens"])
        self.assertIsNone(usage["ttft_p50_ms"])
        self.assertIsNone(usage["latency_p50_ms"])
        self.assertEqual(usage["input_tokens_known_calls"], 0)
        self.assertEqual(usage["ttft_known_calls"], 0)
        self.assertEqual(usage["by_kind"], [])
        self.assertEqual(usage["by_model"], [])

    def test_calls_belong_to_one_case_only(self):
        other_id = self.client.post("/api/cases", json={"title": "另一案件"}).json()["id"]
        with transaction() as conn:
            conn.execute(
                "INSERT INTO cases(title,created_at,updated_at) VALUES (?,?,?)",
                ("第三方案件", now(), now()),
            )
        record_llm_call(case_id=1, kind="chat", model="local-a", usage={"prompt_tokens": 5, "completion_tokens": 1}, latency_ms=80, ttft_ms=20)
        self.assertEqual(self._metrics(1)["calls"], 1)
        self.assertEqual(self._metrics(other_id)["calls"], 0)

    def test_missing_case_is_rejected(self):
        response = self.client.get("/api/cases/9999/platform-metrics")
        self.assertEqual(response.status_code, 404)


class UsagePanelRenderTests(unittest.TestCase):
    """Drive the real renderer with a stub DOM; no browser required."""

    def _run(self, script: str) -> None:
        result = subprocess.run(["node", "-e", script], cwd=ROOT, text=True, capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_renderer_keeps_unknown_zero_and_partial_distinct(self):
        self._run(r'''
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const source = fs.readFileSync('app/static/app.js', 'utf8');
const sliced = source.slice(
  source.indexOf('async function loadLabMetrics('),
  source.indexOf('function renderAgentResult(')
);
const nodes = new Map();
const $ = key => { if (!nodes.has(key)) nodes.set(key, {textContent: '', innerHTML: ''}); return nodes.get(key); };
const $$ = () => [];
const escapeHtml = value => String(value ?? '').replace(/[&<>'"]/g, char => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;'})[char]);
const context = vm.createContext({console, $, $$, escapeHtml});
vm.runInContext(sliced, context, {filename: 'app.js'});
const text = id => $(`#${id}`).textContent;
const html = id => $(`#${id}`).innerHTML;

// 1. No calls at all: nothing was measured, so nothing may read as zero.
context.renderUsage({calls: 0, failed: 0, input_tokens: null, output_tokens: null,
  input_tokens_known_calls: 0, output_tokens_known_calls: 0,
  ttft_p50_ms: null, ttft_p95_ms: null, ttft_known_calls: 0,
  latency_p50_ms: null, latency_p95_ms: null, by_kind: [], by_model: []});
assert.equal(text('usage-calls'), '0');
assert.equal(text('usage-scope'), '尚无调用');
assert.equal(text('usage-input-tokens'), '—');
assert.equal(text('usage-input-note'), '尚无调用');
assert.equal(text('usage-ttft-p50'), '—');
assert.equal(text('usage-ttft-note'), '尚无可用样本');
assert.ok(html('usage-by-kind').includes('尚无调用记录'));

// 2. Calls happened but the backend omitted usage: unknown, not free.
context.renderUsage({calls: 2, failed: 1, input_tokens: null, output_tokens: null,
  input_tokens_known_calls: 0, output_tokens_known_calls: 0,
  ttft_p50_ms: null, ttft_p95_ms: null, ttft_known_calls: 0,
  latency_p50_ms: 120, latency_p95_ms: 300, by_kind: [], by_model: []});
assert.equal(text('usage-calls'), '2');
assert.equal(text('usage-failed'), '失败 1 次');
assert.equal(text('usage-input-tokens'), '未知');
assert.equal(text('usage-input-note'), 'usage 缺失 · 0/2 次可用');
assert.equal(text('usage-ttft-p50'), '—');
assert.equal(text('usage-latency-p50'), '120ms');
assert.equal(text('usage-latency-note'), '全部 2 次调用');

// 3. Partial usage: the sum is a floor and the note gives the coverage.
context.renderUsage({calls: 3, failed: 0, input_tokens: 1234, output_tokens: 20,
  input_tokens_known_calls: 2, output_tokens_known_calls: 3,
  ttft_p50_ms: 45.4, ttft_p95_ms: 88.2, ttft_known_calls: 1,
  latency_p50_ms: 900, latency_p95_ms: 1200,
  by_kind: [{kind: 'chat<b>', calls: 2, failed: 0, input_tokens: 1200, input_tokens_known_calls: 1,
    output_tokens: 18, output_tokens_known_calls: 2, ttft_p50_ms: 45.4, latency_p50_ms: 900}],
  by_model: [{model: 'local-a', calls: 3, failed: 0, input_tokens: 34, input_tokens_known_calls: 3,
    output_tokens: 2, output_tokens_known_calls: 3, ttft_p50_ms: null, latency_p50_ms: 900}]});
assert.equal(text('usage-input-tokens'), '≥1,234');
assert.equal(text('usage-input-note'), '已知部分 · 2/3 次调用有 usage');
assert.equal(text('usage-output-tokens'), '20');
assert.equal(text('usage-output-note'), '全部 3 次调用');
assert.equal(text('usage-ttft-p50'), '45ms');
assert.equal(text('usage-ttft-note'), '可用样本 1/3');
assert.equal(text('usage-latency-p95'), '1200ms');
// Labels are escaped before they reach innerHTML.
assert.ok(html('usage-by-kind').includes('chat&lt;b&gt;'));
assert.ok(html('usage-by-kind').includes('≥1,200'));
assert.ok(html('usage-by-model').includes('—'));

// 4. A response without the summary must not look like a zero-usage case.
context.renderUsage(undefined);
assert.equal(text('usage-scope'), '指标不可用');
assert.equal(text('usage-input-note'), '指标缺失');

// 5. A failed request clears the scope and says so.
context.renderUsageError(new Error('503 unavailable'));
assert.equal(text('usage-scope'), '指标不可用');
assert.equal(text('usage-failed'), '503 unavailable');
assert.equal(text('usage-input-note'), '指标不可用');
assert.ok(html('usage-by-kind').includes('加载失败'));

// 6. While a new case loads, the previous case's numbers are erased first.
context.renderUsage({calls: 7, failed: 0, input_tokens: 70, output_tokens: 7,
  input_tokens_known_calls: 7, output_tokens_known_calls: 7,
  ttft_p50_ms: 10, ttft_p95_ms: 20, ttft_known_calls: 7,
  latency_p50_ms: 30, latency_p95_ms: 40, by_kind: [], by_model: []});
assert.equal(text('usage-calls'), '7');
context.renderUsagePending();
assert.equal(text('usage-calls'), '—');
assert.equal(text('usage-input-tokens'), '—');
assert.equal(text('usage-scope'), '加载中…');
''')

    def test_renderer_formats_token_bounds_without_hiding_the_sample(self):
        self._run(r'''
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const source = fs.readFileSync('app/static/app.js', 'utf8');
const sliced = source.slice(
  source.indexOf('async function loadLabMetrics('),
  source.indexOf('function renderAgentResult(')
);
const context = vm.createContext({console, $: () => ({}), $$: () => [], escapeHtml: String});
vm.runInContext(sliced, context, {filename: 'app.js'});
assert.equal(context.usageTokenText(null, 0, 0), '—');
assert.equal(context.usageTokenText(null, 0, 4), '未知');
assert.equal(context.usageTokenText(0, 1, 1), '0');
assert.equal(context.usageTokenText(30, 2, 3), '≥30');
assert.equal(context.usageTokenNote(0, 3), 'usage 缺失 · 0/3 次可用');
assert.equal(context.usageTokenNote(3, 3), '全部 3 次调用');
assert.equal(context.usageTokenNote(2, 3), '已知部分 · 2/3 次调用有 usage');
assert.equal(context.usageSampleNote(0, 3), '尚无可用样本');
assert.equal(context.usageFailedText(0, 0), '尚无调用');
assert.equal(context.usageFailedText(2, 5), '失败 2 次');
assert.equal(context.usageMilliseconds(null), '—');
assert.equal(context.usageMilliseconds(88.6), '89ms');
''')

    def test_group_rows_are_empty_without_calls(self):
        self._run(r'''
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const source = fs.readFileSync('app/static/app.js', 'utf8');
const sliced = source.slice(
  source.indexOf('async function loadLabMetrics('),
  source.indexOf('function renderAgentResult(')
);
const context = vm.createContext({console, $: () => ({}), $$: () => [], escapeHtml: String});
vm.runInContext(sliced, context, {filename: 'app.js'});
assert.ok(context.usageGroupRows([]).includes('尚无调用记录'));
assert.ok(context.usageGroupRows(undefined).includes('尚无调用记录'));
const table = context.usageGroupRows([{kind: 'chat', calls: 1, failed: 0, input_tokens: 4,
  input_tokens_known_calls: 1, output_tokens: 2, output_tokens_known_calls: 1,
  ttft_p50_ms: 12, latency_p50_ms: 30}]);
assert.ok(table.includes('usage-head') && table.includes('<div class="usage-row">'));
assert.ok(table.includes('12ms') && table.includes('30ms'));
''')


if __name__ == "__main__":
    unittest.main()
