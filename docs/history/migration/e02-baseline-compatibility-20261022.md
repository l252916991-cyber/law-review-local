# E02 源码/依赖摘要、发行物与兼容矩阵（2026-10-22）

依据：[执行计划书 §5.1 E02](execution-plan-20261022.md)、[迁移台账](ledger.md)。

2026-10-23 后续：本文保留 E02 时点的 0.6.0 基线与兼容记录；当前 0.7.1 绑定、依赖修复及新工程证据见[本批实施记录](next-batch-execution-20261023.md)与 `vendor/FRAMEWORK_BINDING.json`，不以旧资格代替新候选验证。

状态：**已完成（Git 部分如实标记未解决）**。主责 PM/QA（QA 主责）；本文由自动化代理整理，实际承担人由 PM 指定。

方法：所有摘要在本机**亲自复核**（`shasum -a 256` / 清单工具 / `git rev-parse`），不转抄他人报告；行号与文件路径逐一核对。

## 1. 基线与源码摘要

| 项目 | 冻结时点 | 摘要 | 文件数 | 说明 |
| --- | --- | --- | --- | --- |
| LexVault | M2 工程收口 | `5a5cbc776160953312e880d4f38d689d7b9e833d8ece038b6083f6f853860c2f` | 219 | 执行计划书编制前基线（`output/migration/m2-engineering-20261022/source-final.json`） |
| LexVault | 执行计划书 + 文档索引 | `9bc854be75d4cd4c11caed8e8a9d7c3b24376488cc1fe88a78369e5705322627` | 220 | 计划书自身入库后的文档修订 |
| LexVault | **E02 执行时点**（含 E01 总表与 E04 代码） | `1dc1b323dafcdf48438007edd97a4b40279580cc97aa2c760dcc67808ece4af0` | 225 | 相对 M2 收口 +6：`app/host_ports.py`、`app/forge_runtime.py`、`tests/test_host_ports.py`、`tests/test_forge_runtime.py`、`docs/history/migration/e01-entry-inventory-20261022.md`；另有文档索引变更 |
| ForgeHarness | 0.6.0 r1 候选冻结 | `13d50704639062208558cc6b94670808d071f6ad1c0a6b1f154380d1d458f39c` | 420 | `reports/release-0.6.0-r1/source.json`（清单 sha256 `4b79d94061c71d5acf62dda4d561323a49fba678dd13bfeebbee92e43b08eaa7`） |
| ForgeCommerce | 当前检出（= 远端 HEAD） | `ac48da0ec971e73d82d4e13031d9555b7715ea3667c8fd10b958982257d13e0b`（参考口径：根文件+src/tests/scripts/docs+vendor，79 文件） | 79 | 权威身份是其 git HEAD `6a752d845e0135fcf2da67fb81beef87c8b3fc21`；内容摘要与 2026-10-21 计算值一致，未变 |

口径说明：LexVault 摘要 = 发布允许清单（`scripts/package_release.py source_files()`），与框架侧 `benchmarks/framework_snapshot.py` 同算法；两侧**不混用**历史候选与当前候选（历史值仅用于对照链）。

## 2. 依赖与锁摘要

| 项目 | 文件 | sha256 | 其他 |
| --- | --- | --- | --- |
| LexVault | `uv.lock` | `f3f8aef1e189e33060d2ef1d53c1cb2395db0a00d4e4ee868b0d4c5d83be4f26` | `requirements.txt` `59e7f2a611ec69dd…`、`requirements-dev.txt` `1d2316ae97f30f40…`（CI 校验导出一致性） |
| LexVault | Python 环境 | — | 主环境 3.12.13；矩阵 3.11.15 / 3.14.6（uv 管理，`.venv-matrix-*`）；框架解释器独立 3.12（`.venv-framework-m2`，`scripts/setup_framework.py` 重建） |
| ForgeHarness | `uv.lock` | `9677f461a8f455fb34199a98ad09703399ac7fe42b26dc39a87e61766cffbcce` | 依赖修复后锁（aiohttp 3.14.5 / certifi 2026.10.19 / pillow 13.0.0）；两个对照环境锁单独固定 |
| ForgeCommerce | `uv.lock` | `fe6ea2c101452244f7d7ed352f54746fdababfe6b34a289b5ecd39b7b35577d1` | 框架依赖指向 `vendor/forgeharness-0.5.0-py3-none-any.whl`（path 源） |

