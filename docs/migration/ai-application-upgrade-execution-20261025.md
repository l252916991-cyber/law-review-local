# AI 应用升级实施记录（A-E0 至 A06.1）

日期：2026-10-25（北京时间）。分支 `codex/ai-app-upgrade`。开始基线 `1af63f5`；计划见[收尾与验收计划书](ai-application-upgrade-continuation-plan-20261024.md)，证据口径见[证据与环境核对](ai-application-upgrade-evidence-20261025.md)。

本批按计划顺序实施 A-E0、A02.1、A01.1、A04.1、A09.1、A02.2、A06.1，每包单独提交。A11（Next.js 前端）**未实施**：本机没有 `npm`/`pnpm`，无法安装依赖或生成锁文件（见证据文档 §7）。A05.1、A07.1、A08.1、A13.1 不在本批范围。

## 1. 提交清单

| 提交 | 包 | 结果 |
| --- | --- | --- |
| `28bd637` | A-E0 | 统计口径更正（78→100/104、81%→85.42%）、环境事实、工程/模型/业务分列 |
| `79f7b2a` | A02.1 | 用量与延迟面板、`*_known_calls` 覆盖计数、真实浏览器页面验证工具 |
| `185cb91` | A01.1 | 流式事件契约（citations/done）、`chat_requests` 幂等（schema v20）、协作取消、客户端同键重试 |
| `bade056` | A04.1 + A09.1 | 审校节点复用只读工具循环、一次性模型回退、轨迹与归属 |
| `d2293d8` | A02.2 | 定价快照（schema v21）、`turn_key`、问答级费用估算与面板 |
| `eff2fae` | A06.1 | 轨迹用量展示、切案清理、回退端点探测修复、workbench 页面流程 |
| `f71cf99` | 修复 | v21 迁移可重复执行（降级重跑不再报重复列） |

A04.1 与 A09.1 合并为一个提交：两包改的是审校节点同一条回答生成路径，拆开会得到不能独立验证的中间状态。

## 2. 各包实际改动

### A02.1 用量与延迟面板

- `usage_summary` 与 `by_kind`/`by_model` 增加 `*_known_calls`、失败数、分组 TTFT 百分位；合计仍只累加已知样本。
- 阅卷任务页新增面板：调用数、失败数、输入/输出 tokens、首 token P50/P95、单次调用延迟 P50/P95、按类型与模型分组；空样本、usage 缺失、部分缺失三种状态分别显示。
- 切案先清空数值；请求失败显示"指标不可用"，不留上一个案件的数据。
- 面板注明只统计模型调用，整次问答延迟与浏览器首字时间尚未采集；本包不展示金额。

### A01.1 流式协议、断连与重复提交

- 事件顺序：`status(retrieval/generating/validating/replayed)` → `token(provisional)` → `citations` → `final`（兼容帧）→ `done`；失败为 `retract` → `done(failed)`。
- `done` 携带 `conversation_id`、`message_id`、`answer_status`、`citation_check`、`llm_used`、`ttft_ms`、`usage`、`request_id`；聊天不创建 `agent_runs`，没有 `run_id`。
- schema v20 `chat_requests`（主键 `(case_id, request_id)`）：已提交的同键重试回放，进行中的同键请求返回 409，崩溃遗留的 `in_progress` 15 分钟后可接管，7 天保留期按小时节流清理。
- `record_turn_with_id` 让幂等记录与消息、审计在同一事务提交；断连只可能"整轮写入"或"完全没有写入"。取消标志在模型调用前与提交前各查一次，已发生的模型调用仍写入 `llm_calls`。
- 客户端每次提问生成 `request_id`，网络级中断自动用同一键重试一次，HTTP 错误不重试；撤回的草稿不打首字耗时。

### A04.1 Agent 工具循环

- 原生 DAG 审校节点在 `LAW_REVIEW_TOOL_LOOP=on` 时复用 `app/tool_loop.py`；历史/记忆/对象说明作为不可信数据进入工具循环（此前非流式工具路径会丢掉这些上下文）。
- 端点拒绝 tools 时显式回退并记 `endpoint_rejected_tools`；宿主管控作用域内拒绝运行，不绕过准入账本。
- 轨迹 `tools={enabled,reason,rounds,tool_calls,trace,model,budget}` 进入 critic 输出、`agent_steps` 与最终结果；工具轮次按次记入 `llm_calls`，Agent 路径现在带 `run_id`。
- LangGraph 路径按运行时策略关闭工具循环（`tools.reason=runtime_policy`），因为它没有独立的工具预算归属。

### A09.1 Agent 一次性模型回退

- 仅当主答案未过引用校验时回退一次；回退答案用同一案件范围、同一校验；传输失败仍走规则汇总与诊断。
- 主调用记 `critic`、回退记 `critic_fallback`，同一 `run_id`；`model_route{fallback,reason,model}` 贯穿轨迹、结果与页面徽标。
- 半配置或未获准的远端回退端点失败关闭并记 `fallback_misconfigured`。
- LangGraph 审校节点改为传入 `case_id`/`run_id`，用量与回退调用可归属。

### A02.2 问答级估算费用

- `app/pricing.py`：版本、币种、单位、来源与每模型单价；解析失败失败关闭；内部 8 位小数、展示 4 位（ROUND_HALF_UP）。
- `llm_calls.pricing_json` 在写入时复制价目快照，改价不重算历史；`turn_key` 归组一次提问（聊天 `request:<id>`，Agent `run:<id>`）。
- `cost` 与 `turns`：只有全部调用可计价才有 `total`，否则给 `partial_total` 与覆盖计数；缺 usage 与未配置单价分开统计；多币种不求和；未配置显示"未配置成本"，不写 0。
- 面板新增"问答级估算费用"，文案标明是估算费用而非账单。

