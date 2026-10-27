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

## 4.1 质量基线（截至 2026-10-27）

| 检查 | 结果 |
|---|---|
| 全量离线测试（隔离数据目录、禁网、覆盖率） | 1476 passed / 2 failed（既有法条语料资产）/ 1 skipped（未装 `mlx_lm`）；综合覆盖率 86.38%，门槛 70%（提交 `e31593e`） |
| 允许清单包内回归 | 99 passed（三组对照、六案例保留集、恢复演示、答案评分、Judge、专家交接） |
| 解压包离线 HTTP 合成验收 | 通过：两页合成材料、问答定位第 2 页、导出包与清单哈希核对；含前端包另通过签发链路（冻结版本 + 权限 + 下载绑定） |
| 前端 | `contract:check`/`typecheck`/`lint` 通过，单测 11、e2e 22、静态构建通过；含前端的候选包签发链路 passed |
| 未执行 | Docker、模型模式、Redis 队列导入 |

## 5. 证据索引

| 主题 | 文件 |
|---|---|
| 计划与阶段状态 | [agent-portfolio-plan-20261027.md](../migration/agent-portfolio-plan-20261027.md) |
| P0 范围与身份冻结 | [portfolio-p0-scope-evidence-20261027.md](../migration/portfolio-p0-scope-evidence-20261027.md) |
| P1 三组对照驱动与回归 | [portfolio-p1-ablation-driver-20261027.md](../migration/portfolio-p1-ablation-driver-20261027.md) |
| P2 预检阻塞记录 | [portfolio-p2-precheck-blocked-20261027.md](../migration/portfolio-p2-precheck-blocked-20261027.md) |
| P3 演示状态与异常片段 | [portfolio-p3-demo-status-20261027.md](../migration/portfolio-p3-demo-status-20261027.md) |
| P4 新目录重建 | [portfolio-p4-reproducible-delivery-20261027.md](../migration/portfolio-p4-reproducible-delivery-20261027.md) |
| 多专家交接实现 | [specialist-collaboration-20261026.md](../migration/specialist-collaboration-20261026.md) |
| 答案级评测口径 | [answer-evaluation.md](../runbook/answer-evaluation.md) |
| 架构与威胁模型 | [system.md](../architecture/system.md)、[threat-model.md](../security/threat-model.md) |

## 6. 明确未覆盖

- 真实模型的质量、token 与延迟对照：**未测**（本机无模型端点，见 P2 记录）。
- 跨案件泛化：60 题同属一个虚构案件；保留集 6 案例 24 题已冻结在 `benchmarks/answer_eval/holdout-v1/`，尚未运行。
- 独立人工评分与 κ：**未做**，因此不声称“独立质量验收完成”。
- Docker 路径、前端构建与浏览器流程、Redis 队列导入：本轮未验证。
- 项目级许可证与对外公开权利：未确定，材料默认私下审阅。

## 7. 合规与数据

只使用合成或获准材料；不随包发布模型权重、卷宗、凭证与原始私有报告。真实模型调用须在明确的数据与预算授权下进行。
