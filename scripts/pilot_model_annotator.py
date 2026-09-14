"""Independent model annotator for the 2-2 pilot (rater2 slot).

Uses the UNTUNED base model with a neutral prompt, so its labels are not derived from
the rater1 pass. This is process agreement (model vs careful human read), NOT human
human kappa; it measures whether the three responsibility labels are recoverable by a
straightforward independent reading of the same text.

Writes the rater2 column of annotations.csv. Run from the repo root.
"""
from __future__ import annotations

import argparse
import csv
import json
import subprocess
import time
import urllib.request
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "output/pilot_2-2/pilot_cases.jsonl"
ANNOTATIONS = ROOT / "output/pilot_2-2/annotations.csv"
URL = "http://127.0.0.1:8000/v1/chat/completions"
MODEL = "Qwythos-9B-v2-8bit-mlx"
VALID = ("责任认定", "责任划分", "责任承担", "边界不清")

SYSTEM = (
    "你是法律文书分析助手。阅读一段民事案件的诉请与抗辩，判断双方真正争执的核心结论属于哪一类，"
    "只输出一个类别名，不要解释。类别：\n"
    "责任认定：争执过错或因果关系的认定结论本身是否正确。\n"
    "责任划分：认可有责任，争执多方之间的比例或主次如何分配。\n"
    "责任承担：争执由谁承担赔偿、是否承担、以何种方式承担。\n"
    "边界不清：真实争执点不在上述三类内（如赔偿数额计算标准、合同效力、程序问题），或无法判定。\n"
    "只输出这四个词中的一个。"
)


def classify(question: str) -> str:
    body = json.dumps({
        "model": MODEL, "temperature": 0.0, "max_tokens": 16, "stream": False,
        "chat_template_kwargs": {"enable_thinking": False},
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": question}],
    }, ensure_ascii=False).encode()
    request = urllib.request.Request(URL, data=body, headers={"Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=180) as response:
        payload = json.load(response)
    text = payload["choices"][0]["message"].get("content") or ""
    for label in VALID:
        if label in text:
            return label
    return ""


def write_rows(cases: list[dict[str, str]], rater1: dict[str, str], results: dict[str, str]) -> None:
    """Rewrite the full CSV after every batch, so a kill never loses progress."""
    with ANNOTATIONS.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["pilot_id", "rater1", "rater2", "note"])
        for case in cases:
            pid = case["pilot_id"]
            note = "rater2=未微调基座模型独立标注" if results.get(pid) else ""
            writer.writerow([pid, rater1.get(pid, ""), results.get(pid, ""), note])


def free_mb() -> int:
    """Free + compressed pages, MB. Wired GPU memory is NOT swappable, so watch this."""
    out = subprocess.run(["vm_stat"], capture_output=True, text=True).stdout
    total = 0
    for line in out.splitlines():
        if "Pages free" in line or "occupied by compressor" in line:
            total += int(line.split(":")[1].strip().rstrip(".")) * 16384
    return total // 1048576


def wait_for_headroom(floor_mb: int = 400, timeout_s: int = 120) -> None:
    """Block until the machine has breathing room; a stall here is cheaper than a freeze."""
    waited = 0
    while free_mb() < floor_mb and waited < timeout_s:
        time.sleep(5)
        waited += 5


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=0, help="本次最多新增多少题（0=全部）")
    parser.add_argument("--delay", type=float, default=0.5, help="每题之间的间隔秒数，给 GPU/缓存留喘息")
    args = parser.parse_args()

    cases = [json.loads(line) for line in CASES.read_text(encoding="utf-8").splitlines() if line.strip()]
    rater1 = {row["pilot_id"]: row["rater1"] for row in csv.DictReader(ANNOTATIONS.open(encoding="utf-8"))}
    # Resume: keep any rater2 already written by an earlier interrupted run.
    results = {row["pilot_id"]: (row.get("rater2") or "").strip()
               for row in csv.DictReader(ANNOTATIONS.open(encoding="utf-8"))
               if (row.get("rater2") or "").strip()}
    pending = [case for case in cases if not results.get(case["pilot_id"])]
    if args.limit:
        pending = pending[:args.limit]
    if not pending:
        print("rater2 已全部完成")
    for index, case in enumerate(pending, 1):
        wait_for_headroom()
        results[case["pilot_id"]] = classify(case["question"]) or "边界不清"
        print(f"[{index}/{len(pending)}] {case['pilot_id']} model={results[case['pilot_id']]} "
              f"free={free_mb()}MB rater1={rater1.get(case['pilot_id'],'')}", flush=True)
        if index % 10 == 0:
            write_rows(cases, rater1, results)
        time.sleep(args.delay)
    write_rows(cases, rater1, results)

    # Score only the questions that actually have both labels (partial runs included).
    pairs = [(rater1[c["pilot_id"]], results[c["pilot_id"]])
             for c in cases if results.get(c["pilot_id"]) and rater1.get(c["pilot_id"])]
    filled = len(pairs)
    observed = sum(a == b for a, b in pairs) / filled
    c1, c2 = Counter(a for a, _ in pairs), Counter(b for _, b in pairs)
    expected = sum(c1[label] * c2[label] for label in VALID) / filled ** 2
    kappa = (observed - expected) / (1 - expected)
    print(f"\n已标 {len(results)}/{len(cases)} 题；参与计分 {filled} 题")
    print(f"模型 vs rater1：一致率 {observed:.3f}  kappa {kappa:.3f}")
    print("rater1 分布:", dict(c1))
    print("模型 分布:", dict(c2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
