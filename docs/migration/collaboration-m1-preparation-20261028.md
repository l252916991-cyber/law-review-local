# M1 源库登记与副本演练执行单

- 日期：2026-10-28（Asia/Shanghai）。
- 前置：[G1 本地验收](collaboration-g1-execution-evidence-20261028.md)已完成，候选代码 `2b1e786`，目标 schema 26。
- 状态：**已完成路径候选核对及执行准备，尚未开始真实库备份、迁移或停写**。需数据维护者登记源库身份和停写窗口后执行。

## 已核对事实

原仓库的 `data/law_review.db`（7,163,904 字节）和 `data/langgraph_checkpoints.sqlite`（163,840 字节）存在，原仓库没有 `.env` 或 `.env.local`。当前代码默认从 `LAW_REVIEW_DATA_DIR` 定位业务库，checkpoint 的实际配置仍需核对运行服务的设置。

对这两个明确文件检查时未发现打开句柄；这不证明没有延迟任务、外部环境覆盖或随后启动的写入者，不能据此填写 `--quiescent`。本次仅检查文件存在性、大小、配置代码及文件占用，没有打开数据库、查看卷宗或触发 schema 迁移。

推荐使用**现有服务实际配置的旧库和 checkpoint 的停写快照**。须确认上述路径是目标源库还是开发副本，停写包括 API、worker、定时任务和其他写入者，并核对在途任务。独立工作树的默认 `data/` 不等于源库，不允许用新空库冒充迁移结果。

实际绝对路径、文件元数据、拟用新目录及未确认状态保存于忽略目录 `output/collaboration-readiness/m1-preparation-20261028/parameters.json`。四个拟用快照/恢复目录均尚不存在，不复用或覆盖既有目录。

## 登记后执行

填写获准源目录、实际 checkpoint、配置保管位置、停写窗口和回滚负责人。确认停写后，由已验证候选执行：

```sh
# 这些变量使用 parameters.json 中经核实的绝对路径。
uv run --locked python scripts/data_snapshot.py backup \
  --data-dir "$M1_SOURCE" --checkpoint-db "$M1_CHECKPOINT" \
  --output "$M1_SNAPSHOT" --quiescent
uv run --locked python scripts/data_snapshot.py verify "$M1_SNAPSHOT"
uv run --locked python scripts/data_snapshot.py restore "$M1_SNAPSHOT" \
  --output "$M1_MIGRATION_COPY"
```

以上是待执行命令，`--quiescent` 是实际停写后的声明。恢复进程以 `RESTORE_REPORT.json` 指定的目录和 checkpoint 启动，端口、身份、队列均独立，模型地址保持不可达。先记录源版本，再由候选完成迁移；核对完整性、外键、归属、原件摘要、任务及历史事件，并逐项说明预期变化。

迁移副本中验证旧批准记录边界和新报告冻结/批准/下载；再次停写备份该副本，恢复到第二个新目录，实际登录和操作，检查附件与冻结工件。回滚使用匹配旧程序及升级前快照，记录耗时与升级后新增数据的处置。仅完成上述演练后才能安排正式升级。

完整验收仍按[续行计划第 6 节](collaboration-g1-to-acceptance-plan-20261027.md#6-m1登记参数后进行副本迁移)执行；合成测试、路径核对和 G1 通过均不计作 M1 完成。
