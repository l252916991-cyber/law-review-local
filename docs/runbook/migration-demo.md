# 求职展示：合成旧库迁移与交付恢复

这个演练用于展示一条可以重新执行的工程闭环：历史程序建库 → 停写快照 → 新版本升级 → 双身份复核签发 → 再次备份恢复 → 正式下载 → 升级前快照回退。全部材料来自内置合成样例，不需要律所卷宗、真实模型或外部账号。

## 运行

在包含历史提交 `092ecacf56621251184035f6aa05b03f5bbe5b6c` 的完整 Git 工作副本中执行。需要当前锁定依赖；源码 ZIP 或缺少该提交的浅克隆不能生成这个历史基线。正式界面可按项目说明预先构建到 `frontend/out/`；本脚本验证 HTTP 流程，不执行浏览器验收。

```sh
uv sync --locked --group dev
uv run --locked python -m scripts.ops.migration_demo \
  --output output/migration-demo-first-run
```

`--output` 必须是新目录。重复演练换一个目录名，已有目录会被拒绝，不覆盖失败证据。成功时终端与 `result.json` 均显示 `stage: passed`。脚本只创建自己的合成源库，不接收现有数据库路径。API 绑定随机回环端口，使用临时随机令牌，模型和队列地址指向不可达回环端口；子进程退出后才做停写快照。失败时也清理服务进程，保留输出供排查。

## 实际验证内容

| 步骤 | 断言与证据 |
|---|---|
| 旧程序建库 | 从固定 Git 提交提取 `app/`，运行真实初始化函数，得到 schema 16；上传合成 TXT，运行一次禁用 LLM 的 LangGraph |
| 升级前保护 | 记录源业务库与外部 checkpoint 字节摘要；真实 backup/verify/restore，将外部 checkpoint 纳入快照 |
| 升级 | 启动当前代码的真实 API；日志记录加载路径；schema 升至当前 `SCHEMA_VERSION`，检查完整性与外键 |
| 旧内容核对 | 比较案件编号/描述、材料归属/名称/摘要、页原文、证据标题/引文/状态、运行状态和 checkpoint 线程标识等指定列；核对 checkpoint 的行数与完整逻辑转储摘要 |
| 新版交付 | 调用 `release_smoke`，实际登录、创建成员和案件、验证复核权限、上传、冻结、独立复核人批准、正式下载 |
| 再次恢复 | 停服务后备份升级副本，恢复到第二个新目录，再启动当前 API；原冻结报告可预览并正式下载，批准版本/正文摘要与恢复前一致 |
| 包内校验 | 核对附件和流水工件的清单摘要，比较恢复前后所有非清单成员的字节摘要；清单生成时间和 ZIP 元数据不要求相同 |
| 回退演练 | 将升级前快照恢复到第三个新目录，用旧程序 `init_db(seed=False)` 打开，仍为 schema 16；指定业务列和 checkpoint 与升级前一致；原合成源库及原 checkpoint 字节摘要未变 |

这是小规模、合成数据的兼容性与恢复演练。历史代码使用当前锁定依赖运行；未重建历史依赖环境，未验证 v13 迁移、真实旧批准报告兼容、恢复后的跨版本 LangGraph 续跑、真实模型效果或律所生产停写。另有 `tests/test_snapshot_runtime.py` 测试当前版本的中断续跑，它不等于本演练覆盖了跨版本续跑。总耗时包含建样本和验收，不能当作生产 RTO。

## 看哪些产物

- `result.json`：基线与候选提交、驱动脚本摘要、源/目标 schema、保留记录数、checkpoint 摘要、恢复报告与耗时。
- `seed.log`、`migration-server.log`、`restored-server.log`、`release-smoke.log`、`rollback.log`：真实进程与 HTTP 操作证据。
- `before-snapshot/manifest.json`、`after-snapshot/manifest.json` 及各恢复目录的 `RESTORE_REPORT.json`：快照成员、完整性、路径重定位与恢复核对。
- `synthetic-export/delivery.zip`、`restored-formal-export.zip`：恢复前后正式交付包，可解压查看 `清单.json`。

全部运行产物位于忽略目录。提交代码和说明即可；面试展示时可现场执行或提供脱离令牌的合成结果摘要。

## 面试讲解顺序（约五分钟）

1. 说明问题：同一律所多人处理案件，更新不能覆盖他人修改，复核通过的报告必须绑定固定内容，升级后还要能交付历史冻结版本。
2. 展示 `result.json` 的提交、schema 与保留记录数，打开两份交付包，展示相同报告版本、正文和附件摘要。
3. 打开 `release-smoke.log`，说明承办编制、复核批准的权限边界；再展示前后快照和回退目录。
4. 对照源码解释为什么报告冻结文件要和数据库一起备份，为什么恢复会重定位路径，为什么回退使用升级前快照而不修改版本号。

可用的项目经历表述：

> 为本地法律阅卷系统实现可复现的旧库迁移与备份恢复演练：以固定历史提交生成合成数据，验证 SQLite schema 16→26 升级、外部 LangGraph checkpoint 保留、双身份报告签发及恢复后冻结正文与附件一致性，并提供旧程序回退检查与自动化校验。

具体运行数字与验证版本见[2026-10-28 演练记录](../history/migration/collaboration-m1-synthetic-demo-20261028.md)。
