# 多人协作首版收尾：G1 工程检查与发布候选证据

- 日期：2026-10-27（Asia/Shanghai）。
- 对应计划：[collaboration-release-readiness-plan-20261027.md](collaboration-release-readiness-plan-20261027.md)。
- 受检代码：`19bdb68`（分支 `codex/collaboration-first-release`），工作区干净；本文件为其后的文档提交，不改变受检代码。
- 结论范围：R1/R2/R3 三组修复的回归、Python/前端工程门禁、含前端构建的私有发布候选与隔离实例验收。**M1（真实旧库迁移）与 U1（律所五身份人工验收）未执行**，原因见第 6 节。
- **放行结论：阶段 B（G1）不通过。** 本次已执行的 Python 3.12 离线测试中仅剩两项法条语料资产失败（第 3 节）；整个 G1 还缺 Python 3.11/3.14 矩阵、gitleaks 和容器交付检查。语料复验通过不自动代表 G1 全部放行，剩余项按[续行计划](collaboration-g1-to-acceptance-plan-20261027.md)执行或明确交付范围裁决。

## 1. 环境与工具

| 项 | 值 |
|---|---|
| 宿主 | macOS（arm64），本机开发机，非目标部署硬件 |
| Python | 3.12.13（`uv run --locked`） |
| uv / SQLite | uv 0.12.0 / SQLite 3.54.0 |
| Node / pnpm | Node v24.22.1（项目固定）/ pnpm 11.25.0（`install` 输出；DSH 运行时 11.28.5） |
| 独立框架环境 | `uv run --locked python -m scripts.setup_framework --output <新目录>`，Python 3.12.13 + forgeharness 0.8.3 |
| 前端浏览器 | Playwright chromium（已缓存），`next build` 与 `build:static` 均通过 |
| 隔离目录 | 测试数据目录、checkpoint、覆盖率产物均在临时目录或 `output/collaboration-readiness/`（已忽略），未指向业务库 |

## 2. 静态、依赖与工程脚本

| 检查 | 命令 | 结果 |
|---|---|---|
| 依赖同步 | `uv sync --locked --group dev` | 90 个包，已是最新 |
| 依赖一致性 | `uv pip check` | 85 个包兼容 |
| 字节编译 | `uv run --locked python -m compileall -q app tests scripts` | 通过 |
| Ruff | `uv run --locked ruff check app tests scripts` | 通过 |
| Mypy | `uv run --locked mypy` | 53 个维护脚本无问题 |
| 旧静态脚本 | `for f in app/static/*.js; do node --check "$f"; done` | 通过 |
| 依赖导出 | CI 的两条 `uv export --locked` + `diff` | `requirements.txt`、`requirements-dev.txt` 一致 |
| 环境诊断 | `scripts/doctor.py`（隔离数据目录） | 通过；`pdfinfo`/`pdftotext`/`pdftoppm`/`tesseract` 均缺失，OCR 不可用 |
| 供应链（Python） | `pip-audit -r requirements.txt --no-deps` | 修复前 3 个包 20 项告警（pyjwt 2.13.0、urllib3 2.7.0、certifi 2026.7.22）；升级后无已知漏洞 |
| 供应链（前端） | `pnpm --dir frontend audit` | 无已知漏洞 |
| 供应链（Git 历史） | gitleaks | **未执行**：PyPI 无该工具、本机无二进制；仅做受限正则抽查（AWS/私钥头），无命中，不能等同 gitleaks |

## 3. 完整离线测试与覆盖率

命令（等价 `.github/workflows/test.yml`，独立框架解释器已就绪）：

```sh
LAW_REVIEW_FORGE_PYTHON=<框架环境>/bin/python \
LAW_REVIEW_DATA_DIR=<隔离目录> LAW_REVIEW_LANGGRAPH_CHECKPOINT_DB=<隔离目录>/checkpoints.sqlite \
LAW_REVIEW_AUTH_MODE=local LAW_REVIEW_LLM_URL=http://127.0.0.1:1/v1 REDIS_URL=redis://127.0.0.1:1/0 \
PYTHONHASHSEED=0 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
uv run --locked pytest tests scripts/test_engineering.py scripts/test_data_snapshot.py scripts/test_snapshot_runtime.py \
  -p pytest_cov -p pytest_socket --disable-socket --allow-unix-socket \
  --junitxml=output/collaboration-readiness/ci/junit.xml --cov=app --cov-branch \
  --cov-report=term-missing --cov-report=xml:... --cov-report=json:... --cov-report=html:...
```

结果：**1411 passed / 2 failed / 1 skipped / 287 subtests passed，耗时 211 s**；语句 13449、缺失 1534，分支 4260、部分覆盖 650，**综合覆盖率（含分支）86.13%**（门槛 70%，未调整），**纯分支覆盖率 78.36%**（3338 / 4260，缺失分支 922）。JUnit/XML/JSON/HTML 见 `output/collaboration-readiness/ci/`。以上为 Python 3.12 单版本结果，不代表 3.11/3.14 宿主矩阵完成。

跳过与失败均逐项裁决：

| 项 | 结果 | 裁决 |
|---|---|---|
| `tests/test_sft_pool.py:128` | skip | 本机未安装 `mlx_lm`，属可选本地后端；不计为通过 |
| `tests/test_corpus_audit.py::test_real_frozen_corpus_is_classified_cleanly` | fail | **资产问题**：本机 `output/score85/` 只有 66 篇的 canonical 快照，`boundary_difference` 期望 1（民法典附则泄漏）实得 0；断言未放宽 |
| `tests/test_corpus_canonical.py::test_real_corpus_builds_clean_canonical` | fail | **资产问题**：本机语料重建后 `publication_count=66`、`duplicate_keys_merged=0`，期望 ≥71 与 19 次合并；说明缺少测试假设的原始来源目录（`output/` 被 `.gitignore` 忽略，源包不携带），代码重建路径本身返回 0 且 canonical 不重复 |

