# E12/E13 生产全范围验收准备

日期：2026-10-24（北京时间）。编制基线 `c14b93d`，承接[稳定性实施记录](g2-stability-execution-20261024.md)、[领域实施记录](domain-integration-execution-20261023.md)和[总执行计划](execution-plan-20261022.md)。本文件交付验收设计和证据清单；生产全范围验收尚未执行。

执行补充：本次公开真实案例的成功/业务拒绝结果完成新进程复用检查，并据此修复历史成功在 deadline 后被误拒的宿主边界；见 [S07 记录](g2-public-development-execution-20261024.md)第 5 节。该证据属于获准的开发验收副本，不改变目标生产全范围状态。

## 1. 当前实现与进入条件

`app/candidate_domain.py::CandidateDeployment` 只接受新建的 acceptance 数据库，固定部署版本、数据库文件身份、案件范围和容量；它没有收养现有库或开启生产的开关。`DomainHostWritePort` 已有领域来源校验、六类动作和持久回执。`IsolatedHostIntegration` 已有稳定映射、框架状态查询、投影、准入与 worker lease，但未接正式 `review_jobs` 或 HTTP 后台路由。

公开真实案例的只读开发对照只覆盖相应材料和隔离库。既有演示库为 schema v13，本次仅作只读清点；升级和写入验收需在新副本执行。当前应用 schema 版本与迁移步骤以 `app/db.py` 的最终实现为准，不沿用旧部署手册的 v6→v7 示例作本批验收。

| 进入条件 | 要登记的实际值 | 当前状态 |
| --- | --- | --- |
| 目标部署 | OS、Python、独立框架解释器、Web/worker 启动命令、持久目录、模型端点和服务归属 | 待确定目标环境；本机隔离候选只作工程依据 |
| 数据副本 | 获准源路径、案件范围、停写或一致性快照方式、原件及数据库哈希、恢复位置 | 网上公开案例已获本次使用授权；目标业务库范围尚未登记 |
| 六类写入范围 | 主体、案件、动作权限、业务审批规则、保留和删除规则 | 需按实际部署登记；不由请求参数声明权限 |
| 容量与并发 | 每案 ≤500 页且正文 ≤200 万字符、超限拒绝；单 Web、≤2 后台任务、准入 4 | 容量已确认；目标部署上的实际边界待验收 |
| 绝对超时 | 生产整次运行上限，含排队、审批等待、恢复和清理责任 | PM 在 E14 前确认；现有候选 300 秒不视为生产确认 |
| 责任与结论 | 执行人、复核人、适用范围、通过/失败/未知、原始证据路径 | Codex 可实现、自审和自动验证；独立复核及业务评审须据实登记 |

## 2. 实施顺序

1. 冻结目标部署配置、源库及附件身份，恢复至全新私有目录。源库始终保留；升级前后保存完整性、外键、表/列、行数和附件哈希对照，记录不可兼容版本的明确拒绝。
2. 设计由宿主受信配置构建的部署适配，绑定库、案件、版本、权限和预算；独立于只接受新验收库的 `CandidateDeployment` 工厂。不得通过删除 acceptance 校验或重标记现有库进入生产。
3. 在获准副本验证写入事务、幂等键、回执查询及来源核验，逐类记录效果与回执，保护草稿和业务审批语义。
4. 将实际领域图的 request/job/host run/framework run 关联接入拟部署的队列与 worker；框架继续负责调度、join、审批和恢复，宿主只投影、查询与对账。
5. 以独立进程实施故障、重启、取消、权限变化和生命周期矩阵；每个故障使用新身份和专属目录，原 unknown 不通过新运行目录“清零”。
6. 对最终源码完成受影响验证及适用完整离线检查，输出 E12 与 E13 两份范围明确的报告。E14 另外核验 G2、适用 G3、E13（含 E12）和生产绝对超时，再接正式入口。

## 3. 动作与故障验收矩阵

下表列出可复用的现有测试依据，均不等于目标生产部署已经通过。执行时每行填入独立证据、实际结果和复核人。

