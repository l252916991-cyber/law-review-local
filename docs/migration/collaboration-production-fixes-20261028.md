# 多人协作落地缺陷修补记录

- 执行与收尾日期：2026-10-28 至 2026-10-29（Asia/Shanghai）。文件名保留本轮开始日期。
- 分支：`codex/collaboration-production-fixes`；起点 `c768090`。
- 本轮范围：修补落地审查确认的七项问题，补充真正并发、故障恢复和页面回归；不操作真实卷宗或正式服务。
- 最终业务代码：`ac2246ec22613fd467b1531207baa6aa2cb45723`。本记录是后续文档提交；验证包与运行结果均绑定该业务代码。

## 1. 修补结果

| 审查问题 | 改动与实际断言 | 本地提交 |
|---|---|---|
| 原文纠错并发覆盖 | 写事务内复核材料版本，条件更新材料后再改页；真实双并发 HTTP 请求一成功、一 409，材料仅升一个版本，纠错与证据失效审计各一条 | `a026525` |
| 人工任务并发覆盖 | 流转、改派、改期限统一按版本和原状态条件更新；Next 每次提交读取版本，冲突后刷新。三组真正并发场景均只接受一次操作；失败不新增事件或审计 | `bb8c09b` |
| 打包期间依据变化仍可交付 | 正式登记的写事务内复查来源与全量流水；证据、页或流水在打包期间变化会拒绝且无正式登记。ZIP 与附件读取留在锁外；流水摘要分块计算，0/500/620 行与原正文摘要一致 | `604a78b` |
| Redis 丢队列无法恢复 | 启动及 worker 每 15 秒巡检核对所有 queued/processing 批次；固定任务 ID、Redis WATCH、本地批次锁保护重派和活跃原生线程；重复消费使用既有导入键去重 | `4e0c6dc` |
| 任务分给无本案权限者 | 创建、改派、专用候选接口均检查启用状态及本案有效查看/编辑权限；目录管理员沿用其全案工作权限。未分案、只读、已撤权、停用均拒绝；分派不自动扩张案件授权 | `bb8c09b` |
| 批量部分失败页面不停轮询 | 识别真实终态 `completed_with_errors`，刷新成功材料并显示失败原因，以中文显示结果；页面时钟推进后不再新增轮询 | `ac2246e` |
| 备份遗漏页面模型配置 | `model_settings.json` 纳入精确清单、摘要和恢复，保留权限；验证结构及无凭证 URL。兼容旧快照，缺配置时恢复报告明确要求核验环境默认模型；仍排除 `.env` | `09380fb` |

## 2. 最终验证

| 环境 | 完整离线结果 | 综合覆盖率 | 纯分支覆盖率 |
|---|---|---|---|
| macOS arm64 / Python 3.11.15 | **1442 passed / 1 skipped**，215.60 秒 | **86.08%** | **78.37%**（3381/4314） |
| macOS arm64 / Python 3.14.6 | **1442 passed / 1 skipped**，203.81 秒 | **86.20%** | **78.54%**（3388/4314） |

两套 JUnit 都是 0 errors / 0 failures，另含 322 个通过的子用例。唯一 skip 为可选 `mlx_lm` 未安装；法条、固定 LawBench 与独立框架实际进程均未跳过。M2 使用独立 Python 3.12.13 + forgeharness 0.8.3，没有以宿主解释器冒充框架运行。覆盖率维持原有 70% 门槛；未运行托管 CI，不将本地 macOS 矩阵描述为 Ubuntu CI 通过。

范围与参数遵循现有 `test.yml`：

```sh
pytest tests scripts/test_engineering.py scripts/test_data_snapshot.py scripts/test_snapshot_runtime.py \
  -p pytest_cov -p pytest_socket --disable-socket --allow-unix-socket \
  --junitxml=REPORT_DIR/junit.xml --cov=app --cov-branch \
  --cov-report=term-missing --cov-report=xml:REPORT_DIR/coverage.xml \
  --cov-report=json:REPORT_DIR/coverage.json --cov-report=html:REPORT_DIR/htmlcov
```

执行时以各自锁定解释器运行，设置 `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`、`PYTHONHASHSEED=0`、独立数据/checkpoint/coverage 路径、`LAW_REVIEW_FORGE_PYTHON`；模型和 Redis 指向不可达回环端口。法条沿用 G1 冻结契约，经 `LAW_REVIEW_CORPUS_REGRESSION_ROOT` 传入。首轮因工作树内固定 LawBench 目录为空而各为 1435 passed / 8 skipped，保留于 `first-python-*`；补齐现有公共验证输入、逐文件登记摘要后，重新执行上述完整范围得到最终矩阵，没有改动测试断言。

