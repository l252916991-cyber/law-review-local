import assert from "node:assert/strict";
import { test } from "node:test";
import { tokenText, costText, milliseconds } from "../src/lib/metrics.ts";
test("missing and partially observed tokens are distinct from a measured zero", () => {
  assert.equal(tokenText(null, 0, 3), "未知");
  assert.equal(tokenText(0, 3, 3), "0");
  assert.equal(tokenText(100, 1, 3), "≥100");
  assert.equal(milliseconds(null), "未知");
  assert.equal(milliseconds(0), "0ms");
});
test("unknown price and mixed currencies never look free or summed", () => {
  assert.equal(costText({ configured: false }), "未配置成本");
  assert.equal(costText({ configured: true, total: null }), "未知");
  assert.equal(
    costText({ total: null, partial_total: "0.1250 CNY" }),
    "≥0.1250 CNY（已知部分）",
  );
  assert.equal(costText({ total: "0.0000 CNY" }), "0.0000 CNY");
  assert.equal(
    costText({ total: "1.0000", mixed_currency: true }),
    "多币种，不求和",
  );
});
