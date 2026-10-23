"""Offline fixtures are synthetic and never require redistributing LawBench."""
import json
from unittest.mock import patch

import pytest


@pytest.fixture(scope="session", autouse=True)
def synthetic_lawbench(tmp_path_factory):
    from app import lawbench
    from app.benchmark_metrics import task_label_space

    directory = tmp_path_factory.mktemp("synthetic-lawbench")
    references = {
        "1-2": ["正确答案：A。", "正确答案：B。"],
        "2-2": ["争议焦点类别：责任认定。", "争议焦点类别：合同效力。"],
        "2-3": ["类别:婚后有子女、支付抚养费。", "类别:不动产分割。"],
        "2-4": ["婚姻家庭", "劳动纠纷"],
        "2-8": ["[正确答案]A<eoa>", "[正确答案]B<eoa>"],
        "2-9": ["支付/给付;获利", "搜查/扣押"],
        "3-1": ["法条:刑法第264条"],
        "3-3": ["罪名:盗窃;诈骗", "罪名:过失损坏广播电视设施、公用电信设施"],
        "3-4": ["刑期:4个月"], "3-5": ["刑期:12个月"],
        "3-6": ["正确答案:A。"], "3-7": ["犯罪金额:1000元"],
    }
    for task in lawbench.TASK_NAMES:
        options = references.get(task, ["合成参考答案"])
        rows = [{"instruction": "这是工程测试的合成题。直接输出指定格式。",
                 "question": f"合成问题 {task}/{index}：用于测试装载、抽样及协议，不评估法律知识。",
                 "answer": options[index % len(options)]} for index in range(500)]
        (directory / f"{task}.json").write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    lawbench.load_task.cache_clear()
    task_label_space.cache_clear()
    with patch.object(lawbench, "LAW_BENCH_DIR", directory):
        yield
    lawbench.load_task.cache_clear()
    task_label_space.cache_clear()
