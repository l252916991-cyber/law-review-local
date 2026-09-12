"""Quality gates for the hand-annotated 2-2 SFT pool (16 labels x 30)."""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from scripts.build_2_2_sft_data import POOL_PATH, build

# The 16 label names exactly as enumerated in the pinned 2-2 instruction.
LABEL_SPACE = {
    "诉讼主体", "租金情况", "利息", "本金争议", "责任认定", "责任划分",
    "损失认定及处理", "原审判决是否适当", "合同效力", "财产分割", "责任承担",
    "鉴定结论采信问题", "诉讼时效", "违约", "合同解除", "肇事逃逸",
}


def pool() -> list[dict[str, str]]:
    return [json.loads(line) for line in POOL_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_pool_structure_and_balance():
    entries = pool()
    assert len(entries) == 480
    labels = Counter(item["label"] for item in entries)
    assert set(labels) == LABEL_SPACE and set(labels.values()) == {30}
    sentences = [item["sentence"] for item in entries]
    assert len(sentences) == len(set(sentences)), "duplicate sentences in pool"
    ids = [item["id"] for item in entries]
    assert len(ids) == len(set(ids))
    assert all(set(item) == {"id", "label", "domain", "sentence"} and item["sentence"].strip()
               for item in entries)


def test_pool_leakage_against_pinned_questions():
    path = Path(__file__).resolve().parents[1] / "benchmarks/lawbench/zero_shot/2-2.json"
    if not path.exists():
        pytest.skip("pinned LawBench dataset not present")
    pinned = json.loads(path.read_text(encoding="utf-8"))
    corpus = "\n".join(row["question"] for row in pinned)
    for item in pool():
        assert not any(item["sentence"][i:i + 14] in corpus for i in range(0, max(1, len(item["sentence"]) - 13))), \
            f"pool example leaks pinned question text: {item['id']}"


def test_build_renders_benchmark_matched_chat_format(tmp_path):
    path = Path(__file__).resolve().parents[1] / "benchmarks/lawbench/zero_shot/2-2.json"
    if not path.exists():
        pytest.skip("pinned LawBench dataset not present")
    output = tmp_path / "train.jsonl"
    result = build(POOL_PATH, path, output)
    assert result["examples"] == 480
    first = json.loads(output.read_text(encoding="utf-8").splitlines()[0])
    roles = [message["role"] for message in first["messages"]]
    assert roles == ["system", "user", "assistant"]
    assert "本任务核对方法" in first["messages"][0]["content"]
    assert first["messages"][1]["content"].startswith("判断句子包含的争议焦点类别")
    assert first["messages"][2]["content"].startswith("[争议焦点]") and first["messages"][2]["content"].endswith("<eoa>")