## 3. 发行物登记（旧件与新件分开）

| 发行物 | 版本 | sha256（前 16） | 资格状态 | 用途/状态 |
| --- | --- | --- | --- | --- |
| `lexvault-v0.3.0-source.zip` | 0.3.0 | `25752ff03d486c77…` | 历史归档 | 回滚/对照的历史参考（早于 10 月改动） |
| ForgeHarness `dist/forgeharness-0.1.0` | 0.1.0 | 未登记（早于迁移） | 历史件 | 仓库 `dist/` 保留，不用于迁移 |
| ForgeHarness `dist/forgeharness-0.2.0` | 0.2.0 | 未登记（早于迁移） | 历史件 | 同上 |
| ForgeHarness wheel（电商绑定） | 0.5.0 | `58579893b83368a3…` | engineering_slice_qualified（2026-10-17） | ForgeCommerce `vendor/`；M7 升级到具备所需能力、重新资格化的共同候选，不预设为 0.6.0 |
| ForgeHarness wheel（M1 初冻，已换绑） | 0.5.0 | `8275baba6a08bb59…` | 6 项审计未处置 | 台账 §2 留档；权威副本在框架 run 目录 |
| ForgeHarness wheel（首轮诊断，仅内部） | 0.6.0 | `de16f0f7…` | 未资格化 | `reports/release-0.6.0/` 证据内；**不得对外引用** |
| ForgeHarness wheel（当前绑定） | 0.6.0 | `69ce604c51325085…` | **87/87 门禁、qualified=true**；业务资格未评 | LexVault `vendor/` + 发布包（B3 路径）；sdist `cbea0088924398fe…` |
| ForgeHarness 运行依赖清单 | 0.6.0 | 见 `vendor/FRAMEWORK_BINDING.json` | 随冻结发行物 | `vendor/forgeharness-runtime-requirements.txt`（带 hash 的核心依赖） |

## 4. 数据与 schema 兼容矩阵

| 层 | 版本/结构 | 兼容规则 | 回滚 |
| --- | --- | --- | --- |
| LexVault 业务库 | schema **v15**（`SCHEMA_VERSION`，`app/storage/db.py:379`） | v13→v14（`runtime_requests` 映射表）→v15（`request_digest`/`principal_name` 绑定列）增量迁移；高版本拒绝降级 | 旧代码（≤v13）打开 v15 库按既有防降级拒绝；回滚需恢复备份 + `output/migration/m0-baseline-20261021/pre-change/` 文件级回滚基线 |
| LexVault 运行表 | `agent_runs`/`agent_steps`/`review_jobs`/`memory`/`gap_detections`/`audit_log` | 执行状态仍由这些表单一拥有；框架映射表只做关联（M1 契约 §5） | 同上 |
| LangGraph 检查点 | 独立 SQLite（`LAW_REVIEW_LANGGRAPH_CHECKPOINT_DB`） | 线程 ID `agent-run-{run_id}`；恢复前校验案件/线程/问题一致 | 与业务库同目录备份 |
| ForgeHarness SDK | SDK API **1**（当前绑定 wheel） | 公共 SDK 按框架兼容策略演进，新候选重新资格化 | 发行物按 sha256 指认，不覆盖旧件 |
| LexVault 宿主契约 | 协议 **v2**；事件 **v2**；结果 **v1**；schema 侧 `runtime_requests` 见上 | 附加式变更不升版；语义/顺序变化升对应版本并登记（m1-contracts §8） | 此为宿主接入契约，不等同框架 SDK 或持久状态版本 |
| 框架持久状态 | `Harness` state_dir（run checkpoint/journal/approval；M2 用 `framework-state` 于临时目录） | R2/E09 定义编排状态协议；当前自定义编排仅只读一次性 | 临时目录随会话清理 |
| 电商业务库 | `commerce.sqlite` 21 张表（`store.py:34-98`），加列迁移 `:99-122` | 幂等建表 + 加列 + 从 audit/messages 回填 | 停服备份脚本 `scripts/backup_state.py`（backup API + integrity_check） |
| 电商框架状态 | `revisions.sqlite`、`retrieval.sqlite`、`sessions/sessions.sqlite` + `executions/<sid>/*.jsonl` | 框架 RevisionStore/SessionService 自带 schema | 同上 |
| 数据目录/权限 | LexVault `data/`（私有运行数据，不入清单）；电商 `.state/` 700、各 sqlite 600 | 私有运行数据不进任何发布清单 | 备份流程见各项目 runbook |