| 编号 | 验收场景与实际断言 | 现有工程依据 |
| --- | --- | --- |
| P01 | `run_record`：同请求只有一个宿主 run、会话与框架映射；受理响应丢失后查询原关联 | `tests/test_host_integration.py::test_concurrent_request_single_association` |
| P02 | `step_record`：run/node 唯一；效果与回执同事务，插入回执失败不留半个步骤 | `tests/test_host_writes.py::test_atomic_rollback_when_receipt_insert_fails` |
| P03 | `gap_detection`：仅引用同案证据、保持待核验；失败或拒绝不产生成功结果 | `tests/test_domain_integration.py::test_missing_sources_fail_without_success_message_or_draft` |
| P04 | `memory_draft`：关闭保存时零草稿；开启时为 `runtime_draft`，不作为已确认事实召回 | `tests/test_host_writes.py::test_draft_is_never_recalled_as_validated_fact`、`tests/test_host_integration.py::test_optional_draft_disabled` |
| P05 | `conversation_message`：真实领域非空引用映射到同案原页；恢复不重复消息，拒绝不写成功回答 | `tests/test_domain_integration.py::test_domain_roundtrip_restores_outputs_and_projects_real_citations` |
| P06 | `audit_entry`：绑定当前主体和案件；同动作重复提交只复用原回执，换参数拒绝 | `tests/test_host_writes.py::test_six_actions_atomic_idempotent_and_query`、`test_each_action_conflict_and_extra_field_rejected` |
| P07 | 写入前、提交后丢回执、journal 后未 checkpoint 等窗口：先查询原回执；无回执不足以证明未执行；未知不得重发 | `tests/test_host_integration.py::test_sigkill_persistent_boundary_recovery`、`test_precommit_sigkill_rolls_back_and_stays_unknown` |
| P08 | worker/宿主重启后复用已知模型结果；成功与拒绝分别验证，调用账本、效果、回执数量不增加 | `tests/test_candidate_domain.py::test_candidate_new_process_keeps_scope_identity_and_no_replay` |
| P09 | 乱序、重复、缺失事件：连续游标、去重、终态不回退；缺事件保持准入责任 | `tests/test_host_integration.py::test_event_reordering_dedup_and_terminal_monotonicity`、`test_missing_events_do_not_release_admission` |
| P10 | 队列满明确拒绝；仅两 worker 持有执行 lease；并发恢复同一请求只允许一个执行者，终态释放一次 | `tests/test_host_integration.py::test_persistent_admission_and_single_release`、`test_two_worker_leases_and_same_request_lock` |
| P11 | 审批后参数/资源变化拒绝；取消不再派发新效果；排队、审批及重启不刷新原绝对 deadline | `tests/test_host_integration.py::test_approval_cannot_cross_changed_action`、`test_cancel_running_stops_new_effects`、`test_original_absolute_deadline_never_renewed` |
| P12 | 撤权、源变化、归档和删除：恢复重新授权；关联数据按声明清理，活跃 worker 的状态不误删，清理重跑幂等 | `tests/test_candidate_domain.py::test_candidate_revocation_archive_and_deletion`、`tests/test_host_integration.py::test_delete_cascade_and_framework_state_erasure` |

## 4. 报告与退出要求

每份报告记录源码提交与内容摘要、wheel/锁、模型与服务配置、源/副本身份、数据库迁移、运行身份、矩阵逐项证据及所有失败/unknown。不能把一项成功覆盖其他动作或部署条件。

E12：六类适用效果与回执一致；重复动作只生效一次，参数冲突拒绝，权限/来源/审批语义不变。E13：映射、状态投影、重启、取消、审批、队列/配额、生命周期和未知对账在目标环境均按契约收敛；没有第二套调度器。

权限泄漏、未批准/重复副作用、虚假完成和引用身份错误任一失败即阻断。证据缺失记未执行，原始 unknown 保留待对账。E12/E13 通过不替代 E07/G2、E15 实际页面流程或 E17 专业终评。

备份和回退演练继续按 E18 进行，不在现行业务库注入故障。正式切换继续沿 E19/E20 的批准范围和观察门槛执行。
