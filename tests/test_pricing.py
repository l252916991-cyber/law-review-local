"""Question-level cost estimation (A02.2): snapshot pricing, honest unknown buckets."""
import json
import os
import subprocess
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.db import db_scope, init_db
from app.llm_usage import record_llm_call, usage_summary
from app.main import app
from app.pricing import (
    PricingConfigError,
    PriceSnapshot,
    call_cost,
    format_money,
    load_pricing,
    parse_snapshot,
)

ROOT = Path(__file__).resolve().parents[1]

SNAPSHOT = {
    "version": "2026-10-25.1",
    "currency": "CNY",
    "unit_tokens": 1000000,
    "source": "测试用价目表",
    "models": {"model-a": {"input": "2.0", "output": "8.0"},
               "model-b": {"input": "1.0", "output": "1.0"}},
}


def pricing_env(snapshot=None, **overrides):
    payload = snapshot or SNAPSHOT
    environment = {"LAW_REVIEW_PRICING_JSON": json.dumps(payload, ensure_ascii=False)}
    environment.update(overrides)
    return patch.dict(os.environ, environment)


class PricingMathTests(unittest.TestCase):
    def test_hand_computed_call_cost(self):
        snapshot = parse_snapshot(SNAPSHOT)
        column = snapshot.column_for("model-a")
        # 1000 input tokens at 2.0 / 1M = 0.002; 500 output at 8.0 / 1M = 0.004.
        self.assertEqual(call_cost(column, 1000, 500), Decimal("0.00600000"))
        self.assertEqual(format_money(call_cost(column, 1000, 500), "CNY"), "0.0060 CNY")

    def test_unknown_tokens_or_price_never_become_zero(self):
        snapshot = parse_snapshot(SNAPSHOT)
        column = snapshot.column_for("model-a")
        self.assertIsNone(call_cost(column, None, None))
        self.assertIsNone(call_cost(None, 1000, 500))
        self.assertIsNone(call_cost(column, None, None))
        # Only one side known is still priced for that side.
        self.assertEqual(call_cost(column, 1000, None), Decimal("0.00200000"))

    def test_unpriced_model_has_no_snapshot_column(self):
        snapshot = parse_snapshot(SNAPSHOT)
        self.assertIsNone(snapshot.column_for("unknown-model"))

    def test_configuration_errors_fail_closed(self):
        for payload in (
            {"version": "v", "currency": "CNY", "source": "s", "unit_tokens": 1000000, "models": {}},
            {"version": "", "currency": "CNY", "source": "s", "unit_tokens": 1000000,
             "models": {"m": {"input": "1", "output": "1"}}},
            {"version": "v", "currency": "CNY", "source": "s", "unit_tokens": 0,
             "models": {"m": {"input": "1", "output": "1"}}},
            {"version": "v", "currency": "CNY", "source": "s", "unit_tokens": 1000000,
             "models": {"m": {"input": "-1", "output": "1"}}},
            {"version": "v", "currency": "CNY", "source": "s", "unit_tokens": 1000000,
             "models": {"m": {"input": "abc", "output": "1"}}},
        ):
            with self.subTest(payload=payload), self.assertRaises(PricingConfigError):
                parse_snapshot(payload)

    def test_unset_configuration_is_not_an_error(self):
        with patch.dict(os.environ, {"LAW_REVIEW_PRICING_JSON": "", "LAW_REVIEW_PRICING_FILE": ""}):
            self.assertEqual(load_pricing(), (None, None))

    def test_invalid_configuration_reports_why(self):
        with patch.dict(os.environ, {"LAW_REVIEW_PRICING_JSON": "{not json"}):
            snapshot, error = load_pricing()
        self.assertIsNone(snapshot)
        self.assertIn("不可用", error or "")


class CostAccountingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="lexvault-cost-")
        self.addCleanup(self.directory.cleanup)
        scope = db_scope(Path(self.directory.name) / "law_review.db")
        scope.__enter__()
        self.addCleanup(scope.__exit__, None, None, None)
        init_db(seed=True)

    def _call(self, **kwargs):
        record_llm_call(case_id=1, latency_ms=10, **kwargs)

    def test_complete_usage_produces_an_exact_total(self):
        with pricing_env():
            self._call(kind="chat", model="model-a", usage={"prompt_tokens": 1000, "completion_tokens": 500}, turn_key="turn-1")
        cost = usage_summary(1)["cost"]
        self.assertEqual(cost["total"], "0.0060 CNY")
        self.assertEqual(cost["partial_total"], "0.0060 CNY")
        self.assertEqual(cost["known_calls"], 1)
        self.assertEqual(cost["version"], "2026-10-25.1")
        self.assertEqual(cost["currency"], "CNY")
        self.assertEqual(cost["unit_tokens"], 1000000)

    def test_missing_usage_keeps_the_total_unknown_but_reports_the_known_part(self):
        with pricing_env():
            self._call(kind="chat", model="model-a", usage={"prompt_tokens": 1000, "completion_tokens": 500}, turn_key="turn-1")
            self._call(kind="chat", model="model-a", usage=None, turn_key="turn-1", status="failed")
        cost = usage_summary(1)["cost"]
        self.assertIsNone(cost["total"])
        self.assertEqual(cost["partial_total"], "0.0060 CNY")
        self.assertEqual(cost["known_calls"], 1)
        self.assertEqual(cost["usage_missing_calls"], 1)

    def test_unpriced_model_is_reported_separately_from_missing_usage(self):
        with pricing_env():
            self._call(kind="chat", model="model-z", usage={"prompt_tokens": 10, "completion_tokens": 5})
        cost = usage_summary(1)["cost"]
        self.assertIsNone(cost["total"])
        self.assertEqual(cost["no_price_calls"], 1)
        self.assertEqual(cost["usage_missing_calls"], 0)

    def test_one_sided_usage_reports_only_a_lower_bound_for_call_and_turn(self):
        for usage, expected in (({"prompt_tokens": 1000}, "0.0020 CNY"),
                                ({"completion_tokens": 500}, "0.0040 CNY")):
            with self.subTest(usage=usage), pricing_env():
                with db_scope(Path(self.directory.name) / f"partial-{len(usage)}-{next(iter(usage))}.db"):
                    init_db(seed=True)
                    self._call(kind="chat", model="model-a", usage=usage, turn_key="partial-turn")
                    summary = usage_summary(1)
                self.assertIsNone(summary["cost"]["total"])
                self.assertEqual(summary["cost"]["partial_total"], expected)
                self.assertEqual(summary["cost"]["known_calls"], 0)
                self.assertEqual(summary["cost"]["usage_missing_calls"], 1)
                self.assertIsNone(summary["turns"][0]["cost"])
                self.assertEqual(summary["turns"][0]["partial_cost"], expected)
                self.assertEqual(summary["turns"][0]["unknown_calls"], 1)

    def test_mixed_currency_turn_has_no_partial_sum(self):
        other = json.loads(json.dumps(SNAPSHOT))
        other["currency"] = "USD"
        with pricing_env():
            self._call(kind="critic", model="model-a", usage={"prompt_tokens": 10, "completion_tokens": 5}, turn_key="mixed-turn")
        with pricing_env(other):
            self._call(kind="critic", model="model-b", usage={"prompt_tokens": 10, "completion_tokens": 5}, turn_key="mixed-turn")
        turn = usage_summary(1)["turns"][0]
        self.assertTrue(turn["mixed_currency"])
        self.assertIsNone(turn["cost"])
        self.assertIsNone(turn["partial_cost"])

    def test_price_changes_do_not_rewrite_history(self):
        with pricing_env():
            self._call(kind="chat", model="model-a", usage={"prompt_tokens": 1000, "completion_tokens": 500}, turn_key="turn-1")
        doubled = json.loads(json.dumps(SNAPSHOT))
        doubled["version"] = "2026-10-26.1"
        doubled["models"]["model-a"] = {"input": "4.0", "output": "16.0"}
        with pricing_env(doubled):
            cost = usage_summary(1)["cost"]
        self.assertEqual(cost["partial_total"], "0.0060 CNY")
        self.assertEqual(cost["versions"], ["2026-10-25.1"])

    def test_two_currencies_are_never_summed(self):
        cny = json.loads(json.dumps(SNAPSHOT))
        usd = json.loads(json.dumps(SNAPSHOT))
        usd.update({"version": "usd-1", "currency": "USD"})
        with pricing_env(cny):
            self._call(kind="chat", model="model-a", usage={"prompt_tokens": 1000, "completion_tokens": 0})
        with pricing_env(usd):
            self._call(kind="chat", model="model-b", usage={"prompt_tokens": 1000, "completion_tokens": 0})
        cost = usage_summary(1)["cost"]
        self.assertTrue(cost["mixed_currency"])
        self.assertIsNone(cost["total"])
        self.assertIsNone(cost["partial_total"])

    def test_turn_reports_group_the_calls_of_one_question(self):
        with pricing_env():
            self._call(kind="critic", model="model-a", usage={"prompt_tokens": 1000, "completion_tokens": 0}, turn_key="turn-1")
            self._call(kind="critic_fallback", model="model-b", usage={"prompt_tokens": 1000, "completion_tokens": 0}, turn_key="turn-1")
            self._call(kind="chat", model="model-a", usage={"prompt_tokens": 1000, "completion_tokens": 0}, turn_key="turn-2")
        summary = usage_summary(1)
        self.assertEqual(summary["turn_count"], 2)
        turn_one = next(row for row in summary["turns"] if row["turn_key"] == "turn-1")
        self.assertEqual(turn_one["calls"], 2)
        self.assertEqual(turn_one["kinds"], ["critic", "critic_fallback"])
        # 1000 @ 2.0/1M + 1000 @ 1.0/1M = 0.002 + 0.001
        self.assertEqual(turn_one["cost"], "0.0030 CNY")
        self.assertEqual(turn_one["unknown_calls"], 0)

    def test_agent_calls_are_grouped_by_run_when_no_turn_key_is_given(self):
        with pricing_env():
            self._call(kind="tool_loop", model="model-a", usage={"prompt_tokens": 10, "completion_tokens": 0}, run_id=None)
        self.assertIsNone(usage_summary(1)["turns"][0]["turn_key"])

    def test_a_turn_with_an_unknown_call_reports_no_closed_cost(self):
        with pricing_env():
            self._call(kind="chat", model="model-a", usage={"prompt_tokens": 1000, "completion_tokens": 0}, turn_key="turn-1")
            self._call(kind="chat", model="model-a", usage=None, turn_key="turn-1")
        turn = usage_summary(1)["turns"][0]
        self.assertIsNone(turn["cost"])
        self.assertEqual(turn["partial_cost"], "0.0020 CNY")
        self.assertEqual(turn["unknown_calls"], 1)

    def test_unconfigured_pricing_shows_no_cost_not_zero(self):
        with patch.dict(os.environ, {"LAW_REVIEW_PRICING_JSON": "", "LAW_REVIEW_PRICING_FILE": ""}):
            self._call(kind="chat", model="model-a", usage={"prompt_tokens": 1000, "completion_tokens": 500}, turn_key="turn-1")
            cost = usage_summary(1)["cost"]
        self.assertFalse(cost["configured"])
        self.assertIsNone(cost["total"])
        self.assertIsNone(cost["partial_total"])
        self.assertEqual(cost["known_calls"], 0)


class CostApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="lexvault-cost-api-")
        self.addCleanup(self.directory.cleanup)
        scope = db_scope(Path(self.directory.name) / "law_review.db")
        scope.__enter__()
        self.addCleanup(scope.__exit__, None, None, None)
        init_db(seed=True)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def test_platform_metrics_exposes_the_cost_block(self):
        with pricing_env():
            record_llm_call(case_id=1, kind="chat", model="model-a",
                            usage={"prompt_tokens": 2000, "completion_tokens": 1000},
                            latency_ms=10, turn_key="request:abc")
        payload = self.client.get("/api/cases/1/platform-metrics").json()["llm_usage"]
        self.assertEqual(payload["cost"]["total"], "0.0120 CNY")
        self.assertEqual(payload["cost"]["known_calls"], 1)
        self.assertEqual(payload["turns"][0]["turn_key"], "request:abc")


class CostPanelRenderTests(unittest.TestCase):
    """Drive the real renderer with a stub DOM; no browser required."""

    def _run(self, body: str) -> None:
        harness = r'''
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
const context = vm.createContext({console, $, $$, escapeHtml, Number, String});
vm.runInContext(sliced, context, {filename: 'app.js'});
const text = id => $(`#${id}`).textContent;
const html = id => $(`#${id}`).innerHTML;
'''
        script = harness + "(async () => {\n" + body + "\n})().catch(e => {console.error(e); process.exitCode = 1;});\n"
        result = subprocess.run(["node", "-e", script], cwd=ROOT, text=True, capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_unconfigured_pricing_never_renders_zero_money(self):
        self._run(r'''
  context.renderCost({calls: 2, turn_count: 1, turns: [{turn_key: 't1', calls: 2, models: ['m'], known_calls: 0, unknown_calls: 2, cost: null, partial_cost: null}],
    cost: {configured: false, config_error: null, currency: null, mixed_currency: false, unit_tokens: null,
           version: null, versions: [], source: null, calls: 2, known_calls: 0, no_price_calls: 2,
           usage_missing_calls: 0, partial_total: null, total: null}});
  assert.equal(text('usage-cost-total'), '—');
  assert.equal(text('usage-cost-note'), '2 次调用无单价（未配置成本）');
  assert.equal(text('usage-cost-scope'), '未配置成本');
  assert.ok(!html('usage-turns').includes('0.0000'));
''')

    def test_complete_and_partial_totals_are_labelled(self):
        self._run(r'''
  const base = {configured: true, config_error: null, currency: 'CNY', mixed_currency: false,
    unit_tokens: 1000000, version: '2026-10-25.1', versions: ['2026-10-25.1'], source: 's',
    calls: 2, known_calls: 2, no_price_calls: 0, usage_missing_calls: 0};
  context.renderCost({calls: 2, turn_count: 1, turns: [], cost: {...base, partial_total: '0.0120 CNY', total: '0.0120 CNY'}});
  assert.equal(text('usage-cost-total'), '0.0120 CNY');
  assert.equal(text('usage-cost-note'), '全部 2 次调用');
  assert.equal(text('usage-cost-coverage'), '2/2');
  assert.equal(text('usage-cost-scope'), '定价版本 2026-10-25.1');

  context.renderCost({calls: 3, turn_count: 1, turns: [], cost: {...base, calls: 3, known_calls: 2,
    usage_missing_calls: 1, partial_total: '0.0120 CNY', total: null}});
  assert.equal(text('usage-cost-total'), '≥0.0120 CNY');
  assert.equal(text('usage-cost-note'), '已知部分 · 2/3 次调用可计价');
  assert.equal(text('usage-cost-coverage'), '2/3');
''')

    def test_mixed_currency_and_config_errors_are_explicit(self):
        self._run(r'''
  context.renderCost({calls: 2, turn_count: 0, turns: [], cost: {configured: true, config_error: null,
    currency: null, mixed_currency: true, unit_tokens: null, version: null, versions: ['a','b'], source: null,
    calls: 2, known_calls: 2, no_price_calls: 0, usage_missing_calls: 0, partial_total: null, total: null}});
  assert.equal(text('usage-cost-total'), '—');
  assert.equal(text('usage-cost-note'), '多币种不可合计');

  context.renderCost({calls: 1, turn_count: 0, turns: [], cost: {configured: false,
    config_error: '定价配置不可用（PricingConfigError）', currency: null, mixed_currency: false,
    unit_tokens: null, version: null, versions: [], source: null, calls: 1, known_calls: 0,
    no_price_calls: 1, usage_missing_calls: 0, partial_total: null, total: null}});
  assert.equal(text('usage-cost-scope'), '定价配置不可用');
  assert.match(text('usage-cost-note'), /定价配置不可用/);
''')

    def test_turn_rows_escape_the_question_key(self):
        self._run(r'''
  context.renderCost({calls: 1, turn_count: 1, turns: [{turn_key: 'request:<script>', calls: 1,
    models: ['m'], known_calls: 1, unknown_calls: 0, cost: '0.0020 CNY', partial_cost: '0.0020 CNY'}],
    cost: {configured: true, config_error: null, currency: 'CNY', mixed_currency: false, unit_tokens: 1000000,
      version: 'v1', versions: ['v1'], source: 's', calls: 1, known_calls: 1, no_price_calls: 0,
      usage_missing_calls: 0, partial_total: '0.0020 CNY', total: '0.0020 CNY'}});
  assert.ok(html('usage-turns').includes('request:&lt;script&gt;'));
  assert.ok(html('usage-turns').includes('0.0020 CNY'));
  assert.equal(text('usage-turn-count'), '1');
''')

    def test_missing_summary_is_not_shown_as_unconfigured(self):
        self._run(r'''
  context.renderCost(undefined);
  assert.equal(text('usage-cost-total'), '—');
  assert.equal(text('usage-cost-note'), '指标缺失');
  assert.equal(text('usage-cost-scope'), '指标不可用');
''')


if __name__ == "__main__":
    unittest.main()
