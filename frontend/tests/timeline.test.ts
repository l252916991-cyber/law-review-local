import assert from "node:assert/strict";
import { test } from "node:test";
import { materialTimeline } from "../src/lib/timeline.ts";
test("timeline orders material dates and preserves unknown precision without inventing a fact date", () => {
  const result = materialTimeline(
    [
      { id: 1, source_document_id: 10 },
      { id: 2, source_document_id: 20 },
      { id: 3 },
    ],
    [
      { id: 10, date_range: "2026-03 至 2026-04" },
      { id: 20, date_range: "2025-12-10" },
    ],
  );
  assert.deepEqual(
    result.map((item) => item.evidence.id),
    [2, 1, 3],
  );
  assert.equal(result[1].dateRange, "2026-03 至 2026-04");
  assert.equal(result[1].sortDate, "2026-03");
  assert.equal(result[2].sortDate, null);
  assert.equal(result[2].dateRange, "");
});
