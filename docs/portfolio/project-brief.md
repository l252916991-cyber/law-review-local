# 项目说明（一页版）

> 面向 AI 应用开发 / Agent 工程岗位的个人项目材料。所有数字均标注来源；未测项保留为未测。

## 1. 一句话定位

本地优先的法律材料审阅工作台：把“检索 → 多专家结构化交接 → 交叉核对 → 审校生成 → 引用校验 → 人工复核 → 版本化导出”做成一条**可恢复、可校验、可对照实验**的流水线，而不是一次性的聊天封装。

边界先说清楚：专家节点是**确定性规则**，不是多个自主 LLM；来源一致性检查**不等于**语义蕴含、独立印证或律师确认；当前评测集来自**单一虚构案件**。

## 2. 架构与数据流

```
                 ┌─────────────── FastAPI（单实例，回环/内网） ───────────────┐
 合成/授权材料 → │ 解析入库(pages) → 检索(BM25+向量+RRF) → 专家层(规则)        │
                 │        │                    │                            │
                 │        │              facts / evidence / contradiction / gap
                 │        │                    │                            │
                 │        └──────→ 交叉核对(来源绑定校验) → 审校节点(LLM)     │
                 │                                   │                        │
                 │            引用契约校验 → 分歧附录 → 记忆准入(仅合格结果)    │
                 └───────────────────────────┬────────────────────────────────┘
                                             ↓
                        人工复核（可批准不可编制）→ 报告冻结正文 → 导出包+清单哈希
```

- **两条运行时**：原生 DAG 与 LangGraph；共享同一份专家规格与交接路径，结果需一致。
- **恢复校验**：checkpoint 记录每页 `page_text_sha256`；来源页改动后拒绝复用旧分析，不消耗重试次数。
- **受控检索**：模型拿到的只是本轮 6 页以内的片段，法条走独立命名空间，不进案件事实提示。

## 3. 技术栈

| 层 | 选型 |
|---|---|
| 服务 | FastAPI + Uvicorn，SQLite（WAL），单实例本地/受控内网 |
| 编排 | 原生 DAG + LangGraph（SQLite checkpoint），共享 `SubAgentSpec` |
| 检索 | 关键词 BM25 类打分 + 本地 embedding + RRF 融合，结果带 `page_text_sha256` |
| 生成 | OpenAI 兼容本地端点（`/v1/chat/completions`），仅回环端点，禁重定向 |
| 前端 | Next.js 静态构建，由 FastAPI 托管，Playwright 端到端 |
| 质量 | pytest + 覆盖率门禁、ruff、mypy（脚本）、离线可复算的评分与盲审包 |
| 实验 | 三组 A/B/C 隔离驱动（`app/collaboration_ablation.py`），逐题冻结提示与账本 |

## 4. 运行步骤（源码路径，已实测）

```sh
uv run --locked python scripts/package_release.py --output output/portfolio-repro/lexvault-source-YYYYMMDD.zip
unzip -q output/portfolio-repro/lexvault-source-YYYYMMDD.zip -d /tmp/portfolio-repro
cd /tmp/portfolio-repro && uv sync --locked --no-dev --offline

LAW_REVIEW_DATA_DIR=/tmp/repro-data LAW_REVIEW_LANGGRAPH_CHECKPOINT_DB=/tmp/repro-data/checkpoints.sqlite \
LAW_REVIEW_AUTH_MODE=token LAW_REVIEW_ALLOWED_HOSTS=127.0.0.1,localhost \
LAW_REVIEW_API_TOKENS_JSON='{"<32 字节以上随机令牌>": {"name": "管理员", "admin": true}}' \
  uv run --locked uvicorn app.main:app --host 127.0.0.1 --port 18766

LAW_REVIEW_SMOKE_TOKEN=<同一令牌> uv run --locked python scripts/delivery_smoke.py \
  --base-url http://127.0.0.1:18766 --mode offline
```

预期输出：`{"case_id": …, "cited_page": 2, "model_used": false, "export_sha256": "…"}`。

三组对照（无需模型也可复现到“冻结提示”这一步）：

```sh
uv run --locked python scripts/run_collaboration_ablation.py --mode prepare \
  --output output/portfolio-ablation/dev-YYYYMMDD
uv run --locked python scripts/run_collaboration_ablation.py --mode precheck \
  --output output/portfolio-ablation/dev-YYYYMMDD
```

恢复/拒用片段：

```sh
uv run --locked python scripts/demo_recovery_check.py --phase interrupt --data-dir /tmp/demo/case --output /tmp/demo/01.json
uv run --locked python scripts/demo_recovery_check.py --phase resume --data-dir /tmp/demo/case --output /tmp/demo/02.json --state /tmp/demo/01.json
uv run --locked python scripts/demo_recovery_check.py --phase tamper --data-dir /tmp/demo/tamper --output /tmp/demo/03.json
```

## 4.1 质量基线（截至 2026-11-01，提交 `{{COMMIT}}`）

| 检查 | 结果 |
|---|---|
| 全量离线测试（隔离数据目录、禁网、覆盖率，显式指定冻结法条资产） | Python 3.11：{{T311}}；Python 3.14：{{T314}}；门槛 70% |
| 静态检查 | ruff（E9/F/I/UP/B）、mypy（`scripts/` 严格 + `app/` 宽松，22 个存量模块列入只减不增名单）通过 |
| 前端 | `contract:check`/`typecheck`/`lint` 通过，单测 11 项、`build`/`build:static` 通过；e2e 见下方说明 |
| 容器交付 | 2026-10-28 在本机用指定基础镜像构建并通过真实 HTTP 验收；CI 的 delivery 工作流每次提交从发布包构建镜像并跑离线冒烟 |
| 本机五人技术试点 | 旧库 v13→26 副本迁移与旧程序回退、五身份 140 次 API 与 5 个浏览器上下文 111 次请求、固定容量门槛、合成扫描件 OCR、真实模型固定事实检查（2026-10-29） |

