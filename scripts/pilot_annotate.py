"""Interactive rater2 annotation for the 2-2 pilot. Resumable, writes CSV directly.

Usage:
    python3 scripts/pilot_annotate.py            # resume: skips already-annotated
    python3 scripts/pilot_annotate.py --restart  # re-annotate everything
    python3 scripts/pilot_annotate.py --limit 60 # stop after N new annotations this session

Keys: 1=责任认定  2=责任划分  3=责任承担  4=边界不清  s=跳过  q=保存退出  b=回退一题
Do NOT read the rater1 column first: independence is the whole point of the pilot.
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "output/pilot_2-2/pilot_cases.jsonl"
ANNOTATIONS = ROOT / "output/pilot_2-2/annotations.csv"

CHOICES = {"1": "责任认定", "2": "责任划分", "3": "责任承担", "4": "边界不清"}
MENU = ("  [1] 责任认定   争执「过错/因果关系的认定结论」是否正确\n"
        "  [2] 责任划分   争执「多方之间比例或主次」如何分配\n"
        "  [3] 责任承担   争执「由谁赔、是否赔、怎么赔」\n"
        "  [4] 边界不清   真实争执点不在上面三类内，或无法判定\n"
        "  [s] 跳过本题   [b] 回退上一题   [q] 保存并退出")


def load_cases() -> list[dict[str, str]]:
    import json
    return [json.loads(line) for line in CASES.read_text(encoding="utf-8").splitlines() if line.strip()]


def load_state() -> dict[str, str]:
    if not ANNOTATIONS.exists():
        return {}
    with ANNOTATIONS.open(encoding="utf-8") as handle:
        return {row["pilot_id"]: (row.get("rater2") or "").strip()
                for row in csv.DictReader(handle) if row.get("pilot_id")}


def save_state(cases: list[dict[str, str]], state: dict[str, str]) -> None:
    """Rewrite the CSV, always emitting the full 227 rows in fixed order."""
    with ANNOTATIONS.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["pilot_id", "rater1", "rater2", "note"])
        # Preserve the existing rater1 column so the pair stays aligned.
        rater1: dict[str, str] = {}
        if ANNOTATIONS.with_suffix(".csv.r1bak").exists():
            with ANNOTATIONS.with_suffix(".csv.r1bak").open(encoding="utf-8") as backup:
                rater1 = {row["pilot_id"]: row.get("rater1", "") for row in csv.DictReader(backup)}
        for case in cases:
            pilot_id = case["pilot_id"]
            writer.writerow([pilot_id, rater1.get(pilot_id, ""), state.get(pilot_id, ""), ""])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--restart", action="store_true", help="清空 rater2 重新标注")
    parser.add_argument("--limit", type=int, default=0, help="本次最多新增多少题（0=不限）")
    args = parser.parse_args(argv)

    cases = load_cases()
    # Keep a one-time backup of rater1 before the first rewrite.
    backup = ANNOTATIONS.with_suffix(".csv.r1bak")
    if ANNOTATIONS.exists() and not backup.exists():
        backup.write_bytes(ANNOTATIONS.read_bytes())
    state = {} if args.restart else load_state()
    todo = [case for case in cases if not state.get(case["pilot_id"])]
    if not todo:
        print("全部 227 题已标注完成。运行：python3 scripts/pilot_kappa.py output/pilot_2-2/annotations.csv")
        return 0

    print(f"待标注 {len(todo)} 题（已完成 {len(cases) - len(todo)}/{len(cases)}）")
    print("判据要点：看「双方真正争执的结论」是什么，不要被『赔偿』『承担』字样带跑。")
    print(MENU)
    done_this_session = 0
    index = 0
    while index < len(todo):
        case = todo[index]
        question = case["question"].replace("\n", " ")
        print("\n" + "─" * 78)
        print(f"[{case['pilot_id']}]  剩余 {len(todo) - index} 题")
        print(question)
        print("─" * 78)
        answer = input("你的标注 (1/2/3/4/s/b/q): ").strip().lower()
        if answer == "q":
            break
        if answer == "s":
            index += 1
            continue
        if answer == "b":
            index = max(0, index - 1)
            continue
        if answer not in CHOICES:
            print("  输入无效，请输入 1/2/3/4/s/b/q")
            continue
        state[case["pilot_id"]] = CHOICES[answer]
        save_state(cases, state)
        done_this_session += 1
        index += 1
        if args.limit and done_this_session >= args.limit:
            print(f"\n已达本次上限 {args.limit} 题，保存退出。")
            break

    save_state(cases, state)
    filled = sum(1 for case in cases if state.get(case["pilot_id"]))
    print(f"\n本次新增 {done_this_session} 题。总计 {filled}/{len(cases)}。")
    if filled == len(cases):
        print("已全部完成，运行：python3 scripts/pilot_kappa.py output/pilot_2-2/annotations.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
