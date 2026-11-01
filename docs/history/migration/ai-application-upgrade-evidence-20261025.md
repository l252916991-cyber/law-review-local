# AI 应用升级：证据口径与验证环境核对（A-E0）

日期：2026-10-25（北京时间）。分支 `codex/ai-app-upgrade`，核实时 `HEAD` = `1af63f5`（相对 `origin/codex/ai-app-upgrade` 领先 4 个提交，工作区干净）。编制依据：[收尾与验收计划书](ai-application-upgrade-continuation-plan-20261024.md) §4 A-E0。

本文只登记本轮核对到的事实、命令与数字，不表示任何功能已通过模型或业务验收。凡无法复现的历史数字，一律标记为"历史陈述，未复核"。

## 1. 核对方法

1. 在仓库根目录执行本文 §3、§5 列出的命令，记录退出码与关键输出。
2. 覆盖率的唯一本轮依据是 `output/baseline-full/coverage.json`（私有运行产物，不入库）；历史百分比只作为对照。
3. 测试数量用统一收集口径比对：在 `81a441d` 的独立 `git worktree` 中收集一次，在当前 `HEAD` 收集一次，两者之差即本批新增用例数。
4. 环境能力只按实测记录；"本 shell 找不到某工具"必须继续核查其它安装位置，不直接当作整机缺失。

## 2. 统计口径更正

| 项 | 历史陈述 | 本轮核对 | 依据 |
| --- | --- | --- | --- |
| 新增测试数 | 实施记录写"新增测试 78 项"，正文十组相加为 100 | **十组所列即 100 个 `def test_` 方法，且全部位于本批新增的 10 个测试文件**；按统一收集口径（含参数化展开）由 `81a441d` 的 1076 项增至 `1af63f5` 的 1180 项，即**新增 104 项收集用例** | `git worktree add /tmp/lv-81a441d 81a441d` 后 `pytest --collect-only -q`；`HEAD` 同命令；`grep -c "    def test_"` 逐文件计数 |
| 覆盖率 | 实施记录写 81%；另一会话称 80% | **本轮实测 85.42%**（`--cov=app --cov-branch`）。差异来自是否安装独立框架解释器、是否装有 Node、以及收集方式，不能互相替代 | `output/baseline-full/coverage.json`、`.github/workflows/test.yml` 同口径参数 |
| 全量测试 | 实施记录写 1055 passed / 85 skipped / 2 deselected | 本轮为 **1177 passed / 2 failed / 1 skipped / 280 subtests passed**。2 项失败均为本地过期语料（§5）；"2 deselected"本轮未复现，记为历史陈述，未复核 | `output/baseline-full/junit.xml`、`pytest.log` |
| `test_mobile_ui.py` 4 项失败 | 实施记录写"本机无 node" | 该结论只对该次 PATH 成立：本机存在真实 node 运行时（§3），补 PATH 后 6 项全部通过 | `tests/test_mobile_ui.py` 实跑 |

实施记录中的"78 项"为笔误性质的合计错误，已在原文档加注更正；正文十组明细本身与代码一致，不做改写。

## 3. 环境事实

| 依赖 | 本轮事实 | 证据 | 影响 |
| --- | --- | --- | --- |
| uv | 0.12.0（`/Users/xiaoy/.local/bin/uv`）；默认缓存目录 `~/.cache/uv` 在本会话文件沙箱下不可写，所有 `uv run --locked` 需 `UV_CACHE_DIR=/tmp/uv-cache` | 失败信息 `Failed to initialize cache ... Operation not permitted (os error 1)` | 仅影响本会话命令写法，不改变锁文件与 CI |
| Python | `.venv` 为 3.12.13，`scripts/doctor.py` 判定 `supported_python: true`；矩阵解释器 `python3.11`、`python3.14` 位于 `~/.local/bin` | `scripts/doctor.py` 输出 | 本地可跑矩阵，本轮未跑（见 §6） |
| Node | PATH 中无 `node` / `npm` / `pnpm`；存在独立 node **v24.22.1**（`~/Library/Application Support/Codex/artifact-sessions/runtime-generations/*/runtime/bin/node`）；DSH 应用二进制在 `ELECTRON_RUN_AS_NODE=1` 下提供 **v24.21.0** | `node --version`；`app/static/*.js` 四个脚本 `node --check` 全部通过；`tests/test_mobile_ui.py` 6 项通过 | 旧前端语法检查可执行；**npm/pnpm 缺失，Next.js 工程无法安装依赖或构建** |
| 独立框架解释器 | `uv run --locked python -m scripts.setup_framework --output /tmp/lexvault-framework-a0` 安装 0.8.3 wheel 与带 hash 依赖，`pip check` 通过 | 脚本 stdout：`{"framework_python": "/tmp/lexvault-framework-a0/bin/python"}` | 设置 `LAW_REVIEW_FORGE_PYTHON` 后 61 项原跳过测试转为通过 |
| `mlx_lm` | 未安装 | `tests/test_sft_pool.py:128` 跳过原因 | 1 项跳过，属可解释环境缺口 |
| 模型端点 | `127.0.0.1:8000`（omlx）在线，列出 `Qwen3-Embedding-4B-4bit-DWQ`、`Qwen3.6-35B-A3B-OptiQ-4bit`、`Qwythos-9B-v2-OptiQ-4bit`、`bge-reranker-v2-m3-mlx`；**配置默认 `Qwythos-9B-v2-4bit-mlx` 不在列表中** | `curl /v1/models` | 可做真实模型验证，但必须显式设置 `LAW_REVIEW_LLM_MODEL`；本轮**未调用模型** |
| 文档工具 | `pdftotext`、`pdfinfo`、`pdftoppm`、`tesseract` 均缺失，`ocr_ready: false` | `scripts/doctor.py` | 只能稳定导入 DOCX；PDF/Tesseract 路径在本机不可用（影响演示取材，见 §6） |
| 浏览器 | 未安装 Playwright；本机有 Microsoft Edge（Chromium 内核）与 Electron 可载入真实页面 | `ls /Applications`、`ms-playwright` 缓存不存在 | 真实页面流程验证方案可行，本轮未执行 |