## 5. Git 来源与授权状态

| 项目 | 现状（本人复核） | 原仓库 | 恢复/初始化方案 | 授权状态 |
| --- | --- | --- | --- | --- |
| LexVault | **无 `.git`** | `github.com/l252916991-cyber/law-review-local`（**公开**；HEAD `58dacddbe8521dfd…`=2026-09-16 v0.3.0 交付；策略基线锚点提交 `b2856be5…` 存在；本机 `api.github.com`/`raw.githubusercontent.com` 可达，`github.com:443` git 协议不通） | 方案 A：git 协议恢复后 clone 历史并把当前工作树作为后续提交；方案 B：经明确授权本地 `git init` 建立基线（只纳入审查过的路径，不整体收录） | **未授权**；按计划书 §9 不自动初始化 |
| ForgeHarness | **无 `.git`，未发现远端** | 未识别（README 无仓库地址） | 同上；需 PM/FW 提供来源或授权本地初始化 | **未授权** |
| ForgeCommerce | **是 git 仓库**：`main...origin/main`，工作区干净，HEAD `6a752d845e0135fcf2da67fb81beef87c8b3fc21`，`origin` = 私有 `github.com/l252916991-cyber/forgecommerce.git` | 同上 | 已具备提交历史；本地检出与 origin/main 同步（无 ahead/behind 标记） | 已具备（无需新建） |

事实修正（供台账更新）：执行计划书编制前提"两侧目录缺少 Git 元数据"对电商不成立——电商有完整 git 历史与私有远端，可作为另两侧"恢复/初始化"的模板（分支 `codex/<主题>`、原子提交、CI 触发）。

## 6. 可重建性声明（旧部署可重建？）

**可重建**：LexVault 源码（发布允许清单 + 清单摘要 + `uv.lock` 与带 hash 的 requirements）、框架发行物（wheel/sdist + 源码清单 + 依赖锁 + 绑定记录）、依赖环境（`uv sync --locked`；框架解释器 `scripts/setup_framework.py` 带 hash 安装）、发布包重建与审计（`scripts/verify_deployment_release.py`，见 B3 证据）。

**不可重建或受限**：
1. **真实模型权重身份**：本机未核验实际加载权重；同模型名不保证同权重（策略基线已声明）。真实模型评测（E07/E17）前必须冻结权重身份。
2. **冻结法条语料**：设计上不分发（2 项测试跳过）；启用需按 runbook 构建并冻结 manifest 身份。
3. **历史运行证据**：`output/` 与框架 `reports/` 为本地证据位置，不随源码发布；跨机交付需另行保留经审查的非敏感证据。
4. **Git 历史**：LexVault/框架未恢复（§5）；在恢复前，变更可追溯性依赖内容寻址清单 + `pre-change/` 差异基线。

## 7. 历史/当前候选不混用（核查）

- 框架：`reports/framework-next`（`783e5fbd`，历史）、`reports/release-0.6.0`（首轮诊断 `de16f0f7`，内部）、`reports/release-0.6.0-r1`（**当前** `13d50704`/`69ce604c`）三者分目录保留，未互相覆盖。
- 律所：`output/migration/m0-baseline-20261021`（M0）、`m1-regression-20261021`（M1）、`a-batch-closing-20261021`（A 批次）、`m2-engineering-20261022`（M2）、`e04-host-ports-20261022`（本轮 E04）各自独立；M2 收口摘要仍以 `source-final.json` 为准。
- 电商：以 git HEAD 为唯一身份，历史报告（`reports/`）标注为升级前证据，不自动适用于新候选（计划书 §2 已声明）。

## 8. 证据与未决

- 复核命令与输出：本会话记录；`output/migration/e04-host-ports-20261022/`（E04 验证）；`output/migration/e01-entry-inventory-20261022/routes.txt`（E01）。
- 未决：Git 恢复/初始化授权（LexVault、框架）；E01 §1 三个候选项目；框架权重身份（E07 前）。