### A06.1 页面与链路验收

- 运行轨迹接口返回该次运行的用量，轨迹面板与详情弹窗展示；critic 步骤显示模型与回退说明。
- 切案时清空上一次结果、轨迹、详情与对比面板。
- 修复回退端点探测缺陷（见 §4）。
- 四条流程脚本：`metrics`、`chat`、`workbench`（浏览器）与 `agent`（API 级）。

## 3. 验证结果

工程侧（本机 macOS，Python 3.12.13，Node v24.22.1，独立框架解释器 `/tmp/lexvault-framework-a0`）：

| 检查 | 结果 |
| --- | --- |
| 完整离线套件 | 见 §3.1（最新一次运行） |
| `ruff check app tests scripts` | All checks passed |
| `mypy` | Success: no issues found in 47 source files |
| `node --check app/static/*.js` | 4/4 通过 |
| requirements 导出一致性 | 两次 `uv export --locked` diff 无差异 |
| 页面流程 `metrics` | 6/6 步通过（正常、空案件、部分缺失、请求失败、快速切案、恢复） |
| 页面流程 `chat` | 4/4 步通过（引用回原文、撤回、同键回放、消息落库） |
| 页面流程 `workbench` | 4/4 步通过（运行、轨迹详情、回退徽标、切案清理） |
| API 级 `agent` | 3/3 步通过（工具循环、运行轨迹、用量记账） |

### 3.1 完整离线套件

最近一次完整运行（`output/final/`，源码 `f71cf99`）：**1254 passed / 2 failed / 1 skipped / 285 subtests**，覆盖率 **86%**（分支 78%），以 `output/final/coverage.json` 为准。

- 2 项失败是 `test_corpus_audit.py` 与 `test_corpus_canonical.py`，读取被 `.gitignore` 的本地过期 `output/score85/legal_corpus*`；CI 中该目录不存在时会跳过。未修改断言、未删除测试。
- 1 项跳过是 `test_sft_pool.py`（`mlx_lm` 未安装）。
- 覆盖率门槛 70% 保持通过。
- Python 矩阵（自带解释器，独立数据目录）：3.11.15 与 3.14.6 均为 **1254 passed / 2 failed（同一对语料用例）/ 1 skipped**，与 3.12.13 一致。

### 3.2 页面流程证据

`output/browser-check-final-*/`（不入库）含 `browser-check.json`（步骤、失败项、浏览器路径、源码 HEAD）、每步整页与元素截图、`server.log`。三种浏览器流程各自由真实 FastAPI 进程 + 真实 Edge（Chromium 内核）驱动；模型是 `scripts/stub_model.py` 的**测试替身**，报告 `model` 字段标为 `stub`。

### 3.3 测试数量变化

本批新增测试文件：`test_usage_panel.py`（7）、`test_chat_client.py`（6）、`test_agent_tool_loop.py`（16）、`test_agent_fallback.py`（13）；`test_chat_stream.py` 扩到 22 项；`test_llm_usage.py` 增至 12 项。统一收集口径：基线 `1af63f5` 为 1180 项，本批结束为 **1257 项**，新增 77 项收集用例。

## 4. 过程中发现并修复的真实缺陷

1. **回退模型被"判为不可用"**：`local_llm_available` 只探测主端点模型列表，回退模型即便在其自有端点存在也会返回不可用，回退实际从未生效（浏览器流程首次暴露 `fallback_failed`）。改为按被调用端点探测，并加回归测试。
2. **跨案件的用量/费用串数据**：切案只清评测结果，运行结果与轨迹仍显示上个案件内容；现在一并清空。
3. **工具循环丢失上下文**：非流式问答的工具路径此前不传历史/记忆/对象说明，追问时指代丢失；已补齐，并将这些内容标注为不可信数据。
4. **工具调用未归属运行**：Agent 路径的工具轮次 `run_id` 为空，费用无法归到问答；已传入并可在轨迹中看到。
5. **切案竞态**：用量面板此前在请求失败时保留上个案件数据；现在先清空再加载，失败显示"指标不可用"。
6. **迁移不可重复执行**：v21 用 `ALTER TABLE ADD COLUMN`，在"降级到 v16 再升级"或恢复快照时因列已存在而失败（`test_schema_16_migration_preserves_existing_receipts` 暴露）。改为先查 `PRAGMA table_info` 再补列。

## 5. 未完成与边界

- **A11 前端重写未实施**：缺 `npm`/`pnpm`，无法安装 Next.js 依赖或生成锁文件。未提交任何未经锁定的工程骨架。
- **A05.1 真实模型验证未执行**：本机 omlx 端点在线，但默认模型名不在其列表，需显式配置并取得调用范围确认；本批所有模型调用均为测试替身。
- **A07.1/A08.1/A10/A12/A13.1 不在本批范围**；A00 继续暂缓。
- 页面验证只在 Edge（Chromium 内核）与本机 macOS 上执行；未在 CI 跑浏览器，CI 仍只保证编译与逻辑层测试。
- `test_corpus_*` 两项依赖本地未提交语料；语料重建属独立任务。
- 真实案件材料、真实模型响应、数据库与截图均未进入 Git。

## 6. 下一步

1. 具备 Node 包管理器后执行 A11.1–A11.4：工程与 API 契约、页面迁移、指标/工作台迁移、E2E 与入口切换。
2. 模型环境确认后执行 A05.1：真实客户端与双模型实测，与测试替身结果分开报告。
3. 按需推进 A07.1/A08.1 答案级质量闭环；A13.1 演示依赖 A11.4 与 A05.1。
