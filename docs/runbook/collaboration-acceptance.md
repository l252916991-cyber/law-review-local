# 多人协作部署与运行验收

适用范围：一家律所内部、单组织、单实例部署的协同阅卷首版。本文给出可执行的验收步骤与判定口径；不构成对真实案件试点、法律质量或等保合规的结论。

配套工具：[`scripts/collaboration_acceptance.py`](../../scripts/collaboration_acceptance.py)（只读检查）与 [`scripts/data_snapshot.py`](../../scripts/data_snapshot.py)（停写备份/恢复）。

## 1. 验收前置

1. 在目标设备上完成安装，确认使用仓库固定的 Node 版本（`frontend/.node-version`）构建静态界面。
2. 以 `LAW_REVIEW_AUTH_MODE=token` 启动，配置至少一个环境管理员令牌（部署引导），或先由本机模式创建目录管理员。
3. 首次启动会执行数据库迁移到当前 schema；旧库升级前先做一次停写备份。
4. 备份演练期间必须停止 API 与 worker：工具会显式要求 `--quiescent`，不会在运行中悄悄备份。

## 2. 只读检查

```sh
uv run --locked python -m scripts.collaboration_acceptance --data-dir /path/to/data
```

可选参数：

| 参数 | 作用 |
|---|---|
| `--require-ocr` | 缺少 PDF/OCR 工具或 `chi_sim`/`eng` 语言包时判为失败（默认仅警告） |
| `--backup-drill DIR` | 在 `DIR` 下执行备份 → 校验 → 恢复 → 行数比对；`DIR` 必须不存在 |
| `--quiescent` | 确认 API/worker 已停止，允许备份演练 |
| `--probe-model URL` | 显式探测模型服务 `/models`，不发送案件数据；不传则不访问网络 |
| `--json` | 机器可读输出，便于值班记录归档 |

判定：`PASS` 通过；`WARN` 需要知悉但不阻断；`FAIL` 阻断验收。存在任一 `FAIL` 时脚本退出码为 1。

### 2.1 检查项

| 检查 | 失败条件（节选） | 说明 |
|---|---|---|
| `python` / `sqlite` / `fts5` | 版本不满足或 FTS5 缺失 | 检索与页索引依赖 FTS5 |
| `document_tools` / `ocr_languages` | `--require-ocr` 时缺少工具或中文语言包 | 扫描件导入需要 |
| `free_space` | 剩余空间 < 512 MiB | < 2 GiB 时警告 |
| `database` / `integrity` | 数据目录或完整性检查失败 | 只读 |
| `schema_version` / `migration` | 低于当前 schema | 先启动一次应用完成迁移 |
| `auth_mode` | 非 `local`/`token` | — |
| `token_strength` | 环境令牌短于 32 字符且未显式开启回环逃生口 | 不打印令牌本身 |
| `allowed_hosts` | 含 `*` | 防止任意 Host 访问 |
| `administrator` | 无环境管理员且无启用的目录管理员 | 否则无人能管理成员 |
| `member_directory` | 缺少成员表 | 旧库未迁移 |
| `revoked_access` | 停用成员仍持有有效令牌 | 正常流程会撤销 |
| `case_assignment` | 有启用成员未分配案件 | 仅警告，适合按需分配 |
| `overdue_tasks` | 存在逾期未结任务 | 仅警告 |
| `report_basis` | 已批准报告的基线与当前材料不一致 | 仅警告；导出会拒绝 |
| `backup_drill` / `restore_counts` | 备份失败、校验失败或恢复后行数不一致 | 阻断 |

## 3. 备份与恢复演练

```sh
# 停止 API 与 worker 后执行
uv run --locked python -m scripts.collaboration_acceptance \
  --data-dir /path/to/data --backup-drill /tmp/lexvault-drill --quiescent
```

脚本会：备份到 `DIR/backup` → 校验清单与哈希 → 恢复到 `DIR/restored` → 比对 `cases/documents/evidence/members/review_tasks/case_reports` 行数。任一失败即阻断验收。演练产物可保留为证据，但不得含真实案件材料对外流转。

恢复真实环境仍使用 `scripts/data_snapshot.py restore`，并在恢复后执行一次只读检查确认 `schema_version`、`integrity` 与行数。

## 4. 人工业务验收（五个身份）

用管理员、承办律师、复核律师、助理、只读各登录一次，按[计划书第 9 节](../../docs/migration/collaboration-first-release-plan-20261027.md#9-验收用例)执行：

1. 未授权人员无法查看、下载或导出案件。
2. 两人同时修改同一条证据，第二人得到冲突提示，内容不被覆盖。
3. 修改已确认证据或纠错原文后，相关证据退回待复核。
4. 任务可指派、退回（写原因）、完成，交接后责任明确。
5. 报告未批准不能正式导出；批准后材料变化，导出被拒绝。
6. 导出包内 `清单.json` 的记录数与哈希一致。
7. 备份后清空工作副本再恢复，案件、证据、附件可读。

## 5. 人员变动流程

| 场景 | 操作 | 结果 |
|---|---|---|
| 新同事入职 | 管理员在“成员与权限”创建成员，一次性令牌当面交付 | 成员按案件分工获得权限 |
| 转岗/加案件 | 案件总览“案件分工”调整角色 | 下一次请求按新角色执行 |
| 离职/停用 | 停用成员 | 令牌与会话立即失效，历史审计与任务保留 |
| 令牌泄露 | 轮换令牌 | 旧令牌立即失效 |
| 复核人变更 | 新复核人批准新报告版本 | 旧批准版本置为已作废 |

## 6. 运行提醒建议

当前版本提供 `/api/health`、`/api/ready`、结构化日志与 `security_events`，没有内置告警通道。建议至少监控：

- `/api/ready` 连续失败或 `legal_verification=unavailable`（法条语料缺失只降级核验）；
- 数据目录剩余空间低于 2 GiB；
- 备份任务超过计划周期未成功；
- 登录失败与 `security_events` 中 `denied` 的异常增长；
- 已批准报告变为“依据已变化”后长期未重新签发。

## 7. 未覆盖项

- 没有在线热备、异地复制、自动故障转移与多实例部署承诺。
- 没有邮件/短信令牌投递，令牌由管理员手工转交。
- 没有外部身份平台停用事件的实时同步；离职需在本系统停用成员。
- 模型服务连通性检查仅在显式传入 `--probe-model` 时执行。
