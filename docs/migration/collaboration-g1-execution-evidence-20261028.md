# G1 续行执行证据：法条恢复、版本矩阵与候选交付

- 执行日期：2026-10-27 至 2026-10-28（Asia/Shanghai）。
- 业务代码及资产契约受检提交：`fb7b0aa`；凭证扫描误报裁决：`476c5a8`。本报告为后续文档提交。
- 工作分支：`codex/collaboration-g1-execution`。原共享目录切换到其他任务并出现未提交改动后，本轮在独立工作树继续，未搬动或提交其他任务的改动。
- **结论：G1-A–E 本地验收完成，放行范围限定为本报告登记的新冻结资产、macOS Python 矩阵和指定基础镜像覆盖的 Linux 容器路径。** 未运行托管 CI；默认 Docker Hub 基础镜像路径在本机超时，不能记作通过。M1 真实旧库迁移及 U1 律所验收仍未执行，不作生产就绪结论。

## 1. 工作包结果

| 工作包 | 实际结果 | 证据 |
|---|---|---|
| G1-A 法条资产 | 官方重取五份缺失历史文本；七个明确输入目录共 71 份、7,921 条，全部原文重新解析验证通过 | [恢复说明](../benchmarks/collaboration-corpus-recovery-20261028.md)、[冻结契约](../benchmarks/collaboration-corpus-freeze-20261028.json)；原始资产及 `all-source-reextraction.json` 私有保管 |
| G1-B Python 矩阵 | 3.11/3.14 各 1421 passed / 0 failed / 1 skipped；独立 3.12 框架实际进程测试已执行 | 两套 JUnit、覆盖率及逐步退出码，见第 2 节 |
| G1-C 历史扫描 | gitleaks 8.30.1 完整本地 Git 历史扫描通过，代码受检时 146 个提交、退出 0 | 第 3 节；脱敏 JSON 和日志 |
| G1-D 容器交付 | 源码 ZIP 429 文件，解包摘要 0 差异；指定镜像覆盖后成功构建，真实 HTTP 验收通过 | 第 4 节；保留默认镜像超时记录 |
| G1-E 候选冻结 | 含前端 ZIP 451 文件，摘要 0 差异；解包进程加载候选自身代码，签发链路通过 | 第 5 节；候选提交固定为 `fb7b0aa` |
| M1/U1 | 待登记源库、checkpoint、停写窗口、目标设备、模型及人工复核安排 | 第 7 节；未打开业务库或调用真实模型 |

## 2. 完整离线矩阵

在各自新建的源码副本、独立 venv、数据目录、checkpoint、覆盖率和报告目录中执行。每份源码与受检提交对应，冻结法条通过 `LAW_REVIEW_CORPUS_REGRESSION_ROOT` 显式传入；固定 LawBench 资产仅在私有测试环境提供，不进入源码包。

| 环境 | Python / SQLite | 完整结果 | 综合覆盖率 | 纯分支覆盖率 |
|---|---|---|---|---|
| macOS arm64 | 3.11.15 / 3.53.1 | 1421 passed / 1 skipped，220.49 s | 86.13% | 78.35%（3347/4272） |
| macOS arm64 | 3.14.6 / 3.53.1 | 1421 passed / 1 skipped，206.98 s | 86.14% | 78.42%（3350/4272） |

两套 JUnit 均为 0 errors / 0 failures，另含 287 个通过的子测试。唯一 skip 为可选 `mlx_lm` 后端未安装；法条、固定 LawBench 与 M2 均未跳过。框架解释器为独立 Python 3.12.13 + forgeharness 0.8.3；未用宿主解释器冒充框架进程。

两版本的 `uv pip check`、doctor、compileall、Ruff、Mypy（53 个维护脚本）、旧 JavaScript 语法、两份 requirements 导出 diff 和发布清单检查均退出 0。Node 为项目固定 24.22.1，uv 为 0.12.0；未修改锁文件或覆盖率 70% 门槛。

测试范围和参数沿用 `test.yml`：

```sh
pytest tests scripts/test_engineering.py scripts/test_data_snapshot.py scripts/test_snapshot_runtime.py \
  -p pytest_cov -p pytest_socket --disable-socket --allow-unix-socket \
  --junitxml=REPORT_DIR/junit.xml --cov=app --cov-branch \
  --cov-report=term-missing --cov-report=xml:REPORT_DIR/coverage.xml \
  --cov-report=json:REPORT_DIR/coverage.json --cov-report=html:REPORT_DIR/htmlcov
```

执行时使用锁定环境的 `uv run --locked`，配置 `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`、`PYTHONHASHSEED=0`、独立数据/checkpoint、`LAW_REVIEW_FORGE_PYTHON`，并将模型及 Redis 地址指向不可达回环端口。原始命令和逐步退出码保存在两份 `result.json`。此为本地等价范围，未表述为 Ubuntu 托管 CI 已运行。

原有 66 份资产在两版本下继续复现两项失败。途中一次隔离副本缺固定 LawBench 导致额外 7 项 skip，另一次启动遗漏 Node PATH 导致 18 项环境失败；均保留负结果，最终上述完整重跑同时补齐 Node 与固定数据。最终测试对应本次提交内容。

## 3. gitleaks