## 4. 本轮实测门禁（工程侧）

命令与参数沿用 `.github/workflows/test.yml`，仅多提供一个本地 `UV_CACHE_DIR`，并显式提供 Node 与框架解释器：

```sh
export UV_CACHE_DIR=/tmp/uv-cache
export PATH="<node-runtime-bin>:$PATH"                     # node v24.22.1
export LAW_REVIEW_FORGE_PYTHON=/tmp/lexvault-framework-a0/bin/python
export LAW_REVIEW_AUTH_MODE=local LAW_REVIEW_LLM_URL=http://127.0.0.1:1/v1
export REDIS_URL=redis://127.0.0.1:1/0 PYTHONHASHSEED=0 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
export LAW_REVIEW_DATA_DIR=<隔离目录> LAW_REVIEW_LANGGRAPH_CHECKPOINT_DB=<隔离目录>/checkpoints.sqlite
uv run --locked pytest tests scripts/test_engineering.py scripts/test_data_snapshot.py scripts/test_snapshot_runtime.py \
  -p pytest_cov -p pytest_socket --disable-socket --allow-unix-socket \
  --junitxml=output/baseline-full/junit.xml --cov=app --cov-branch --cov-report=term-missing \
  --cov-report=json:output/baseline-full/coverage.json
```

| 检查 | 结果 | 结论 |
| --- | --- | --- |
| 完整离线测试 | 1177 passed / 2 failed / 1 skipped / 280 subtests，215.94s | 2 项失败为本地过期语料（§5），1 项跳过为 `mlx_lm` |
| 覆盖率门槛（70%） | 85.42% | 达到门槛 |
| `ruff check app tests scripts` | All checks passed | 通过 |
| `mypy` | Success: no issues found in 44 source files | 通过 |
| `python -m compileall -q app tests scripts` | 无输出，退出码 0 | 通过 |
| `node --check app/static/*.js` | 4/4 通过 | 通过 |
| `scripts/doctor.py` | 依赖齐全，`ocr_ready: false` | 通过（OCR 能力缺失已记录） |
| requirements 导出一致性 | `uv export --locked` 两次 diff 均无差异 | 通过 |
| 发布允许清单 `scripts/package_release.py --dry-run` | 未执行 | 本轮不涉及发布内容；留到 A11.4 发布包验证 |

## 5. 两项失败的归因（不修改断言）

`tests/test_corpus_audit.py::test_real_frozen_corpus_is_classified_cleanly` 与 `tests/test_corpus_canonical.py::test_real_corpus_builds_clean_canonical` 读取 `output/score85/legal_corpus*`。该目录被 `.gitignore` 忽略（`output/`），本机存在 `legal_corpus`、`legal_corpus_canonical_v2`、`legal_corpus_supplement_npc_v10` 三个**未提交、未更新**的语料目录，因此断言（`boundary_difference == 1`、`publication_count >= 71`）与实际不符。

- 两项测试在语料目录不足两个时会 `skip`，说明它们断言的是外部冻结语料，而非仓库内代码。
- 处置：本轮不修改断言、不删除测试；语料重建属独立任务（`510699a` 记录的法条库重建），未纳入本批。
- 结论：**可解释的本地环境差异，不是本批代码回归**；CI 中该目录不存在，两项会跳过。

## 6. 验收分列

| 类别 | 本轮状态 | 说明 |
| --- | --- | --- |
| 工程验收 | **部分完成**：离线测试、静态检查、导出一致性、doctor 通过；2 项依赖本地过期语料 | 未跑 Python 3.11/3.14 矩阵（解释器可用）；未跑发布包 dry-run |
| 模型验收 | **未执行** | 端点在线但默认模型名不匹配；本轮无模型调用，不使用单元测试代替真实模型结果 |
| 业务/人工验收 | **未执行** | 无真实案件材料、无人工标注或专业复核 |
| 浏览器/页面验收 | **未执行** | 无 Playwright；真实页面方案待 A06.1 落实 |

## 7. 结论与后续阻塞

1. 本机具备继续实施 A02.1、A01.1、A04.1、A09.1、A02.2、A06.1 的工程条件（Python 侧齐全，Node 可做旧前端语法检查，真实页面验证需另建方案）。
2. **A11（Next.js 重写）当前被阻塞**：无 `npm`/`pnpm`，无法安装 Next.js/React/TypeScript 依赖、无法生成锁文件与生产构建。解除条件是装好包管理器（可用已存在的 node v24.22.1 引导），本轮不擅自安装、不提交未经锁定的工程骨架。
3. 真实模型验证（A05.1）可直接用本机 omlx 端点，但需显式指定实际存在的模型名，并先取得用户对调用范围的确认。
4. 历史"78 项 / 81% / 1055 passed"统一按 §2 更正；后续记录以本文件的命令与产物路径为口径基准。
