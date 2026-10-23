"""Quality gates for the hand-annotated 2-2 SFT pool (16 labels x 30)."""
from __future__ import annotations

import builtins
import json
from collections import Counter
from pathlib import Path

import pytest

from scripts.build_2_2_sft_data import POOL_PATH, build, render

# The 16 label names exactly as enumerated in the pinned 2-2 instruction.
LABEL_SPACE = {
    "诉讼主体", "租金情况", "利息", "本金争议", "责任认定", "责任划分",
    "损失认定及处理", "原审判决是否适当", "合同效力", "财产分割", "责任承担",
    "鉴定结论采信问题", "诉讼时效", "违约", "合同解除", "肇事逃逸",
}


class FakeTokenizer:
    def __init__(self, prompt: str = "rendered prompt<think>\n\n</think>\n\n") -> None:
        self.prompt = prompt
        self.calls: list[tuple[list[dict[str, str]], dict[str, bool]]] = []

    def apply_chat_template(
        self,
        messages: list[dict[str, str]],
        *,
        tokenize: bool,
        add_generation_prompt: bool,
        enable_thinking: bool,
    ) -> str:
        self.calls.append((messages, {
            "tokenize": tokenize,
            "add_generation_prompt": add_generation_prompt,
            "enable_thinking": enable_thinking,
        }))
        return self.prompt


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


def test_render_passes_benchmark_messages_and_options_to_tokenizer():
    tokenizer = FakeTokenizer()

    rendered = render(tokenizer, "识别争议焦点", "双方对本金数额有争议。", "本金争议")

    assert rendered == {
        "prompt": tokenizer.prompt,
        "completion": "[争议焦点]本金争议<eoa>",
    }
    assert len(tokenizer.calls) == 1
    messages, options = tokenizer.calls[0]
    assert messages[0]["role"] == "system"
    assert "本任务核对方法" in messages[0]["content"]
    assert messages[1] == {
        "role": "user",
        "content": "识别争议焦点\n句子:双方对本金数额有争议。",
    }
    assert options == {
        "tokenize": False,
        "add_generation_prompt": True,
        "enable_thinking": False,
    }


def test_render_rejects_open_or_missing_think_suffix():
    tokenizer = FakeTokenizer("rendered prompt<think>\n")

    with pytest.raises(AssertionError, match="benchmark rendering drifted"):
        render(tokenizer, "识别争议焦点", "合同是否有效。", "合同效力")


def test_build_without_mlx_keeps_training_environment_error(tmp_path, monkeypatch):
    pool_path = tmp_path / "pool.jsonl"
    pool_path.write_text(json.dumps({
        "id": "synthetic-1",
        "label": "合同效力",
        "domain": "合同",
        "sentence": "合同是否有效。",
    }, ensure_ascii=False) + "\n", encoding="utf-8")
    pinned_path = tmp_path / "pinned.json"
    pinned_path.write_text(json.dumps([{
        "instruction": "识别争议焦点",
        "question": "仅用于测试的问题",
    }], ensure_ascii=False), encoding="utf-8")

    real_import = builtins.__import__

    def import_without_mlx(name, *args, **kwargs):
        if name == "mlx_lm":
            raise ImportError("synthetic missing optional dependency")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_mlx)
    with pytest.raises(SystemExit, match="needs mlx_lm"):
        build(pool_path, pinned_path, tmp_path / "train.jsonl")


def test_build_renders_benchmark_matched_chat_format(tmp_path):
    pytest.importorskip("mlx_lm")
    path = Path(__file__).resolve().parents[1] / "benchmarks/lawbench/zero_shot/2-2.json"
    if not path.exists():
        pytest.skip("pinned LawBench dataset not present")
    output = tmp_path / "train.jsonl"
    result = build(POOL_PATH, path, output)
    assert result["examples"] == 480
    first = json.loads(output.read_text(encoding="utf-8").splitlines()[0])
    assert set(first) == {"prompt", "completion"}
    assert first["prompt"].endswith("<|im_start|>assistant\n<think>\n\n</think>\n\n")
    assert "本任务核对方法" in first["prompt"]
    assert first["completion"].startswith("[争议焦点]") and first["completion"].endswith("<eoa>")