从 [官方 v8.30.1 发布](https://github.com/gitleaks/gitleaks/releases/tag/v8.30.1)获取 arm64 二进制并核对官方 checksums：归档 SHA-256 `b40ab0ae55c505963e365f271a8d3846efbc170aa17f2607f13df610a9aeb6a5`。工具保存在临时工具目录，未写入系统或仓库。

使用 `gitleaks git --log-opts=--all --redact=100 --report-format=json`，非浅克隆，默认规则和已有精确 `.gitleaksignore`。首次扫描发现本地初始提交中的两处已知误报：合成案件键 `v2-tender-withdrawal`、vis-network 的 `_getDistanceToBezierEdge2` 方法名；历史源文件与当前字节一致。`476c5a8` 仅追加这两个历史指纹，未关闭规则、未整路径排除，复扫退出 0。`fb7b0aa` 上全历史复扫同样为 0 findings。

## 4. Linux 容器与真实队列/OCR

Docker Engine 29.9.1，Linux aarch64，容器 Python 3.12.15。默认 `python:3.12-slim` 构建因 Docker Hub 鉴权请求超时失败，日志保留；随后使用 Dockerfile 已支持的 `PYTHON_IMAGE` 参数，按 ECR 中 Python 镜像的固定摘要构建：

```sh
docker build \
  --build-arg PYTHON_IMAGE=public.ecr.aws/docker/library/python:3.12-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258 \
  -t lexvault-g1-20261028:recovered UNPACKED_SOURCE
```

源码 ZIP SHA-256：`1cdc0deca95de944c3a0ed907f01a95b9a7a04879fb647db744ffee2c6ef4a9b`。镜像 ID：`sha256:35448462da0d3f67eb5baaa55dd5051a360f9196d3d4a24b6817354d295cc457`。Next.js 在镜像构建内使用 Node 24.22.1 / pnpm 11.25.0 重新生成静态界面，运行进程实际加载 `/app/app/main.py`。

一次性 token、数据卷和私有网络中执行：离线问答定位第 2 页及导出摘要、`release_smoke` 的登录/权限/冻结/批准归属/指定版本 ZIP 均通过。另启真实 Redis 7 和 arq worker，HTTP 批量上传合成文件后状态为 completed、成功 1/失败 0、进度 100%。合成 PNG 经实际 OCR 上传解析，页 1 识别出预设金额 1200；镜像内 Poppler 与 `chi_sim/eng/osd` 语言包可用。这只证明合成样本路径，不代替中文扫描卷宗的业务准确率验收。

本次容器、worker、Redis、网络及数据卷均已清理；启动前为空闲的 Lima 实例已恢复停止状态，本地候选镜像保留。未更改 Dockerfile 默认镜像、用户 Docker context 或其他任务资源。

## 5. 含前端候选包

将本次镜像内实际构建的 `frontend/out` 复制到工作树的忽略目录，再执行 `scripts/package_release.py --with-frontend`。候选绑定 `fb7b0aa`，451 个清单文件，`private_runtime_data_included=false`；逐成员摘要核对 0 差异。

- ZIP SHA-256：`b29038b1c053ce60fb439f93a9d29d5c78370eb354e558179452ed30bb096ba4`。
- 解包后使用独立 token、数据与 checkpoint 启动，启动日志证明模块来自解包目录。
- `release_smoke` 返回 `stage=passed`：首页 5193 字节、CSP 存在；复核身份可批准但不可编制；冻结预览、指定报告版本正式下载及包内摘要一致。
- 候选报告正文摘要前缀 `06d4dac011bd`、交付 ZIP 摘要前缀 `f935dec8a08b`，均为本次一次性合成数据。

前端源码和依赖未修改；此前 22 项 E2E 等证据保留，本轮未将它们重新计为执行。镜像重新构建包含 TypeScript 构建检查，上述 HTTP 实际验证覆盖本次候选界面与签发链路。本报告及索引属于随后的文档提交；若从其后的 HEAD 重新打包，须生成新摘要，不能继续套用本候选 ZIP 身份。

## 6. 私有证据位置

工作树内 `output/collaboration-readiness/g1-execution-20261028/` 保存：两套最终 JUnit/覆盖率/日志/逐步命令结果、旧失败轮次、原文重新解析汇总、gitleaks 脱敏报告及工具摘要、默认镜像失败与最终构建日志、容器结果、候选 ZIP 及解包进程验收结果。

冻结输入为 `output/collaboration-readiness/recovered-corpus-20261027/`，canonical 为 `output/collaboration-readiness/recovered-canonical-20261028/`。全部保留在忽略目录，旧 `output/score85/` 未覆盖；迁移工作树或归档前需另行保管这些必要资产。文本版本新鲜度仍为 2023-12-29，不等于当前法律现行性。

## 7. M1/U1 尚未开始

M1 必须先登记实际服务的数据目录、独立 checkpoint、配置保管位置及停写窗口。建议按当前服务实际配置取停写快照，在新的私有目录完成升级和二次恢复演练，再安排正式库窗口；不从开发工作树猜测正式库位置。本轮未停止业务 API/worker，也未读取真实卷宗或迁移业务库。

U1 仍缺目标设备、人数/并发、模型 ID 与数据/费用范围、容量与恢复阈值、五身份执行人和独立业务复核人。真实模型推理、中文卷宗效果、容量及值守接收须在目标环境完成。下一步依[续行计划](collaboration-g1-to-acceptance-plan-20261027.md)第 6–8 节登记后执行；本轮未推送、合并或发布。
