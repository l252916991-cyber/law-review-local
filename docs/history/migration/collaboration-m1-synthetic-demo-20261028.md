# M1 合成迁移演练与求职展示记录

- 日期：2026-10-28（Asia/Shanghai）。
- 本轮目标：按用户“你自己决定，对求职有帮助”的指示，将下一步落为可复现、可展示的迁移恢复能力。
- 结果：**合成迁移与恢复演练通过；真实律所 M1/U1 仍未执行**。没有打开、备份、升级原仓库的真实候选库，也没有安排生产停写。
- 演练代码：`2916b2e7fdf468477edf65f135c1fb8a01af14b9`，在提交后的干净工作区运行；旧版固定为 `092ecacf56621251184035f6aa05b03f5bbe5b6c`。
- [复现命令与面试讲解](../../runbook/migration-demo.md)；驱动入口为 `scripts/migration_demo.py`。

## 最终运行

```sh
uv run --locked python -m scripts.migration_demo \
  --output output/collaboration-readiness/m1-portfolio-demo-20261028-final
```

| 检查 | 实测结果 |
|---|---|
| 版本 | 历史程序创建 schema 16，新 API 实际迁移到 schema 26 |
| 指定旧业务列保留 | 1 个案件、6 份材料、15 页原文、5 条证据、1 次 Agent 运行，升级前后指定列相等；具体列见驱动 `summary()` |
| 外部 checkpoint | 9 条 checkpoints；完整逻辑转储摘要在升级副本、二次恢复、回退副本中一致 |
| 新版交付 | 实际 HTTP 登录；管理员创建案件 #2、复核成员 #1 和材料 #7；复核人可批准但不可新建报告；报告 #1 v1 冻结后独立批准并下载 |
| 二次恢复 | 恢复实例再次加载当前工作树 `app/main.py`；指定业务列一致，冻结报告可预览和正式下载；恢复报告记载 2 处受控路径重定位 |
| 正式包 | 冻结版本 #1、正文摘要一致；4 个非清单成员的字节摘要全部相同，附件和流水工件的清单摘要核对通过 |
| 回退 | 升级前快照恢复后由历史 `init_db(seed=False)` 打开，仍为 schema 16，指定业务列与 checkpoint 不变 |
| 源保护 | 原合成业务库和外部 checkpoint 的文件摘要未变；两个临时 API 进程均正常关闭 |
| 耗时 | 总计 6.09 秒，仅代表本机这个小样本完整演练，不能推算生产恢复时限 |

本次正文 SHA-256 为 `4de56b7de94a9cf647ed26d83efcbd5f55577b8ad6c867174f44d77e31540e70`；checkpoint 逻辑转储 SHA-256 为 `add0ae55bdef220e7d139ebb642e4bb2e797f436331b418b63807be4c683737c`。各次运行有随机身份、时间戳及运行标识，不要求不同演练之间摘要相同，只比较同次演练的前后状态。

## 验证与证据绑定

- Python 3.14.6，当前 `uv.lock` 依赖；历史源码使用同一解释器和当前依赖，未重建历史依赖环境。
- 受影响离线测试：`scripts/test_engineering.py`、`scripts/test_data_snapshot.py`、`scripts/test_snapshot_runtime.py`、`tests/test_report_frozen_delivery.py`、`tests/test_case_reports.py`，禁用网络运行，**43 passed，13 个子用例通过**。新增测试检查拒绝复用已有目录、交付报告/正文/冻结版本不符及附件/工件摘要损坏。
- `ruff check app tests scripts`、`mypy`（54 个脚本）、新脚本 `compileall` 通过；本地提交钩子通过。
- 本轮没有修改产品 API、schema、依赖或前端，没有重跑完整 CI、前端、容器及模型基准。此前 G1 的双版本完整离线检查和候选包仍绑定[原 G1 记录](collaboration-g1-execution-evidence-20261028.md)中的版本，不冒称本轮新提交已有新的完整 CI 或发布包。

运行产物根目录为忽略路径 `output/collaboration-readiness/m1-portfolio-demo-20261028-final/`，以下 SHA-256 用于核对本次本地产物：

| 产物 | SHA-256 |
|---|---|
| 驱动脚本 | `ee27fc9de9d6781c812c95edc3b42a09f407bbd6ffd9a05d23fd64475df26cd4` |
| `result.json` | `99296f7e7c03bb86515bb4bceaf2feb8a9c22183e9795d1229219b2ceb10a575` |
| 恢复前 `synthetic-export/delivery.zip` | `526e1709cdec9ab24d27d28430009a07e76afc8cd81c5e48738de51e544d822b` |
| 恢复后 `restored-formal-export.zip` | `21d330ae07513855a9c176e11003f886b17f06d58d33d4765385089bfa412d0e` |

ZIP 摘要不同来自清单生成时间及 ZIP 元数据变化，非清单成员逐一比较一致。`before-snapshot`、`after-snapshot`、三个恢复目录、真实进程日志和验收日志均保留在同一输出目录。失败尝试保留于同级初次运行、`run2` 和 `run4` 目录，分别暴露旧程序直调缺身份、报告状态枚举和校验列名错误；修正演练驱动后重新运行，没有修改产品断言或旧库版本号。

## 后续边界

求职展示可以使用本记录与复现入口，讲清迁移、数据保护、签发一致性和故障恢复的设计取舍。真实库停写迁移、v13 兼容、旧批准报告迁移、跨版本 checkpoint 续跑、五身份目标设备人工验收与真实模型业务效果尚无本轮证据，继续按[原 M1 执行单](collaboration-m1-preparation-20261028.md)及[U1 计划](collaboration-g1-to-acceptance-plan-20261027.md#7-u1律所实际验收)另行执行。
