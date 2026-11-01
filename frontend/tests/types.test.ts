import assert from "node:assert/strict";
import test from "node:test";
import { objectOf, rowsOf, type SearchHit } from "../src/lib/types";

test("typed rows keep the runtime shape guard", () => {
  assert.deepEqual(rowsOf<SearchHit>(null), []);
  assert.deepEqual(rowsOf<SearchHit>(undefined), []);
  const hits = rowsOf<SearchHit>([{ page_id: 1, name: "借条.txt", page_no: 2 }]);
  assert.equal(hits[0].page_no, 2);
  assert.throws(() => rowsOf<SearchHit>({ page_id: 1 }), /列表格式无效/);
  assert.throws(() => rowsOf<SearchHit>([1, 2]), /格式无效/);
});

test("typed objects reject arrays and primitives", () => {
  assert.equal(objectOf<{ id: number }>({ id: 3 }).id, 3);
  assert.throws(() => objectOf([]), /格式无效/);
  assert.throws(() => objectOf("text"), /格式无效/);
});
