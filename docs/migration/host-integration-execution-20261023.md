# 宿主接入实施记录：隔离写入与持久恢复

日期：2026-10-23（北京时间）。依据：[执行计划](host-integration-plan-20261023.md)。实施、测试和自审均为 Codex；无独立复核人，未代替 PM 或 DOMAIN 确认任何业务条件。

## 1. 本次范围与结论边界

生产容量仍待确认，因此按计划第 2、6 节交付 H00/H01 及 H02–H05 的**合成隔离原型**。六类宿主写入、同事务回执、持久请求/run/会话关联、实际 0.8.3 wheel 的显式持久图、真实进程故障恢复与只读双对照已经实现。设计见 [H01 决策](host-integration-design-20261023.md)。

本次没有将合成图作为真实法律审查编排，没有接入正式 `RuntimeControl`/`review_jobs`，没有切换 HTTP/UI 默认入口。**E12 生产全范围、E13 实际领域链路、E07/G2、E14 均未验收。** `FrameworkReadonlyControl` 继续显式拒绝持久 inspect/resume/status；正式后台的 `_start_run` 临时替换仍按 E14 处理。

## 2. 基线与交付

当前目录没有 `.git`，没有分支或可提交基线；未初始化 Git、未创建提交、未推送。修改前发布允许清单：237 文件，tree_digest `542377cb91b7665fa292691616fc799015ba44f4f52b3401fcf4b5cde134b968`。全部允许清单文件的修改前副本、原始失败、最终差异及命令保存在：

`output/migration/host-integration-20261023/20261023T060303Z/`

最终源码身份由该目录 `source-final.json` 给出，不在本文件自引用摘要。发行物仍为原精确 0.8.3 wheel，未修改框架源码或 wheel，未继承旧宿主测试的通过结论。DevSpace open_workspace 返回内部错误后，使用本地文件、补丁和测试工具完成。

| 工作包 | 本次交付 | 状态 |
| --- | --- | --- |
| H00/H01 | 基线副本、摘要、配置和 E03 复核；六动作字段、事务、身份、版本、状态与故障设计 | 完成；实现者自审 |
| H02 | `LocalHostWritePort` 六类动作和授权 query_receipt；输入版本/效果摘要；同事务持久幂等回执 | 隔离原型完成；生产容量未确认，不作 E12 全验收 |
| H03 | 原子受理与唯一会话，持久 request/job/host run/framework run 关联，准入 4、OS worker lease 2、取消受理、未知责任保留 | 隔离控制完成；未接正式后台任务执行器 |
| H04 | `host_integration_worker.py` 独立解释器；显式 DurableGraph、join、宿主回调、真实回执 resolver、审批恢复及只读投影重建 | 合成图完成；原专家节点仍走既有只读链路 |
| H05 | 故障测试、增量迁移、防降级、生命周期及发布重建；完整离线验证 | 最终复跑结果见第 4 节 |
| H06 | 补各自检索/记忆对照，保留共享输入对照；复核后端旁路；只读请求版本按实际 wheel 绑定 | 离线部分推进；真实后端、材料与模型身份未落实，G2 未验收 |

## 3. 故障验收映射

专项测试为 `tests/test_host_writes.py`、`tests/test_host_integration.py` 和仅供测试的进程 driver。SIGKILL 注入用独立进程组，同时终止宿主与 sidecar；新宿主进程和新独立框架进程验证恢复。测试没有修改精确 wheel；journal/checkpoint 窗口通过测试入口包装 SDK `_complete` 确定性终止真实进程。

| 场景 | 证据与边界 |
| --- | --- |
| H-A01 | 六动作首次/重复/查询，各一份效果和稳定 receipt_id；草稿不会进入已验证记忆召回 |
| H-A02 | 六动作同键换内容/非法字段；主体、案件及源版本冲突，跨案/跨主体回执查询拒绝 |
| H-A03 | 当前 token 配置撤权，案件/证据/合成知识变化、归档及删除；历史成功和恢复重新核验。外部法条身份继续由只读来源测试覆盖 |
| H-A04 | 写入前与业务 SQL 后、回执提交前 SIGKILL 均无半份草稿；宿主提交后丢响应，新进程查原回执并对账。没有回执但已派发则保留 unknown |
| H-A05 | 原子受理后 SIGKILL、框架完成但宿主投影前 SIGKILL；同一映射、run、会话和 framework run 重用 |
| H-A06 | journal 已保存、节点 `_complete` 前 SIGKILL；新进程复用结果，无重复消息、草稿或审计。没有实际模型费用，不能外推付费模型结论 |
| H-A07 | 同 request_id 并发受理只生成一条关联；参数冲突拒绝 |
| H-A08 | 待审批重启、重复批准不产生新效果；完整动作摘要变更使原图/审批身份不符而拒绝。框架执行审批不改变证据业务审批 |
| H-A09 | 重复/乱序事件及终态倒退拒绝；显式 join 与完成结果重用。实际分布式预算竞争、外部调用并发不在本原型范围 |
| H-A10 | 持久准入 4、worker lease 2、重复取消、运行中取消、已提交效果取消后对账；未知未执行证明不会因取消而被删除 |
| H-A11 | SIGKILL 断连、保存协议不符、绝对排队超时、框架状态丢失不重建；进程 finally 清理。生产整次超时待 PM |
| H-A12 | v15→v16、重复启动、旧代码拒绝新 schema、历史 v14 请求兼容；删除级联/outbox 清理、实际 inspect 投影重建不执行工具。生产保留/备份恢复留 E18 |
| H-A13 | 新 Python 3.12 环境安装精确 wheel 与带 hash 依赖；实际宿主新进程闭环；发行包提取源码再运行并用第二宿主进程复用结果 |