e2e 说明：2026-11-01 在云端 Linux 用系统 Chrome for Testing 153 运行时 28 项通过、1 项（桌面与手机两个项目）失败，失败点是导出下载的建议文件名为 `download` 而非 `*.zip`；同一测试在本次改动之前的代码上同样失败，属于浏览器与运行环境差异，以 GitHub Actions 的 Playwright Chromium 结果为准。

## 4.2 三组对照实测（Qwen3.8-27B）

| 数据集 | A | B | C | 相对 A 的配对差 |
|---|---|---|---|---|
| 开发集（单案 60 题 × 1 轮） | 86.67% | 76.67% | 86.67% | B −10.0 pp / C 0.0 pp |
| 保留集（6 案 24 题 × 3 轮） | 90.28% | 88.89% | 87.50% | B −1.39 pp / C −2.78 pp |

成本：B/C 输入 token 为 A 的 4.9–6.4 倍，耗时中位 1.4–1.7 倍；保留集 6 案例重采样区间跨 0。
样本口径（[分母审计](../benchmarks/lexvault-evaluation-sample-audit-20261029.md)）：开发集 60 题只来自 1 个案件；保留集 24 题/6 案中缺法条仅 1 题、无关 2 题、注入 0 题，C 组在缺法条上的差异来自同一道题的多个 claim，不能当作类别结论。
结论：协作机制未观察到质量收益，成本显著更高；详见[开发集](../history/migration/portfolio-p2-dev-results-20261027.md)与[保留集](../history/migration/portfolio-p2-holdout-results-20261028.md)记录。

Judge 交叉检查（同模型、开发集 60 题 × 3 组）：开发集三组均为 57/60（与规则一致 43–51/60），保留集 A/B 22/24、C 14/24 后中断；空答案被判通过，说明 Judge 只可作观察辅助，主口径仍用确定性 v2（[记录](../history/migration/portfolio-p2-judge-results-20261028.md)）。

## 5. 证据索引

| 主题 | 文件 |
|---|---|
| 计划与阶段状态 | [agent-portfolio-plan-20261027.md](../history/migration/agent-portfolio-plan-20261027.md) |
| P0 范围与身份冻结 | [portfolio-p0-scope-evidence-20261027.md](../history/migration/portfolio-p0-scope-evidence-20261027.md) |
| P1 三组对照驱动与回归 | [portfolio-p1-ablation-driver-20261027.md](../history/migration/portfolio-p1-ablation-driver-20261027.md) |
| P2 预检阻塞记录 | [portfolio-p2-precheck-blocked-20261027.md](../history/migration/portfolio-p2-precheck-blocked-20261027.md) |
| P3 演示状态与异常片段 | [portfolio-p3-demo-status-20261027.md](../history/migration/portfolio-p3-demo-status-20261027.md) |
| P4 新目录重建 | [portfolio-p4-reproducible-delivery-20261027.md](../history/migration/portfolio-p4-reproducible-delivery-20261027.md) |
| 多专家交接实现 | [specialist-collaboration-20261026.md](../history/migration/specialist-collaboration-20261026.md) |
| 答案级评测口径 | [answer-evaluation.md](../runbook/answer-evaluation.md) |
| 评测补齐与双人盲标注交接 | [lexvault-evaluation-completion-20261029.md](../history/migration/lexvault-evaluation-completion-20261029.md) |
| 本机五人技术试点验收 | [local-five-user-acceptance-20261029.md](../history/migration/local-five-user-acceptance-20261029.md) |
| 法条冻结资产恢复 | [collaboration-corpus-recovery-20261028.md](../benchmarks/collaboration-corpus-recovery-20261028.md) |
| 架构与威胁模型 | [system.md](../architecture/system.md)、[threat-model.md](../security/threat-model.md) |

## 6. 明确未覆盖

- 模型范围：真实对照只在单一本机模型（Qwen3.8-27B 4-bit）上完成，没有跨模型对照。
- 跨案件泛化：开发集单案 60 题、保留集 6 案 24 题，样本小，只作探索性描述，不作显著性结论。新的 48 题 / 8 案封存草稿已建好，尚未让模型作答。
- 独立人工评分与 κ：双人盲标注包已备好，**还没有标注人**，因此不声称“独立质量验收完成”。
- 真实模型主演示视频：异常路径片段已完成，主流程演示待录制。
- 法律质量与律所业务验收：本机五人试点是技术验收，不等于律师签收。

## 7. 合规与数据

只使用合成或获准材料；不随包发布模型权重、卷宗、凭证与原始私有报告。真实模型调用须在明确的数据与预算授权下进行。

## 8. 开发方式与许可

本项目由维护者主导设计、取舍与验证，编码过程使用 AI 编程助手（Codex、Claude）辅助；提交作者统一为维护者，改写说明见 [author-rewrite-20261101.md](../history/author-rewrite-20261101.md)。源码按 [MIT 许可证](../../LICENSE) 发布，第三方依赖与数据按各自许可。
