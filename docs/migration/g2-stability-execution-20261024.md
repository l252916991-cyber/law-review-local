# G2 输出稳定性、误拒与用量修复实施记录

日期：2026-10-24（北京时间）。分支 `codex/g2-answerability`，编制基线 `1fed2d2284d589376243c6658f3308c71ab9a6b2`。本记录对应[实施计划](g2-stability-plan-20261024.md)的 S01～S06。

本批只使用合成材料和 loopback 本机模型，未恢复 35B、未启用付费推理、未切换生产路由、未降低内存守卫。**S07 未执行，完整 G2/E07 继续不通过。**

## 1. 结果摘要

| 项目 | 结果 | 证据 |
| --- | --- | --- |
| S01 十九题与四道阻塞题冻结、实际输入输出归因 | 完成 | `output/migration/g2-stability-20261024/s01/blocker-analysis.json` |
| S02 结构化输出能力验证 | 在隔离 oMLX 进程内实际启用 xgrammar；全局服务环境未改动 | `s02/structured-output-capability.json`、`s02/grammar-env-freeze.txt` |
| S03 “未见记录”判断边界 | 协议升级为 `candidate-answerability-v9-grammar`，区分明确未见、未提及、有记录和要求认定未发生 | `app/answerability.py`、`tests/test_answerability.py` |
| S04 用量压缩 | 结构化语法约束输出形状；系统提示和用户输入去除重复排版与 route/文件元数据；实际 39 次评估全部逐题及总量 ≤1.10 | `s05/actual-summary.json`、`s05/latency-usage-summary.json` |
| S05 冻结方案与合成复验 | 39 次题目评估、78 条双对照；25 成功 / 14 业务拒绝 / 0 系统失败，全部符合预期，unknown=0，源库不变 | `s05/final-full19/`、`s05/stability-holdout/`、`s05/final-blockers-repeat*/` |
| S06 工程、发布与新进程复用 | 三版 Python 各 987 passed / 0 failed / 3 skipped；静态、依赖 Export、发布允许清单及发布包重建流程通过 | `s06/tests-311|312|314/`、`s06/static-312-final/`、`s06/release-312-final/` |
| S07 真实材料 E07 | 未执行，等待材料目录、使用范围、快照和评审责任落实 | 无 |

## 2. S01 四道阻塞题归因

v8 十九题实际结果为 17/19 符合预期，38 条双对照中 34 条通过，候选总量比 1.0259，但逐题最大 1.1340。本批保留原 expected、原始响应和失败记录，未做回写。

| 题目 | v8 现象 | 归因 |
| --- | --- | --- |
| `tuition-paraphrase` | `sources` 数组缺少右括号，宿主严格解析失败 | 当时服务只注入 schema 提示，没有实际语法约束 |
| `repair-paraphrase` | 材料明确“未见维修完成或交付回执”，模型仍判 `insufficient` | “明确未见”和“完全未提及”边界未在提示中固定，也未由语法约束输出形状 |
| `delivery-broad` | 单题 token 比 1.1269 | 额外 answerability 调用的系统提示和 JSON 用户载荷重复、冗长 |
| `repair-broad` | 单题 token 比 1.1340 | 同上；主要开销在输入，不在输出 |

原始 v8 身份、摘要和响应相应文件仍保留在既有私有目录，见 S01 证据文件。

## 3. S02 结构化输出验证

- 全局 oMLX 0.7.0 所使用解释器没有 `xgrammar`。本批没有改全局环境，而是在独立的 Python 3.12 目录安装固定版本 `xgrammar==0.2.3`、`apache-tvm-ffi==0.1.11`，以单独 `PYTHONPATH` 启动临时 oMLX 进程，使用独立 `--base-path`、`--memory-guard safe`、并发 1、`--no-cache`。
- 服务对 `guided_grammar` 实际编译语法；成功响应没有 `Warning: 199 omlx "..."` 回退头，合法输出 `finish_reason=stop`。对照请求中的普通 JSON Schema 仍会把中文输出为 `\uXXXX` 转义并增加输出 token，因此最终选用自定义 EBNF，允许原始中文并限制输出形状。
- 最终语法在服务侧强制：`supported` 必须带 1～3 项 findings；`unrelated`、`insufficient` 的 findings 必须为空；来源编号 1～6；reason 最多 40 字符、finding 文本最多 200 字符。宿主仍独立执行严格 JSON、重复键、字段集合、来源范围、拒绝不得夹带 findings 和领域回答合同校验，服务语法不是宿主校验的替代。
- 缺少语法编译能力时，`guided_grammar` 请求以 HTTP 400 失败，按系统失败记录，不会计作业务拒绝；实际启用仅由显式 `LocalModels.answerability_grammar=True` 绑定，并进入模型/服务绑定指纹。

## 4. S03 “未见记录”边界

协议提示固定如下判断，且不按题名、案件编号或关键词硬编码：

- 材料明确写“未见/未记录某记录”，询问有无该记录：`supported`，text 写“本次材料未见……”，sources 指向该片段；不进一步推断事件没有发生。
- 只有明确记录时：按记录回答并保留来源。
- 片段完全没有提到该记录：`insufficient`，不把未召回等同于不存在。
- 明确未见回执但要求认定“没有交付/免除责任”：不作无依据推论，按 `insufficient` 或仅回答受支持部分处理。