前端使用 Node **24.22.1** / pnpm **11.25.0**：固定锁文件安装、契约生成及检查、类型检查、lint、11 项单测、静态构建通过。FastAPI 托管此次重新构建的 `frontend/out/`，Playwright 桌面/手机 **28 项通过**。新增任务候选与陈旧界面操作使用真实合成 API；批量部分失败页面使用后端终态契约响应，并通过真实上传 API 放入成功材料，队列本身另有下节真实 Redis 演练。

首轮页面测试为 26 passed / 2 failed，原因是新增测试的错误提示选择器同时匹配 Next 路由播报器；将选择器限定到实际业务错误提示后全量 28 项通过。首轮痕迹保留于 `frontend-e2e-first/`，没有降低业务断言。

Ruff、Mypy（54 个工程脚本）、compileall、依赖兼容检查、两份 requirements 导出一致性、遗留 JS 语法、doctor 及发布白名单检查通过。doctor 如实报告本机未配置 Tesseract/完整 PDF 文本工具；本轮不将它写成目标 OCR 验收通过。全本地 Git 历史 gitleaks 扫描无发现，未添加新的豁免规则。

## 3. 真实 Redis 恢复与迁移演练

使用缓存的 Redis 7.4.11 镜像、新建随机命名容器、回环随机端口和独立合成库。实际 arq Worker 加载当前 `app/tasks.py`，结果：

- 已确认入队的 queued 批次清空测试队列后，第一次重派 **1**，第二次 **0**；实际消费成功 1、失败 0。
- 强制再次投递同一批次，材料仍只有 1 份。
- 原生索引线程活跃时清空一次性 Redis，重派 **0**，索引调用 **1**，原任务成功完成。

临时容器已清理；不操作业务 Redis、不改变 Docker context，不清理其他退出容器。`redis-drill/cleanup.json` 确认原有三个退出容器保留、无运行容器、Lima 恢复原始 Stopped 状态。首次独立演练驱动因导入路径遗漏而退出，负结果另行保留；修正忽略目录中的驱动后重跑通过，产品代码未变。

合成历史库迁移驱动在最终代码再次通过：schema **16→26**，1 个案件、6 份材料、15 页、5 条证据、1 次 Agent 运行及 9 条 checkpoint 保持指定列/摘要；双身份冻结签发、二次恢复后的 4 个交付成员一致，升级前快照由历史程序初始化回退通过。总计 **6.51 秒**，仅为小样本演练耗时，不推算生产恢复时间。

## 4. 候选包与证据

候选包 `candidate-fixed.zip` 包含 **458 个清单文件**及本次前端构建，SHA-256：`9f0e1e89910600739240c75b9b796060d6bd661d625ddadc1931f165e6b7e6f1`。逐成员验证 **0 差异**；解包新目录后以随机令牌及隔离数据启动，日志确认加载解包目录代码，`release_smoke` 的登录、复核权限、冻结、批准人归属及正式下载摘要均通过。

证据根目录为忽略路径 `output/collaboration-readiness/production-fixes-20261028/`，保存双版本 JUnit/覆盖率、前端日志与负结果、Redis 演练、迁移快照、候选包及解包实例日志。公开 LawBench 验证输入逐文件身份记于 `lawbench-input-identity.json`，不随候选包或 Git 提交分发。

`summary.json` 汇总最终代码与结果。Redis 演练 `result.json` SHA-256 为 `2e4556379c2af75a287688866fc3c0bf46980243eb0b87a53b235060e4ede6d4`，迁移演练 `result.json` SHA-256 为 `b16272de9c6d1acaeda9e9b20910e4b5ce2fdf7be17d468ed53cad0328f4fd46`。

本报告与索引晚于业务代码；若从后续 HEAD 重新打包，应生成新清单和新摘要，不能继续套用上述 ZIP 身份。

## 5. 适用范围

本轮七项缺陷修补及所列工程回归已完成，可作为律所受控试点候选。真实旧库（含 v13）副本迁移、目标设备 TLS 与五身份人工验收、中文扫描卷宗和真实模型效果、多人容量及备份值守仍按[原续行计划](collaboration-g1-to-acceptance-plan-20261027.md)执行；本报告不替代这些接收条件。

队列恢复要求同一实例共用本地文件系统；不可手工删除批次锁文件，不承诺跨主机/NFS。正式交付最终校验仍会扫描全量流水，写锁时间随规模增长，目标容量需要实测。没有改 schema、运行真实模型、推送、合并或发布生产服务。
