# 求职展示版 P3：演示状态——异常片段已完成，真实模型主演示未开始

- 日期：2026-10-27（Asia/Shanghai）。
- 对应计划：[agent-portfolio-plan-20261027.md](agent-portfolio-plan-20261027.md) 第 5.1 节；前置：[P0](portfolio-p0-scope-evidence-20261027.md)、[P1](portfolio-p1-ablation-driver-20261027.md)、[P2](portfolio-p2-precheck-blocked-20261027.md)。
- 结论：**P3 部分完成**。恢复/拒用异常片段已在当前代码上按独立进程复现并留下可核对证据；**真实问答主演示（材料导入 → 提问与来源定位 → 专家发现 → 候选分歧 → 人工复核 → 指定版本导出）未录制**，因为登记的模型端点不可用，用固定答案替身录制的旧成片不能顶替。

## 1. 已有成片的边界

`docs/runbook/demo-recording.md` 记录的 180 秒成片使用模型**固定答案测试替身**，文档本身已标注。本计划的 P3 要求“真实模型响应”，因此旧成片只能用于展示界面与流程，不可作为本轮演示交付，也不可被描述为模型效果。

## 2. 已完成的异常片段（当前代码，关闭模型）

新增 `scripts/demo_recovery_check.py`，三个阶段各为独立进程，证据写入 `output/portfolio-demo/anomaly-20261027/`：

| 阶段 | 命令 | 结果 | 证据文件 SHA-256 |
|---|---|---|---|
| 中断 | `--phase interrupt` | 合成中断发生在审校完成后；`run_id=1`、`steps=9`、运行状态 `failed`、`resume_count=0` | `01-interrupt.json` = `f582838c58dffb58d5d7654f7e90acaf10d37015abdde799aae7c1303b5426e6` |
| 恢复 | `--phase resume`（新进程） | 审校结果摘要一致（`collaboration_reused=true`）；步骤数 9 → 9（未重复专家节点）；`resume_count=1`；状态 `completed` | `02-resume.json` = `1acf12759bd045a044592bc6787b8c97314ee75eb6e4d5339008c03c51e33585` |
| 篡改来源 | `--phase tamper` | 修改被引用的第 `[2,3]` 页后，恢复被拒绝：`运行依据的资料页内容已变化，拒绝恢复旧分析`；运行仍 `failed`、`resume_count=0`（未消耗重试） | `03-tamper.json` = `1b23d0474da15ce25a6995d0823e8e84e673da8ccc4d23bceb524e7bbdc44c3c` |

- 三份证据均含 `model_disabled=true` 与“不是模型效果证据”的说明，避免与真实模型演示混淆。
- 审校摘要 `13bba1ad02e15dff37deafbbce1d64f2387726c087a15a4798b928ac7798fc20`、状态 `ready_for_lawyer_review`，来源为本轮检索的 6 页合成材料。
- 复现入口已写入[录屏手册](../../runbook/demo-recording.md)的“恢复校验片段（关闭模型，可离线复现）”一节。
- 回归：`tests/test_recovery_demo_script.py` 4 项通过（复用、拒用、缺参数、目录不可复用）。

## 2.1 录制前身份核对（已可执行）

`scripts/demo_identity_report.py` 在不启动模型与服务的前提下记录 P3 清单要求的身份，输出排他创建、不含案件正文：

```sh
uv run --locked python scripts/demo_identity_report.py --output output/portfolio-demo/identity-YYYYMMDD.json
```

2026-10-27 生成两份：首份 `identity-20261027.json`（SHA-256 `1e2705ae…3958`，当时前端未重建）；前端重建后重生成 `identity-20261027b.json`（SHA-256 `33ab8676cd86d52e4d3ba863c6bf1e5ad9f533245eb714b57eee70ca552fab29`），以下为后者实测：