新增宿主契约测试覆盖“明确未见”的限定范围成功、拒绝夹带 findings 的失败、策略版本变化拒绝和语法绑定开关。实际新保留集中 4 道正例“明确未见”题全部 success 且引用正确片段。

## 5. S04/S05 实际用量与行为

最终方案在独立最终服务批次中运行：原 19 题完整一次；`tuition-paraphrase`、`repair-paraphrase`、`delivery-broad`、`repair-broad` 各追加两次独立运行；新合成保留集 12 题一次。共 **39 次题目评估、78 条双对照**。服务日志记录 117 次 chat completion，等于 39×3 次候选/共享/独立 LLM 调用，全部调用存在且用量已知。

| 指标 | 结果 |
| --- | --- |
| 行为 | 25 成功、14 业务拒绝、0 系统失败；所有 39 次符合冻结 expected |
| 阻塞题重复 | 四题三次均 success，引用一致，token 比 0.8276～0.8900 |
| 新保留集 | 12/12 符合预期；正例、明确未见、未提及拒绝、无关拒绝均通过 |
| 候选调用 | 193 次后端调用；输入 25,213 token、输出 1,613 token、合计 26,826 |
| 原生独立链路 | 31,316 token |
| 总量比 | 0.8566 |
| 逐题 token 比 | 0.6909～0.9558；最近秩 p95=0.9387；全部 ≤1.10 |
| unknown usage | 0 |
| 源库/测量 | 所有报告 `source_unchanged=true`、`measurement_valid=true` |
| 经验墙钟时延 | 候选 p95 12.895 秒，原生独立 p95 14.934 秒，p95 比 0.8635；包含 loopback 独占驻留模型加载/卸载，仅描述固定合成批次 |
| 服务前后 | 用户 `~/.omlx/settings.json` SHA-256 相同；临时服务已停止；未触碰其他应用进程 |
| 口径 | `g2_accepted=false`；合成结果不能替代 E07/真实材料专业验收 |

全部逐调用账本、原始模型响应、冻结样本、服务日志和重复报告保存在 Git 忽略的 `output/migration/g2-stability-20261024/s05/`。

## 6. S06 工程与发布复验

本机 macOS 按当前 CI 同参数执行；托管 Ubuntu CI 未运行。Node 使用已核验官方包 22.23.4，二进制 SHA-256 `2cc28a1880d0774616959f2210ee739bffa6ac695c887e8988a496a21271f6c0`。

| 宿主 Python | 离线测试 | 含分支覆盖率 |
| --- | --- | --- |
| 3.11.15 | 987 passed / 0 failed / 3 skipped | 85.00% |
| 3.12.13 | 987 passed / 0 failed / 3 skipped | 85.01% |
| 3.14.6 | 987 passed / 0 failed / 3 skipped | 85.00% |

三项跳过为两份冻结法条语料未检出和宿主可选 `mlx_lm` 缺失，没有框架环境跳过。另用精确 ForgeHarness 0.8.3 wheel 与带 hash 运行依赖新建独立 Python 3.12 框架环境，`test_answerability` 等实际框架进程测试执行通过。

静态与工程检查通过：`doctor`、compileall、Ruff、Mypy（42 文件）、`uv pip check`、三份 requirements 导出一致性、四份前端脚本 Node 22 语法、发布允许清单 272 文件；私有运行数据与 LawBench 未进入发布包。

发布包重建验证 PASS：从源码包新建带 hash 宿主/框架环境；领域只读对照源库不变；宿主持久化结果由新进程复用；候选成功和业务拒绝由两次全新宿主进程复用相同 run/result，提供方调用、业务效果和回执不重复。该流程使用显式离线替身，`measurement_valid=false`，不构成真实模型质量通过。

最终候选身份为 `candidate-answerability-v9-grammar`，提示摘要 `e1fa56c02689e21c3d40ec719217bf1e7805ccbe28414b41caee0d133a4bcb45`，语法版本 `omlx-guided-grammar-v1`，语法摘要 `8112a8673333f3f7981a3535a66bf8590e669ebb0fd832ebec0d675a95d91ba3`。模型权重、数据库、原始响应、日志和最终发布包身份保存在 Git 忽略私有目录；最终代码提交后以其文件摘要为准。

## 7. 遗留与下一步

- S07/G2-06 仍未执行：真实开发材料目录、使用范围、快照和评审责任未落实；完整 G2 不通过。
- 生产 E12/E13、正式入口 E14、HTTP/UI E15、专业 E17 和切换仍按各自条件，不因本批合成通过而改状态。
- 本批新增服务语法依赖只存在于私有隔离环境；若后续正式部署要使用，需单独设计安装、版本固定、回退和发布清单，不得假定全局 oMLX 已具备该能力。
- 四道阻塞题和原 19 题仍是已见开发样本；新 12 题只在本批首次执行后成为已用样本，不能充当下一篇独立终评。

本批没有真实案卷、付费调用、外发、模型权重修改或生产开关变更。
