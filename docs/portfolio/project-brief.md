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
- **信息隔离墙**：设墙案件只有墙内名单可见，管理员也不例外；应用层统一检查，PostgreSQL 上再由 35 张表的强制行级安全兜底（[手册](../runbook/case-walls.md)）。
- **利益冲突检查**：登记当事人即检索全所案件，命中后合伙人放行前不能开展工作，放行须由检查发起人以外的人决定（[手册](../runbook/conflict-check.md)）。

## 3. 技术栈

| 层 | 选型 |
|---|---|
| 服务 | FastAPI + Uvicorn，SQLite（WAL，默认）或 PostgreSQL 16 + pgvector，单实例本地/受控内网 |
| 编排 | 原生 DAG + LangGraph（SQLite checkpoint），共享 `SubAgentSpec` |
| 检索 | 关键词 BM25 类打分 + 本地 embedding + RRF 融合，结果带 `page_text_sha256` |
| 生成 | OpenAI 兼容本地端点（`/v1/chat/completions`），仅回环端点，禁重定向 |
| 前端 | Next.js 静态构建，由 FastAPI 托管，Playwright 端到端；核心实体类型由后端 OpenAPI 生成 |
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

## 4.1 质量基线（2026-11-08，提交 `38d1a04`）

| 检查 | 结果 |
|---|---|
| 全量离线测试（隔离数据目录、禁网、覆盖率，显式指定冻结法条资产） | Python 3.11.18：1802 passed / 0 failed / 24 skipped，综合覆盖率 86.94%（分支 79.10%）；Python 3.14.6：1802 passed / 0 failed / 24 skipped，86.93%（分支 79.10%）；跳过项为私有 LawBench 数据 7 项、Apple 专用 `mlx_lm` 1 项，以及只在 PostgreSQL 下运行的 16 项；独立 3.12 框架解释器实际进程测试已执行；门槛 70% |
| 同一套测试跑在 PostgreSQL 上 | PostgreSQL 16.15 + pgvector 0.6.0，Python 3.11.18：1817 passed / 0 failed / 9 skipped，覆盖率 88.04%（分支 80.41%）；每个测试一个新库，库归非超级用户角色所有，行级安全对全部用例生效；322 个测试在测试体内实际连到 PostgreSQL；从冻结的 v27 结构只执行迁移文件升级到 v28，与新建库逐项一致；测 SQLite 文件本身的用例标 `sqlite_only` 并写明原因 |
| 信息隔离墙 | 以墙外管理员、仍被分配到该案的墙外律师遍历全部 GET 接口，墙内标记 0 次出现；PostgreSQL 上关掉应用层检查重做仍为 0，SQLite 对照为 10 个接口泄露；35 张表强制行级安全，生成器拒绝未归类的案件关联表 |
| 答案质量门禁 | 60 题冻结评测集，规则模式 46/60 通过，PostgreSQL 上逐题通过情况与 SQLite 相同；CI 每次推送运行，退化即阻断。模型模式：本机 Qwen3.8-27B 4-bit 首轮 51/60，按失败题修复后第二轮 57/60；剩余 3 道失败复核为评分词表未覆盖的正确拒答，评分升为 v3 后用保存的回答重新评分为 60/60 并登记为基线（v3 新增措辞取自这两轮，属样本内；首轮按 v3 为 53/60，其中 1 题是勉强通过）（[记录](../history/eval-gate-model-baseline-20261102.md)） |
| 静态检查 | ruff（E9/F/I/UP/B）、mypy（`scripts/` 严格 + `app/` 宽松，22 个存量模块列入只减不增名单）通过 |
| 前端 | `contract:check`/`typecheck`/`lint` 通过，单测 13 项、`build`/`build:static` 通过；e2e 见下方说明 |
| 接口类型 | 11 个核心读接口声明 Pydantic 响应模型，改动前后在演示库上的响应逐字段相同；前端核心组件改用生成的 TypeScript 类型；材料接口不返回服务器存储路径，由遍历全部 GET 接口与导出包的测试守住 |
| 重点模块 | 法条核验 `statutory_retrieval.py` 行与分支覆盖率 100%；会话接口 `routes/chat.py` 85% |
| 容器交付 | 2026-10-28 在本机用指定基础镜像构建并通过真实 HTTP 验收；CI 的 delivery 工作流每次提交从发布包构建镜像并跑离线冒烟 |
| 本机五人技术试点 | 旧库 v13→26 副本迁移与旧程序回退、五身份 140 次 API 与 5 个浏览器上下文 111 次请求、固定容量门槛、合成扫描件 OCR、真实模型固定事实检查（2026-10-29） |

e2e 说明：GitHub Actions（Playwright Chromium）上全部通过。云端 Linux 用系统 Chrome for Testing 153 运行时，导出下载的建议文件名是 `download` 而非 `*.zip`，这一项在改动前后都失败，属于浏览器环境差异，其余 28 项通过。

曾经的偶发失败：`tests/test_eval_transport_faults.py` 的任务截止从发出开始信号起计时，机器稍慢时模型调用还没开始就到点，约三次失败一次；2026-11-02 改为从模型调用卡住时起计时（截止时长与断言不变），见[质量手册](../runbook/quality.md)。

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
| 答案质量门禁 | [eval-gate.md](../runbook/eval-gate.md) |
| PostgreSQL 双后端与向量检索基准 | [postgresql.md](../runbook/postgresql.md)、[postgres-vector-search-20261102.md](../benchmarks/postgres-vector-search-20261102.md) |
| 信息隔离墙与行级安全开销 | [case-walls.md](../runbook/case-walls.md)、[postgres-rls-overhead-20261102.md](../benchmarks/postgres-rls-overhead-20261102.md) |

## 6. 明确未覆盖

- 模型范围：三组对照实验只在单一本机模型（Qwen3.8-27B 4-bit）上完成；答案质量门禁另在 Qwen3.6-35B-A3B 和 Qwythos-9B 上做过跨模型对照（[记录](../history/eval-gate-cross-model-20261107.md)）。
- 跨案件泛化：开发集单案 60 题、保留集 6 案 24 题，样本小，只作探索性描述，不作显著性结论。新的 48 题 / 8 案封存草稿已建好，尚未让模型作答。
- 独立人工评分与 κ：双人盲标注包已备好，2026-11-08 又用 27B 的真实模型回答生成了一份律师盲审包，**还没有标注人**，因此不声称“独立质量验收完成”。编码助手对 9 次模型运行做过人工复核（失败题全读、通过题抽查）（[可信度说明](../benchmarks/eval-credibility-20261108.md)），只用来衡量评分器，不替代律师。
- 真实模型主演示视频：异常路径片段已完成，主流程演示待录制。
- 法律质量与律所业务验收：本机五人试点是技术验收，不等于律师签收。

## 7. 合规与数据

只使用合成或获准材料；不随包发布模型权重、卷宗、凭证与原始私有报告。真实模型调用须在明确的数据与预算授权下进行。

## 8. 开发方式与许可

本项目由维护者主导设计、取舍与验证，编码过程使用 AI 编程助手（Codex、Claude）辅助；提交作者统一为维护者，改写说明见 [author-rewrite-20261101.md](../history/author-rewrite-20261101.md)。源码按 [MIT 许可证](../../LICENSE) 发布，第三方依赖与数据按各自许可。
