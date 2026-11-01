# 答案级回归集

## 新封存草稿 holdout-v2（尚未人工复核或模型终评）

2026-10-29—30另建48题、8案、32页，六类各8题；不是向已打开旧24题追加题目后重新命名。正文及代理草稿答案保存在Git忽略的私有目录，仓库仅保留 `holdout-v2-protocol.json` 和 `holdout-v2-sealed-manifest.json`。两份空白人工标注表及不含代理答案的来源/问题交接包已生成，人工一致性保持未完成。

此处“8题”是覆盖下限，不是功效或显著性门槛，数据仍是代理原创的未审草稿。原始标注与开启条件见[执行记录](../../docs/history/migration/lexvault-evaluation-completion-20261029.md)，候选模型未运行这批新题。

## 已公开的开发/回归资产

`v1/demo-case.json` 保留原有 17 题。`v2/dataset.json` 冻结 60 题及内置合成演示案件的 14 页完整来源：正常 30、材料对照/矛盾 10、资料不足 8、无关问题 4、法条缺失 4、注入 4。注入题为问题中携带的攻击文本；卷宗/工具/历史/文件名注入仍由独立红队测试覆盖。

来源为仓库 `app.db.seed_demo`，全部为虚构材料。题目和规则由编码助手构造，**未经律师或独立人工标注**，不能称为人工 gold 或法律准确率。对照题要求并列呈现来源差异，不能把任一人的陈述当成已认定事实。

每题包含案情、问题、必需要点、可接受页、拒答预期和场景类别。v2 可回答题必须有可接受引用；关键词规则仍可能因同义表述或否定句产生误判。来源摘要证明冻结内容一致，不能证明事实或法律判断真实。

```sh
uv run --locked python scripts/build_answer_eval.py
uv run --locked python scripts/run_answer_eval.py --dataset benchmarks/answer_eval/v2/dataset.json --answers /path/to/answers.json --output output/answer-v2/report.json
```

构建/核对使用临时合成数据库，不接触运行数据，不调用模型。缺少冻结输出时显式 `--write`；已有产物不同则拒绝覆盖，应建立新版本及其生成规则。`manifest.json` 记录题目、来源、最终数据集摘要和分组数量。

答案级报告与页级检索评测分开。Judge 输出、人工评分和确定性规则各列报告，人工数据缺失时不计算“人工一致性已通过”。

## 六案例保留集 `holdout-v1`

`holdout-v1/cases.json` 是作者编写的 6 个独立虚构案例（建设工程、劳动争议、买卖质量异议、民间借贷、交通事故、股权转让），每案 2 份文书共 24 页、4 题，合计 24 题：正常 12、候选矛盾 6、资料不足 3、无关 2、法条缺失 1。案例与开发集在当事人、金额、文书名和问题上都不同，不共享任何页面。

`holdout-v1/dataset.json` 由 fixture 派生并冻结：来源逐页摘要、`source_sha256`、每题的 `case_id` 与 `case_description`。`scripts/build_holdout_eval.py` 额外校验“题目只能引用本案例页面”，并拒绝跨案例引用。

```sh
uv run --locked python scripts/build_holdout_eval.py            # 核对冻结产物
uv run --locked python scripts/run_collaboration_ablation.py --mode prepare \
  --dataset benchmarks/answer_eval/holdout-v1/dataset.json --output output/portfolio-ablation/holdout-v1
```

隔离库由 `dataset.json` 的 `cases` 直接播种（`init_db(seed=False)`），因此检索上下文与计分来源是同一份冻结文本；不使用开发集的演示案件。构建与核对不调用模型；已有产物不同则拒绝覆盖。

保留集只用于“候选冻结后”的复验。一旦用它的结果调整方法，它就转为开发数据，需要另建未见过的保留集。