## 4. 验证结果

本机 macOS 上按当前 CI 的隔离、插件和覆盖率参数执行完整离线检查，设置实际独立 `LAW_REVIEW_FORGE_PYTHON`，没有因缺少解释器跳过框架进程集成：

| 环境 | pytest | 分支口径总覆盖率 |
| --- | --- | --- |
| Python 3.11.15 / Node 22.23.4 | 875 passed / 0 failed / 3 skipped | 84.88% |
| Python 3.12.13 / Node 22.23.4 | 875 passed / 0 failed / 3 skipped | 84.89% |
| Python 3.14.6 / Node 22.23.4 | 875 passed / 0 failed / 3 skipped | 84.88% |

3 项跳过均为既有条件：两项冻结官方法条语料未检出，一项 `mlx_lm` 未安装。未降低 70% 覆盖率门槛；独立 sidecar 通过实际进程测试验证，其代码不计入宿主解释器采集到的覆盖行。

Ruff、Mypy（41 个工程脚本）、compileall、doctor、Node 语法、锁定依赖安装/检查、两份 requirements 导出一致性、发布允许清单均通过。发布包提取源码后，新建独立框架环境，既有只读对照及新增写入闭环均通过；第二个新宿主进程复用原结果且业务效果/回执唯一。宿主依赖复用本机环境，没有声称全新宿主安装、Ubuntu 托管 CI 或真实 HTTP/UI 验收。

最终命令、环境与原始报告在 `ci-311/`、`ci-312/`、`ci-314/`、`static/`、`release/`，汇总在 `summary.json`。六个 SIGKILL 边界另外保留最终数据库、框架状态、连续两次新进程恢复及行数断言：`fault-matrix-final/`、`fault-matrix-final.json`；其中写入未提交的两种已派发不确定状态仍正确保持待对账，其余四种恢复为唯一成功结果。此前执行结果保留在 `pre-join-checks/`，不替代最终内容验证。

已发现并修正两项本轮问题：最初超短排队超时进入派发不确定状态，已改为在受理/启动同一锁内证明未派发后 exhausted；完整回归发现旧测试仍固定期待 v15，已同步为 v16，保留原列与迁移默认值断言。`targeted-third.log`、`full-312.log` 等初次失败未删除。

## 5. H06：后端与两类合成对照

当前实际路径：生成模型在 `agents.py` Critic、`services.py::llm_generate`；embedding/reranker 在 `rag.py`。私有快照的 `BackendLedger` 继续覆盖其既有 governed_backend 调用，hashed-local 用量标未知，脚本模型数据不是实际模型测量。正式入口与评测脚本的旁路仍在 [E01](e01-entry-inventory-20261022.md) 登记，未借本原型宣称它们已接线。

`compare_native(shared_inputs=False)` 在另一份私有副本中独立检索和召回，不安装 FrozenRetrieval、不传共享 memory_snapshot；共享输入模式保持原接口。CLI 现在同时保存两种结论。两份对照都使用既有宿主领域节点，未声称新增独立检索算法或评估法律质量。只读请求的显式 framework_version 从遗留的 0.6.0 修正为实际绑定版本，旧版本明确拒绝。

冻结的 8 道合成开发题分别保存 `comparison-question-1..8.json`；共享与各自检索对照各 **8/8 结构一致**，源业务库 dump 前后相同。框架来源校验 **7 次成功、1 次拒绝**；第 4 题“证人陈述之间存在哪些矛盾？”没有来源，明确 `verification rejected: no sources; evidence is insufficient`。保留原结果，没有更换题目或放宽 verifier。对照一致不等于该题有可核验答案，也不等于 G2 通过。

两份汇总为 `comparison-shared.json`、`comparison-independent.json`。首次对照脚本要求所有题成功而停止的原始日志保留为 `comparisons.log`；完整汇总在 `comparisons-complete.log`，区分一致性和来源拒绝。

## 6. 未决条件与后续

本轮无真实材料、付费/外发调用、真实模型测量、专业终评、生产切换或现场 UI 验证。未改 JS；Node 检查和既有页面测试属于完整离线检查，不描述为真实 UI 集成验收。

生产容量/拒绝或分批策略（PM，E12 前）、真实材料范围、实际模型权重/config/revision、外发及费用授权（E07/调用前）、生产整次超时（E14 前）、更高并发（E15 前）、量表/专业评审人（DOMAIN）、试点/回退窗口继续待确认。E14 同时依赖 E07/G2、E11/G3、E13；本次没有放行。下一步先确认容量并将真实领域图接入本事务/回执边界，再按 E05/E07 真实后端条件推进，而非直接用合成图替换正式入口。
