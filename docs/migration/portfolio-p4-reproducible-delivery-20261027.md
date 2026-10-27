# 求职展示版 P4：允许清单源码包在新目录重建、合成验收与全量离线检查

- 日期：2026-10-27（Asia/Shanghai）。
- 对应计划：[agent-portfolio-plan-20261027.md](agent-portfolio-plan-20261027.md) 第 5.2 节；前置：[P0](portfolio-p0-scope-evidence-20261027.md)、[P1](portfolio-p1-ablation-driver-20261027.md)。
- 结论：**源码路径复现成功**（离线依赖安装 + 隔离数据目录 + 离线 HTTP 合成验收 + 包内回归），仓库内全量离线检查为 **1413 passed / 2 既有资产失败 / 1 skip**。**Docker 与前端构建未验证，不声明可用。**

## 1. 打包身份

| 项 | 值 |
|---|---|
| 命令 | `uv run --locked python scripts/package_release.py --output output/portfolio-repro/lexvault-source-20261027c.zip` |
| 归档 | `output/portfolio-repro/lexvault-source-20261027c.zip`（私有产物，`.gitignore` 已排除） |
| 归档 SHA-256 | `bd9006b491fb848fd1c3898b1ff20bcebf4b9663dc0a74fedc066e10516d0652` |
| 成员数 | 448 个源码文件；解压后 449 个文件（含 `RELEASE_MANIFEST.json`） |
| 包内清单 | 逐成员摘要 **448 项全部一致，0 项不匹配** |
| 代码一致性 | 包内 `app/agents.py` = `4dd5451b…aa2c`、`app/collaboration_ablation.py` = `1adc329c…d05a`、`scripts/build_holdout_eval.py` = `e721a67c…fef2`，与提交 `0c82a2b` 逐字节相同；文档树含本轮阶段记录，因此身份以代码摘要与成员清单为准 |
| 后续提交 | 本包对应 `0c82a2b`。其后新增 `app/ablation_analysis.py`、`scripts/analyse_collaboration_ablation.py`、`scripts/demo_identity_report.py` 及其回归（提交 `608df86`、`68139aa`），只增加文件、不改既有路径；需要复核时按第 2 节重新打包即可 |

## 2. 新目录重建

| 步骤 | 命令 | 结果 |
|---|---|---|
| 解压到全新目录 | `unzip -q … -d /tmp/portfolio-repro-c` | 449 个文件 |
| 离线安装 | `uv sync --locked --no-dev --offline` | 通过（命中本机 uv 缓存，未联网，85 个包） |
| 开发依赖 | `uv sync --locked --group dev --offline` | 通过 |
| 语法检查 | `python -m compileall -q app scripts tests` | 通过 |
| 解释器 | uv 依 `requires-python` 选用宿主机 **Python 3.14.6** | 见第 5 节限制 |
| 工具版本 | uv 0.12.0、SQLite 3.54.0 | — |

## 3. 合成样例（实际进程）

隔离数据目录 `/tmp/portfolio-repro-data-c`，token 认证，关闭模型与远端 embedding：

```sh
LAW_REVIEW_DATA_DIR=/tmp/portfolio-repro-data-c \
LAW_REVIEW_LANGGRAPH_CHECKPOINT_DB=/tmp/portfolio-repro-data-c/checkpoints.sqlite \
LAW_REVIEW_AUTH_MODE=token LAW_REVIEW_ALLOWED_HOSTS=127.0.0.1,localhost \
LAW_REVIEW_API_TOKENS_JSON='{"<本次随机 36 字节令牌>": {"name": "管理员", "admin": true}}' \
  uv run --locked uvicorn app.main:app --host 127.0.0.1 --port 18768
```

- `/api/health` → `{"status":"ok"}`。
- `LAW_REVIEW_SMOKE_TOKEN=… python scripts/delivery_smoke.py --base-url http://127.0.0.1:18768 --mode offline`：

```json
{"case_id": 2, "document_id": 6, "cited_page": 2, "model_used": false,
 "export_sha256": "4451366fe730468115b11a65c74f7bcf64588fa53a909ee19d7648828343647c"}
```

脚本实际创建两页合成 TXT、入库、离线问答并定位到**第 2 页**、导出包与清单哈希核对、最后把自建案件移入回收站。`model_used=false` 如实反映本次没有模型。验收后关闭实例并删除一次性数据目录。

## 4. 包内回归

