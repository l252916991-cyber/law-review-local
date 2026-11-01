# PostgreSQL 后端

同一套代码可以跑在 SQLite（默认，单机）或 PostgreSQL 上：设置 `LAW_REVIEW_DATABASE_URL` 就切换到 PostgreSQL，不设置仍用数据目录里的 SQLite 文件。全量离线测试在两种数据库上都跑；答案质量门禁在两种数据库上逐题结果相同。

## 配置

```sh
export LAW_REVIEW_DATABASE_URL="host=db.internal port=5432 dbname=lexvault user=lexvault password=..."
export LAW_REVIEW_DATA_DIR=/srv/lexvault/data   # 仍需要：上传原件、导出包、LangGraph checkpoint 放在这里
```

- 数据库需要 pgvector 扩展。应用启动时会执行 `CREATE EXTENSION IF NOT EXISTS vector`，没有权限时请 DBA 预先在该库里创建。
- 第一次启动按 `app/schema_postgres.sql` 建出最新结构（当前 schema 26），版本记在 `lexvault_schema_meta`。
- 已有库的版本比代码新，启动会拒绝（不降级）；比代码旧，也会拒绝并提示需要迁移，不会只建新表而让旧表缺列。PostgreSQL 的逐版本迁移还没有实现，下一次表结构变更时需要一并补上。
- URL 写法同 libpq，`postgresql://user:pass@host:5432/db?sslmode=require` 也可以。

## 同一套 SQL 怎么在两边都能跑

应用的约 490 处 SQL 都经过 `app.db.connect()`。PostgreSQL 下它返回 `app/db_postgres.py` 的 `PgConnection`，对外保持 sqlite3 的用法，在内部做这些转换：

| SQLite 写法 | PostgreSQL 下的处理 |
| --- | --- |
| `?` 占位符 | 改为 `%s`，字面量里的 `%` 转义 |
| `cursor.lastrowid` | 向有自增 `id` 的表插入时追加 `RETURNING id`；显式指定 `id` 的插入会把序列推到最大值之后 |
| `INSERT OR IGNORE` | `ON CONFLICT DO NOTHING` |
| `BEGIN IMMEDIATE`（单写者锁） | 事务级 advisory lock，跨连接、跨进程仍然串行 |
| 语句失败后事务仍可用 | 每条语句包在 savepoint 里，失败只回滚这一条 |
| `PRAGMA user_version / table_info` 等 | 从 `lexvault_schema_meta` 和 `information_schema` 回答 |
| `sqlite3.IntegrityError` 等异常 | psycopg 的完整性错误转成同名 sqlite3 异常，调用方不用改 |
| 结果类型 | `SUM` 等返回的 `numeric` 转回 `int`/`float`，布尔比较结果转回 `0`/`1` |

表结构由 `scripts/generate_postgres_schema.py` 从 SQLite 迁移结果自动翻译，CI 检查生成文件是否过期：改了 SQLite 迁移却没重新生成，构建会失败。全文检索表、它的触发器和 pgvector 列是手写部分，在同一个脚本里。

确实写不成通用 SQL 的两段检索查询放在 `app/search_backend.py`：

- **全文检索**：SQLite 用 FTS5 的 `MATCH` 和 `bm25()`；PostgreSQL 用 `tsvector` + GIN 索引和 `ts_rank`，文档名、类型、正文三列的权重比例与 FTS5 相同（1.6 : 1.2 : 1.0）。
- **页向量**：SQLite 把整案向量读进 Python 逐页算余弦；PostgreSQL 由 pgvector 在库内精确排序，只返回前 `limit` 页。兼容性检查和哈希回退的词项过滤两边一致。

## 与 SQLite 的差异

| 方面 | 差异 | 影响 |
| --- | --- | --- |
| 全文排序 | BM25 与 `ts_rank` 打分公式不同 | 门禁 60 题里 59 题回答与引用逐字相同；1 题两条引用的先后顺序不同，引用的页相同。门禁逐题通过情况一致 |
| 向量得分 | pgvector 用 float32 | 逐页得分与 Python 计算的差小于 1e-5，演示案件上排序相同 |
| 文本里的 NUL 字符 | PostgreSQL `TEXT` 不能保存 | 写入前去掉；SQLite 原样保存 |
| 时延 | 每次 `connect()` 新建连接，每条语句多两次 savepoint 往返 | 门禁规则模式单题 p50 约 43 ms（SQLite 约 15 ms），尚未加连接池 |

## 仍然只在 SQLite 上的功能

| 功能 | PostgreSQL 下的行为 | 原因 |
| --- | --- | --- |
| 隔离框架运行时（M2 sidecar、只读快照、宿主端口） | 明确拒绝，抛 `UnsupportedCapabilityError`（`app.db.SQLITE_FILE_ONLY`） | 它复制的是 SQLite 文件本身 |
| LangGraph checkpoint | 仍写数据目录里的 SQLite 文件 | 中断的运行只能在同一台机器上恢复 |
| 数据快照与恢复脚本（`scripts/data_snapshot.py`） | 设置了 `LAW_REVIEW_DATABASE_URL` 时拒绝执行 `backup`，避免只备份到上传文件而漏掉数据库 | 它复制的是 SQLite 文件；PostgreSQL 用 `pg_dump` 或数据库自身的备份方案 |
| 演示与容量造数、安装验收（`seed_demo_case.py`、`capacity_seed.py`、`collaboration_acceptance.py`、`validate_application_acceptance.py`） | 固定写 `--data-dir` 里的 SQLite 文件，忽略 `LAW_REVIEW_DATABASE_URL` | 防止把合成数据写进存有真实案件的库 |

**多实例尚未支持**：后台阅卷任务在 Web 进程内执行，Web 进程锁和 LangGraph checkpoint 都是本机文件。换成 PostgreSQL 解决的是数据库这一层（并发写、容量、备份工具、库内向量检索），多副本部署还需要把任务队列和 checkpoint 也移出进程，见企业级改进计划的阶段 1。

## 在 PostgreSQL 上跑测试

```sh
export LAW_REVIEW_TEST_POSTGRES_URL="host=127.0.0.1 port=5432 user=postgres dbname=postgres"
uv run --locked pytest tests -p pytest_socket --disable-socket --allow-unix-socket
uv run --locked python scripts/eval_gate.py --postgres "$LAW_REVIEW_TEST_POSTGRES_URL"
```

- 设置后，每个测试函数拿到一个新建的空库，每个 `IsolatedDatabaseTestCase` 测试类共用一个库；结束即删除。该账号需要建库权限。
- 测 SQLite 文件本身的用例标了 `@pytest.mark.sqlite_only(...)`、类属性 `sqlite_only` 或模块级 `SQLITE_ONLY`，在这种模式下仍跑 SQLite，并写明原因。
- 结束时会打印有多少个测试在执行过程中真的打开过 PostgreSQL 连接；设 `LAW_REVIEW_TEST_POSTGRES_REPORT=<文件>` 可输出明细。
- `tests/test_postgres_backend.py` 专测这一层：SQL 翻译、savepoint 语义、写锁串行、全文索引触发器、向量结果两库一致、旧版本库拒绝启动、门禁在 PostgreSQL 上通过。

CI 的 `postgres` 任务用 Ubuntu 软件源里的 PostgreSQL 16 和 pgvector 0.6.0 跑同一套测试和门禁。向量检索的性能数据见 [PostgreSQL 页向量检索基准](../benchmarks/postgres-vector-search-20261102.md)。