| 项 | 值 |
|---|---|
| 提交 / 分支 | `03dcc5c477b0ed0ee75d73018b6db03c1d1c5ed7` / `codex/agent-portfolio-plan` |
| 工作区 | `dirty=true`，仅 `tmp_eq/`（与本任务无关的本地草稿目录） |
| 加载的后端入口 | `app/main.py` 的实际解析路径，随报告记录 |
| 后端文件摘要 | 7 个文件逐个 SHA-256（`main/services/agents/review_collaboration/collaboration_ablation/ablation_analysis/answer_eval`） |
| 提示与策略版本 | `chat-system-v4-multiturn-untrusted-history`、`candidate-answerability-v11-detail`、`answer-judge-v2-frozen-sources` |
| 模型探针 | `available=false`，端点 `http://127.0.0.1:8000/v1`，模型 `Qwen3.6-35B-A3B-OptiQ-4bit` |
| 前端门禁 | `contract:check`/`typecheck`/`lint` 通过、单测 11、e2e 22、静态构建通过（见 [P4 5.1](portfolio-p4-reproducible-delivery-20261027.md)） |
| 前端构建绑定 | 存在：21 个资源，`build-info.json` 摘要 `20c9c0ede5e5f9a65879898d50e92612846f7771b5505300c0173da96be0c52b`（`build:static` 重建后） |
| 数据集 | 开发集 60 题 / 14 页（`b912f4ca…`）；保留集 24 题 / 24 页（`66265fdb…`） |

该报告即录制前的“身份基线”：正式录制时需在同一提交上重跑并保存新报告，与成片、服务日志和模型用量相互核对。模型探针为 `false` 时不得录制主演示。

## 2.2 演示链路的可运行部分（无需模型）

前端构建与候选包签发链路已在本机跑通，可直接作为演示中“界面/权限/冻结/导出”环节的证据：

| 项 | 结果 |
|---|---|
| 前端构建 | `pnpm --dir frontend build:static` 通过；21 个资源，`build-info.json` 摘要 `20c9c0ed…c52b` |
| 浏览器流程 | Playwright e2e **22 passed**（desktop + mobile） |
| 候选包 | `lexvault-frontend-20261027.zip`（482 成员，0 摘要不符，含前端构建） |
| 签发链路 | `release_smoke.py` **passed**：静态首页带 CSP、管理员登录、建案、上传、报告冻结版本、复核权限、下载绑定冻结正文 |

仍缺的是“真实模型回答、专家交接后的候选分歧、模型不可用时的降级说明”这三段画面；它们必须在端点可用后录制，不能用固定答案替身顶替。

## 3. 未录制的主演示与录制条件

| 演示项 | 现状 | 前提 |
|---|---|---|
| 材料导入 → 提问 → 来源定位 | 未录制 | 模型端点可用；前端构建摘要登记 |
| 专家结构化发现、候选矛盾/待补事项 | 未录制 | 同上；需展示节点输入输出而非隐藏思维链 |
| 人工查看与报告复核、指定版本导出 | 未录制 | 同上 |
| 模型不可用时的降级 | 未录制 | 必须标明降级，不得用固定答案替身冒充 |

录制前必须同时记录并能相互核对的身份：

1. 后端进程实际加载的代码提交（`git rev-parse HEAD`）与前端 `frontend/out/build-info.json` 构建摘要。
2. 模型实际 ID、端点与 `/v1/models` 返回，以及本次生成的 `usage`（未知则留空）。
3. 合成来源摘要（导入文件的 SHA-256）与运行标识（`run_id`、`request_id`）。
4. 原始录制、服务日志与成片清单；必要剪辑/加速须注明，实际耗时以测量日志为准。

## 4. 沿用与不复用

- **沿用**：界面与流程设计、录制脚本 `frontend/scripts/record-demo.mjs`、180 秒分镜结构、导出与冻结正文的检查点。
- **不复用**：旧成片中的模型响应部分（固定答案替身）、旧模型用量与「已验真实模型」的表述。
- 不强行终止用户现有服务来录制异常片段；本次异常片段使用一次性数据目录，结束后未留下运行进程。

## 5. 结论

异常路径的可核验证据已具备，主演示因模型资源阻塞而未开始。P3 的完成标准“实际进程加载冻结代码和前端构建；演示报告、视频和原始日志能相互核对”尚不满足，需在模型端点恢复后按第 3 节重录。