| 范围 | 结果 |
|---|---|
| `test_collaboration_ablation.py`、`test_holdout_eval.py`、`test_recovery_demo_script.py`、`test_review_collaboration.py`、`test_answer_eval_v2.py`、`test_answer_judge.py`、`test_agents.py` | **99 passed**（解压目录内，使用包内代码与冻结数据） |
| 包内保留集复算与冻结 | `scripts/build_holdout_eval.py` 输出与 `manifest.json` 相同的摘要；`--mode prepare` 成功播种 6 案例 24 页、24 题 |

## 5. 仓库内全量离线检查

命令等价 `.github/workflows/test.yml`，隔离数据目录，`--disable-socket --allow-unix-socket`，`--cov=app --cov-branch`：

```sh
LAW_REVIEW_FORGE_PYTHON=<0.8.3 框架环境>/bin/python \
LAW_REVIEW_DATA_DIR=<临时目录> LAW_REVIEW_LANGGRAPH_CHECKPOINT_DB=<临时目录>/checkpoints.sqlite \
LAW_REVIEW_AUTH_MODE=local LAW_REVIEW_LLM_URL=http://127.0.0.1:1/v1 REDIS_URL=redis://127.0.0.1:1/0 \
PYTHONHASHSEED=0 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
uv run --locked pytest tests scripts/test_engineering.py scripts/test_data_snapshot.py scripts/test_snapshot_runtime.py \
  -p pytest_cov -p pytest_socket --disable-socket --allow-unix-socket \
  --ignore=tests/test_chat_client.py --ignore=tests/test_mobile_ui.py \
  --ignore=tests/test_pricing.py --ignore=tests/test_usage_panel.py …
```

| 项 | 结果 |
|---|---|
| 通过 / 失败 / 跳过 | **1413 passed / 2 failed / 1 skipped**，另 278 subtests passed（248.91 s） |
| 覆盖率 | 语句 13,881、缺失 1,631；分支 4,388、部分覆盖 667；**综合 85.77%**（门槛 70%，未调整） |
| 跳过 | `tests/test_sft_pool.py::…` 需要未安装的 `mlx_lm`，不计为通过 |
| 两处失败 | `test_corpus_audit.py::test_real_frozen_corpus_is_classified_cleanly`（`boundary_difference` 期望 1 实得 0）、`test_corpus_canonical.py::test_real_corpus_builds_clean_canonical`（`publication_count` 66 < 71）。与 [G1 记录](collaboration-release-readiness-evidence-20261027.md) 第 3 节同一资产问题：包内不含 `output/score85` 的本机法条快照；断言未放宽 |
| 未执行（环境阻塞） | `test_chat_client.py`、`test_mobile_ui.py`、`test_pricing.py`、`test_usage_panel.py` 共 18 项需要 `node`；本机 `which node` 无结果，单独执行时报 `subprocess.run(["node", …])` 找不到可执行文件。**不计为通过**，也不是代码回归 |
| 框架解释器 | 使用 `output/framework-083-20261027`（`scripts/setup_framework --offline` 新建，forgeharness 0.8.3）。仓库内既有 `.venv-framework-m2` 是 **0.6.0**（`DurableGraph` 导入即失败），直接使用会让约 84 项框架测试报错；本轮不修改该目录，只记录它不能代表当前绑定 |
| 新增对比 | 本次 `agents.py` 因发现顺序缺陷重登记为 `specialist-review-v3`，`test_model_comparison.py` 在框架环境 **14 passed**，覆盖真实侧车与 native 对照 |

## 6. 限制与未覆盖

| 项 | 状态 |
|---|---|
| Docker 构建与 Compose 启动 | **未验证**，不声明可用 |
| 前端构建（`pnpm build:static`）与浏览器流程 | 未验证；本机无 Node，本批未改前端 |
| 模型模式（`--mode model`） | 未验证：本机无 LLM 端点，见 [P2 记录](portfolio-p2-precheck-blocked-20261027.md) |
| Python 版本矩阵 | 全量检查在本机 3.12.13 执行；解压包内的安装与回归在 3.14.6 执行。3.11/3.14 的托管矩阵未跑 |
| Redis / 队列导入 | 未启动 Redis，批量导入路径未验证 |

## 7. 复现结论

在全新目录、仅用允许清单包、离线依赖与隔离数据目录的条件下，当前提交能够安装、启动、完成一次真实 HTTP 合成验收并跑通本轮新增回归；仓库内全量离线检查只余两处既有资产失败与一项可选后端跳过。这满足计划“至少验证一种实际安装与启动路径”的要求；容器路径、前端构建与模型模式仍按各自门禁单独执行，不能被本次结果替代。
