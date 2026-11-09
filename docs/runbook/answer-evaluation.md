# 答案评分、Judge 与独立人工盲审

v2 为编码助手从单一虚构案件构造的回归集，未经律师或独立人工标注。页级检索、确定性关键词、模型 judge、人工分数分别报告，均不能单独代表法律准确率。

## 离线采集及冻结盲审

```sh
uv run --locked python scripts/evaluation/build_answer_eval.py
uv run --locked python scripts/evaluation/run_answer_judge.py --collect-rules \
  --answers-output output/answer-review/rule-answers.json \
  --review-directory output/answer-review/blind \
  --review-archive output/answer-review/blind.zip \
  --output output/answer-review/rule-report.json
```

采集使用临时合成库，明确关闭模型和远端检索。也可用 `--answers` 载入已有答案；答案文件需显式记录 collection_mode，未知来源记为 user_supplied_unverified。输出拒绝覆盖，保留失败与历史版本。

`blind/sample-manifest.json` 冻结 20 题：每类先取两题，再按摘要顺序补足；样本绑定完整数据集与答案摘要，在模型或人工打分前生成。`blind-review.json` 含来源、题目、候选答案和评分标准，不含 judge/自动得分。审核人核对：要点是否完整、是否有来源不支持的断言、引用是否准确、需要拒答时是否正确拒答；整体四项均满足才标 passed=true。

转交时优先使用 ZIP：审核人先打开包内 `README.md`，再按 `REVIEW.md` 的冻结顺序阅读 20 题及独立来源全文，最后填写 `human-labels.template.json` 的副本。题本把问题/候选/来源作为纯文本展示，不执行其中的 HTML、Markdown 或指令。

`--review-archive` 需同时指定 `--review-directory`。ZIP 只收录上述说明/题本、三份 JSON 和 `PACK_MANIFEST.json` 的逐文件摘要；不收录目录内额外文件、自动评分报告或候选的额外生成元数据。模板已有通过/不通过标签、材料与样本不一致、符号链接和覆盖旧 ZIP 均拒绝打包。缺少人工时保持全部 passed=null，准备包本身不算完成 A08.1。

样本内摘要使用排序键、无额外空格的 UTF-8 JSON 计算；数据集 manifest 的文件摘要使用实际文件字节，两者编码口径不同，均可按脚本复算。

复制 `human-labels.template.json` 后由独立审核人填写 reviewer、每题 passed（严格 true/false）及 note；保持 sample_sha256，不删除困难题、不把模板的 null 或字符串转换成通过标签。`independent_human` 是提供者声明，脚本无法独立核实身份，不把程序生成标签称为人工标注。

## 显式运行真实 Judge

```sh
uv run --locked python scripts/evaluation/run_answer_judge.py \
  --answers output/answer-review/rule-answers.json --judge \
  --endpoint http://127.0.0.1:8000/v1 --judge-model ACTUAL_MODEL_ID \
  --output output/answer-review/judge-report.json
```

仅允许明确的回环端点，模型调用保留 `governed_backend`、出口校验、禁重定向 opener、读取截止时间。最多对冻结集各请求一次；首个不可用/无法解析/类型错误后停止后续调用，写入失败与未尝试状态并非零退出。不退回确定性分数充当 judge 通过。用量缺失和未配置价格为 null，不写零费用。

提示版本 `answer-judge-v2-frozen-sources`：题目、回答、引用均作为不可信 JSON 数据；独立证据来自冻结集全文，不以答案自报的 quote 证明答案。仅当响应通过严格布尔/列表/理由校验且实际提供独立来源时，逐题 semantic_entailment_checked 才可为 true；该字段表示执行过模型语义审查，并非人工确认正确。本工程未把此标志写回生产问答的引用契约，生产原标志保持适用。

## 从冻结结果离线复算 κ

```sh
uv run --locked python scripts/evaluation/run_answer_judge.py \
  --answers output/answer-review/rule-answers.json \
  --judge-results output/answer-review/judge-report.json \
  --human-labels output/answer-review/human-labels.json \
  --output output/answer-review/agreement-report.json
```

该命令不重发模型请求。数据集、答案、样本和提示版本必须匹配，缓存要保留所有逐题结果。至少 20 个预先冻结样本同时具有有效 judge 和人工标签才计算 κ，分歧及双方标签列示。人工缺失、judge 不完整、常量标签导致 κ 无定义时分别报告 missing_human_labels、judge_incomplete、degenerate_labels，κ 为 null，闭环验收保持未完成。

旧 `run_answer_eval.py --human-labels` 比较的是确定性规则与人工，报告已明确标记 deterministic_rules_not_judge；不能用该数值冒充 judge/人工一致性。
