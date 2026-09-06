# 发布与数据保护

## 不破坏当前目录的发布方式

```bash
uv run python scripts/package_release.py --dry-run
uv run python scripts/package_release.py --output dist/lexvault-source.zip
```

脚本仅打包明确允许的源码、配置模板、测试和维护文档，拒绝符号链接；不覆盖已存在的 zip，并记录每个成员的 SHA-256。默认不包含模型、案件、checkpoint、导出、凭证、日志、历史评测原始答案或 LawBench。需要随包运行 LawBench 测试时，核对 `benchmarks/lawbench/SOURCE.md` 与原始数据集许可证，再显式增加 `--with-benchmarks`。

`.gitignore` 不是发布权限检查，也不会清除已经跟踪的数据。首次 git 提交前检查暂存区，切勿无检查地执行 `git add .`；发布包仍须人工检查源码/文档中是否有手工粘贴的案件片段。不要把“文件不在 data 目录”当作已经脱敏。

## 单进程运行与升级边界

Web 启动在迁移及中断任务恢复之前获取数据库旁的 `law_review.web.lock` 排他锁，持有到后台阅卷执行器停止。第二个 Web 进程会拒绝启动，不会把第一个进程的运行标记中断。部署必须使用单个 Uvicorn worker 和本机文件系统；此锁不是多实例租约，也不适用于网络共享存储。不要删除或替换运行中的锁文件；崩溃后操作系统自动释放文件锁。独立 arq worker 不受 Web 锁管理，维护备份仍须分别停写。

数据库按 `PRAGMA user_version` 有序升级，迁移 DDL 和版本号在同一事务提交，失败整体回滚；新于当前程序支持的版本拒绝启动，不自动降级。升级前完成下述停写备份；应用回滚若遇到较新数据库，应恢复匹配版本的备份到新目录，不能手工调低版本号。

浏览器登录签发独立随机会话，不在 cookie 中保存配置访问令牌。服务端以哈希保存会话标识及凭证指纹，8 小时到期、退出立即撤销，进程重启全部会话失效；每次请求重新解析当前授权，删除或轮换配置令牌即拒绝旧会话。会话仅存在本机进程内（上限 4096），不是集中身份服务；Bearer API 令牌仍由部署配置管理，应通过安全配置更新执行轮换，不能用浏览器退出代替撤销 API 凭证。

## 备份与恢复验收

业务 SQLite、独立 LangGraph checkpoint、uploads 和必要配置属于一个恢复单元。为获得一致快照，先停止 Web 新写入与 worker 消费，等待在途任务结束；使用 SQLite backup API 或数据库提供的备份命令，不直接复制正在写入的主文件而遗漏 WAL。

备份先写到独立、受控且加密的位置；在新的私有数据目录恢复，检查 `PRAGMA integrity_check`、文档数量/文件 hash、证据归属、历史轨迹及一次失败节点恢复。验证成功前不要删除原件。保留策略应由案件负责人确定；不默认定期清除审计或 checkpoint。

提供独立备份工具：

```bash
# 先停止 API/worker 并排空队列；路径为维护者选择的私有目录。
uv run python scripts/data_snapshot.py backup \
  --data-dir /absolute/private-data --output /absolute/backups/snapshot-001 --quiescent
uv run python scripts/data_snapshot.py verify /absolute/backups/snapshot-001
uv run python scripts/data_snapshot.py restore /absolute/backups/snapshot-001 \
  --output /absolute/new-restored-data
```

checkpoint 不在默认数据目录时，备份必须显式提供 `--checkpoint-db /absolute/checkpoints.sqlite`。恢复后业务库位于新目录，checkpoint 统一为新目录下 `langgraph_checkpoints.sqlite`；修改 `LAW_REVIEW_DATA_DIR` 和 `LAW_REVIEW_LANGGRAPH_CHECKPOINT_DB`，不要继续指向旧库。

工具使用 SQLite backup API，以只读连接读取原数据库，验证完整性、SHA-256 与文件清单；拒绝活动 run/batch/job、符号链接、缺失文档及已存在的恢复目标。复制期间用 SQLite `data_version` 和文件清单检查变化，但这不是在线跨库原子快照，`--quiescent` 是必须遵守的停写前提。SQLite 只读连接可能更新 WAL 的共享内存读标记，不改业务记录。

恢复仅重写 `documents.stored_path` 与 `batch_import_files.stored_path` 到新目录，保留已清理 staging 文件的历史引用、checkpoint 与审计内容。Redis 队列、系统密钥和 `.env` 不在备份中；需要单独按密钥管理策略备份。校验 hash 是防损坏，不是抵御恶意修改的数字签名，只从可信私有备份恢复。

中途失败会保留带 `INCOMPLETE`/`RESTORE_INCOMPLETE` 标识的目录供排查，不能启动该副本，也不会自动删除或覆盖它。新一次尝试须使用新的目标目录。

可重复的无模型恢复演练（只使用临时合成数据）：

```bash
uv run python -m pytest scripts/test_data_snapshot.py scripts/test_snapshot_runtime.py \
  --disable-socket --allow-unix-socket -v
```

演练包含：原件丢失后恢复、篡改和路径穿越拒绝、批量文件路径迁移、自定义 checkpoint，以及真实 LangGraph 在 Memory 首次失败后恢复到新数据库，断言 Critic 不重复运行。

## 发布门槛

1. 离线测试、关键 lint、类型范围内检查与覆盖率达标；记录具体失败而非改低门槛遮蔽。
2. Python 锁文件可在干净环境安装且 `uv pip check` 通过；OCR 系统依赖独立验证。
3. 多用户部署通过身份/案件授权与跨案件访问测试；不能只使用 `user_name`。
4. 完成独立目录备份恢复演练；确认日志、导出、长期记忆和 checkpoint 保留/删除政策。
5. 维护者明确本项目许可证及第三方内容再分发条件；不自动上传、推送或创建公开仓库。
