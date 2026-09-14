"""Compute Cohen's kappa, the confusion matrix and escape-rate for the 2-2 pilot.

Usage: python scripts/pilot_kappa.py output/pilot_2-2/annotations.csv
CSV columns: pilot_id,rater1,rater2  (labels: 责任认定/责任划分/责任承担/边界不清)
"""
from __future__ import annotations

import csv
import sys
from collections import Counter
from pathlib import Path

VALID = ("责任认定", "责任划分", "责任承担", "边界不清")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 1
    path = Path(argv[1])
    rows = [row for row in csv.DictReader(path.open(encoding="utf-8")) if row.get("pilot_id")]
    pairs = [(row["rater1"].strip(), row["rater2"].strip()) for row in rows]
    bad = [(row["pilot_id"], a, b) for row, (a, b) in zip(rows, pairs) if a not in VALID or b not in VALID]
    if bad:
        print("非法标注值:", bad[:5])
        return 1
    if not pairs:
        print("空文件")
        return 1

    observed = sum(a == b for a, b in pairs) / len(pairs)
    count1, count2 = Counter(a for a, _ in pairs), Counter(b for _, b in pairs)
    expected = sum(count1[label] * count2[label] for label in VALID) / len(pairs) ** 2
    kappa = (observed - expected) / (1 - expected) if expected != 1 else 0.0

    labels = [label for label in VALID if count1[label] or count2[label]]
    print(f"n={len(pairs)}  observed={observed:.3f}  expected={expected:.3f}")
    print(f"Cohen's kappa = {kappa:.3f}")
    print("判定:", "可分（≥0.6，可扩产）" if kappa >= 0.6 else
          "边界地带（0.4-0.6，先修订定义再测一轮）" if kappa >= 0.4 else
          "不可分（<0.4，SFT 线终结）")
    print("\n混淆矩阵（行=rater1，列=rater2）:")
    header = "            " + "".join(f"{label:>10}" for label in labels)
    print(header)
    for row_label in labels:
        cells = "".join(f"{sum(1 for a, b in pairs if a == row_label and b == col_label):>10}"
                        for col_label in labels)
        print(f"{row_label:>10}  {cells}")
    escape = sum(1 for pair in pairs for value in pair if value == "边界不清") / (2 * len(pairs))
    print(f"\n'边界不清'使用率: {escape:.1%}（>15% 视为三类边界证据不足）")
    disagreements = [(a, b) for a, b in pairs if a != b]
    print("分歧 top:", Counter(disagreements).most_common(6))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