两项语料失败的复现依据：`uv run --locked python -m scripts.build_canonical_corpus --root output/score85 --output <临时目录>` → `publication_count=66 / duplicate_keys_merged=0 / boundary_difference=0`；`中华人民共和国民法典` 第 1258 条不含 `附` 开头的行（附则泄漏已不在本机资产中）。因此不是代码或断言回退，而是本地资产晚于/少于测试假设的组合。按计划第 6.3 节，需在使用指定语料版本的机器上复验，未降低计数与边界断言。

## 4. 前端门禁与实际页面

| 检查 | 结果 |
|---|---|
| `pnpm install --frozen-lockfile` | 已是最新 |
| `pnpm contract:check` | 通过（新增 `GET /api/reports/{id}/frozen` 已同步 `schema.d.ts`） |
| `pnpm typecheck` / `pnpm lint` | 通过 |
| `pnpm test` | 11 passed |
| `pnpm build` / `pnpm build:static` | 通过，`frontend/out` 重新生成 |
| `pnpm audit` | 无已知漏洞 |
| `pnpm test:e2e` | 22 passed（桌面 + 窄屏），WebServer 实际加载 `frontend/out` |

页面变化：报告面板改为“选模板 → 新建草稿 → 提交冻结 → 冻结预览 → 批准/退回 → 下载指定版本”；正式下载链接只携带 `report_id`，不再允许替换模板。e2e 中导出链接文案随之改为“下载预览包”。

## 5. 发布候选与隔离实例验收

| 项 | 结果 |
|---|---|
| 候选包 | `scripts/package_release.py --with-frontend --output <私有目录>/release.zip`：446 个成员，`RELEASE_MANIFEST.json` 声明 `frontend_build_included=true`、`private_runtime_data_included=false` |
| 包摘要 | `sha256(release.zip)=1b527ce317ee38bc15c7be6d8704138a49120638005d412da0e7bffffa6505d5` |
| 解包校验 | 解压到新目录后逐成员核对：446 个文件 0 处缺失或摘要不符；`frontend/out/index.html` 存在 |
| 启动 | 从解包目录 `uv run --locked uvicorn app.main:app --host 127.0.0.1 --port 18767`，隔离数据目录，token 模式，`LAW_REVIEW_LLM_URL=http://127.0.0.1:1/v1`，Redis 不可达（批量导入禁用） |
| 签发链路 | `scripts/release_smoke.py`：`stage=passed`；静态首页 5192 字节且带 CSP；管理员登录；成员与案件角色；上传合成材料；证据；模板；报告提交后冻结 `版本 #1`（正文摘要 `22be99f4bf02`）；冻结预览可用；复核律师新建报告被拒（403）、批准归属为复核律师；正式下载 `报告 #1 v1`，包摘要 `b10f6dccffb8`，清单与文件摘要一致 |
| 容器交付 CI | **未执行**：本机 Docker 守护进程未运行（`docker info` 连不上 socket）。按计划不宣称容器路径通过 |

候选包不含模型权重、案件、凭证与原始报告；smoke 仅使用合成材料，数据目录为一次性临时目录。

## 6. 未执行项与前提

| 项 | 状态 | 前提 |
|---|---|---|
| Python 3.11/3.14 宿主矩阵 | 未执行 | 按当前 `test.yml` 在独立环境执行；M2 另用独立 Python 3.12 框架环境 |
| M1 旧库副本迁移与恢复 | 未执行 | 需登记获准源目录、`user_version`、独立 checkpoint 路径、保管配置与停写窗口；本机不得直接打开业务库 |
| U1 五身份与真实环境验收 | 未执行 | 需登记目标硬件、人数/并发、典型页数与流水量、模型 ID、OCR 类型、时延/错误率、恢复目标、业务复核人与交付接收人 |
| A13 真实模型与扫描件 OCR | 未执行 | 本机无 Poppler/Tesseract/中文语言包，也没有授权模型服务 |
| A14 并发容量 | 未执行 | 需在目标硬件与登记阈值下执行 |
| A15 告警闭环 | 未执行 | 本版无内置告警通道，仅有只读巡检与值班建议 |
| 容器交付 CI | 未执行 | 需要可用的 Docker 守护进程 |
| gitleaks 历史扫描 | 未执行 | 需要可安装的 gitleaks 二进制或联网获取 |

## 7. 逐项复核要点

- R1：报告与任务的复核/管理判定改为“成员权限 ∩ 案件角色 ∩ 案件授权”，撤权在下一请求生效；环境令牌无案件角色时除管理员外不能复核，已写入验收手册权限矩阵。
- R2：提交即冻结正文，批准校验冻结文件与来源摘要，导出只读冻结字节；模板、问答、材料、证据、流水任一变化均要求重新冻结与审批；历史批准无冻结正文时拒绝正式交付。
- R3：巡检只读打开数据库、`--multi-user` 分层、模型探测区分连通/列表/目标模型/推理、恢复演练覆盖完整性/外键/表分类/冻结工件，并记录耗时。
- 上述修复的自动回归：`tests/test_permission_intersection.py`(9)、`tests/test_report_frozen_delivery.py`(12)、`tests/test_collaboration_acceptance.py`(13) 全部通过；`scripts/test_data_snapshot.py` 扩展冻结工件恢复断言(10 passed)。
