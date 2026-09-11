# LexVault：本地优先法律卷宗智能阅卷系统

![CI](https://github.com/l252916991-cyber/law-review-local/actions/workflows/test.yml/badge.svg) ![Supply-chain scans](https://github.com/l252916991-cyber/law-review-local/actions/workflows/security.yml/badge.svg)

FastAPI + SQLite 的私有化法律阅卷工作台：多格式卷宗导入、页级混合检索、证据管理与审批、可审计的双运行时 Agent 分析。核心设计是**证据优先**——法律检索结果必须能追溯到受控法条语料与案件原文页，法条引用在无法精确核验时拒绝作答，而不是用相近条文替代。**AI 输出仅辅助审阅，不能代替律师判断；系统不自动出具法律结论。**

## 1. 项目定位

| 适合 | 不适合（当前版本边界） |
|---|---|
| 单组织、单实例、受控设备的本地/内网部署 | 多实例高并发、多租户 SaaS |
| 有明确使用授权的卷宗数字化与证据整理 | 对外提供法律意见或法条依据（法条引用功能未达验收） |
| 律师主导、AI 辅助的人机协同流程 | 无人复核的全自动决策 |

当前版本 **v0.2.0**（2026-09-07）。发布序列：`v0.1.0` 工程基线 → `v0.1.1` 首个 CI 全绿 → `v0.2.0` 治理批次（权限矩阵、OIDC、证据链、审计事件、容器化、供应链扫描）。版本历史与验收证据见[企业化改进报告](docs/runbook/enterprise-improvement-report.md)第 10–11 节。

### 1.1 仓库结构

```text
app/          产品代码：API、Agent 双运行时、检索、语料、权限与审计
tests/        离线自动化测试（CI 禁网运行）
benchmarks/   评测数据集与历史实验产物（LawBench 上游数据、结果 JSON）
scripts/      发布、运维与维护工具（环境自检、备份、语料构建、发布打包）
docs/         架构说明、运维手册与工程记录

unified_benchmark_runner.py   模型评测公开入口
rag_project_benchmark.py      项目 RAG 页级检索回归入口
verify_benchmark_run.py       评测产物完整性与评分复算校验
compare_agent_runtimes.py     双运行时对比入口
BENCHMARK_TEST_PLAN.md        评测验证计划
```

评测**入口脚本有意保留在根目录**，由 `.gitignore` 白名单与[发布白名单](scripts/package_release.py)显式维护，使公开发布入口一眼可见；评测**数据与产物**则归入 `benchmarks/`，避免把可执行代码混入评测资产树。这是设计结果，不是历史残留。

`data/`、`models/`、`output/` 是本地运行边界（数据库、上传、导出、模型权重与实验产物），永不入库。

## 2. 能力总览

| 领域 | 能力 | 明确边界 |
|---|---|---|
| 卷宗导入 | PDF/DOCX/TXT/图片；扫描件本机 Tesseract OCR；页级存储 | 解析预算：单文件 50MiB、PDF ≤500 页、提取文本 ≤5M 字符（超限明确截断标注） |
| 检索 | SQLite FTS5/BM25 + 法律词项 + 本地向量，RRF 融合；页级引用溯源 | 向量服务不可用时确定性降级并标注 |
| 法条核验 | 显式法名/条号在受控法条语料中精确核验，附版本、来源与正文；无法唯一核验时拒答并标记待核验 | 法条语料为受控部署资产（非入库数据）；语料标注不保证现行有效性，有效性仍需律师核验 |
| 证据治理 | 证据/关系/标注 CRUD；原件内容哈希与同案件去重；审批归属，确认内容被编辑后审批自动失效 | 并发乐观锁、批量操作、软删除尚未实现 |
| 问答与 Agent | 检索路由问答；原生 DAG（默认）与 LangGraph（可选）双运行时，失败续跑、后台任务轮询 | 长期记忆是草稿性质，不具已确认事实地位 |
| 身份与权限 | `local`（仅回环）/ `token`（Bearer + 逐主体权限矩阵）/ 可选 OIDC 组织登录；服务端不透明会话（8 小时过期、注销吊销） | 非完整组织级 IAM；OIDC 真实 issuer 连通属部署验收项 |
| 审计与可观测 | 业务审计、独立 `security_events`（不随案件删除消失）、请求关联 ID、结构化 JSON 日志 | 外部独立审计存储（防管理员篡改）待建设 |
| 导出 | 案件包导出附 `清单.json`（哈希与审批状态），证据标题防公式注入 | 导出为人工授权动作，不自动外发 |
| 部署 | 非 root Docker 镜像与 compose（web/worker/redis）；`scripts/data_snapshot.py` 停写备份与恢复 | 未做在线热备、异地复制与自动故障转移 |
| 供应链 | 依赖锁定带哈希、每周 pip-audit + gitleaks 扫描、发布白名单拒绝符号链接 | 扫描为周检，不替代发版前人工审查 |

## 3. RAG 检索策略

LexVault 的检索目标不是单纯提高召回率，而是在法律场景下同时保证四件事：检索结果与案件原文页可对应；法条引用可在受控语料中定位与核验；法律依据与案件事实走不同的检索边界；检索不足时系统不把不确定内容包装成确定结论。

原则是**证据优先**：先确认能取到足够支撑结论的法律依据或卷宗证据，再组织答案。检索按问题性质**分派到不同路径**，而不是把所有资料混入同一个无差别向量库后在末端统一裁剪：

| 路由（`detect_route`） | 检索路径 | 生成 |
|---|---|---|
| 目录统计 | 确定性数据库元数据查询 | 不经模型，直接汇总 |
| 事实检索 / 多文档对比 / 知识库+卷宗 | 案件卷宗页级混合检索 | 模型（失败回退规则式回答） |

法条核验是**独立的分析节点**：阅卷编排在问题含法律依据信号（法律、法规、构成要件、规定、法条）时，把「法条核验 Agent」追加进分析计划，与事实、证据、矛盾节点**并列**运行，不汇入案件事实问答的上下文；其核验结果独立呈现并标注待核验。

### 3.1 法条检索与引用安全边界

法律条文与普通知识文档不同：词面相似的段落不构成已核验的法律引用。因此法条侧采用**偏向 fail-closed** 的策略（`app/statutory_retrieval.py`）：

- 问题中显式写出的法名与条号（如《工伤保险条例》第十四条）被解析后，在受控法条语料中做**精确查找**；只有「唯一法名 + 唯一条号 + 语料命中」同时成立，状态才是 `ok`。
- **纯文本检索永远不返回 `ok`**，只返回 `candidate`——语义相近不等于引用成立。
- 显式引用无法唯一解析时（含两部不同法律共用同一简称、条号不存在）返回 `needs_review`，**绝不以相近法条替代**用户所指。
- 同一部法存在多个版本而未指定适用版本时拒绝核验，要求明确版本。
- 语料未配置或完整性校验失败时同样返回 `needs_review`，并在就绪探针 `/api/ready` 报 `legal_verification: unavailable`；语料损坏只降级法条核验，不阻断案件工作区启动。

每条结果都附带 `law_name`、`article_number`、`version_date`、`version_status`、`source_url` 与正文，供律师回看来源。需要明确的是：语料的 `version_status` 标注为 **currentness_not_asserted**——系统保证的是「在受控语料中精确、唯一地定位到某版本条文并标注版本与来源」，**不保证**引用的一定是现行有效法条，有效性判断仍需律师核验。

### 3.2 卷宗证据检索

案件材料走页级混合检索（`app/rag.py`），服务于案件事实、证据与文档内容：

- 关键词通道为 FTS5/BM25 与法律词项 bigram，向量通道为本地 embedding，两路以 RRF 融合（`k=60`，通道权重随查询画像与向量后端自适应）。
- 确定性 hashed 向量后端只作召回通道并把权重压到 0.25，避免其越过精确法律词项、金额或文件名的匹配。
- 命中页扩展相邻页候选后再做候选级重排；向量服务不可用时确定性降级，并在检索指标中标注。
- 回答中的引用卡片与上下文一一对应，可点击回溯到原文页。

### 3.3 生成与引用校验

生成阶段受检索结果约束：模型回答必须通过引用校验（`validate_review_answer`）才会被采纳，否则该次作答判为失败并回退规则式回答，同时记录 `citation_check` 状态。

fail-closed 的适用范围需要如实区分：**法条引用不可核验时会拒答并标记待核验**；**案件卷宗检索不足时不会拒答**，而是降级为基于已检索材料的规则式回答，并提示律师补充材料。

## 4. 安全边界

- **数据本地性**：模型服务默认仅允许本机回环地址；改为远端必须显式设置 `LAW_REVIEW_ALLOW_REMOTE_MODELS=1` 并使用 https，HTTP 重定向一律拒绝。批准后提示词与证据才会发往该端点。
- **访问控制**：默认 `local` 模式仅接受回环连接并拒绝代理头；对外提供服务必须切换 `token` 模式并配置允许 Host 与 TLS。跨站写入、错误 Host、路径逃逸、上传活动内容均被拦截。
- **失败关闭**：鉴权配置错误返回 503 拒绝服务；OIDC 部分配置等同禁用；未来 schema 的数据库拒绝启动。
- **输入预算**：上传总量/单文件/文件数受限；恶意文档按页数、解压与提取文本上限隔离处理。

安全配置细则见[运维手册](docs/runbook/operations.md)，组件来源与许可见[许可清单](docs/runbook/licenses.md)。

## 5. 环境要求

| 项 | 要求 |
|---|---|
| Python | 3.11–3.14（CI 在 Ubuntu 上验证 3.11 与 3.14 两端） |
| 操作系统 | macOS（开发验证）、Ubuntu 24.04（CI 与容器）；Windows 未验证 |
| 数据库 | SQLite ≥3.34（需 FTS5） |
| 可选服务 | Redis 7（仅批量导入）、本地 LLM 服务（OpenAI 兼容接口）、本地 embedding 服务 |
| OCR | poppler + tesseract（含中文语言包；仅处理 TXT/DOCX 时不需要） |
| 资源 | 内存 16GB+（模型加载）、磁盘 10GB+（含检查点与索引） |

## 6. 部署

### 6.1 开发/单机运行

```bash
python3 -m pip install uv==0.12.0
uv sync --locked --group dev
uv run python scripts/doctor.py            # 环境自检
uv run uvicorn app.main:app --host 127.0.0.1 --port 8765
```

打开 <http://127.0.0.1:8765>。没有模型时自动使用规则模式（回答显式声明未用模型）。配置参考 [.env.example](.env.example)——应用不隐式加载 dotenv，需显式注入环境并重启服务。

OCR 依赖：

```bash
# macOS
brew install poppler tesseract tesseract-lang
# Debian/Ubuntu
sudo apt-get install poppler-utils tesseract-ocr tesseract-ocr-chi-sim
uv run python scripts/doctor.py --require-ocr
```

### 6.2 批量导入（可选）

需要 Redis 与 worker 两个额外进程，且与 Web 共享数据目录、`REDIS_URL`、`ARQ_QUEUE_NAME`：

```bash
docker run -d --name lexvault-redis -p 127.0.0.1:6379:6379 redis:7-alpine
uv run arq app.tasks.WorkerSettings
```

批量导入限 200 文件/次、单文件 200MB，登记与派发走持久化 outbox，进程重启自动补偿未确认入队。Redis 未启动时基础功能不受影响，批量导入返回 503。

### 6.3 容器部署

镜像以非 root 用户运行，compose 提供 web/worker/redis 三服务：

```bash
docker build -t lexvault-local .
docker compose up -d
```

### 6.4 上线前检查

生产/试点部署前完成：`doctor.py` 通过、鉴权切换 `token` 模式、允许 Host 收紧、备份恢复演练（含一次异机恢复）、OIDC 如启用则完成真实 issuer 连通验收。清单见[运维手册](docs/runbook/operations.md)与[发布手册](docs/runbook/release.md)。

## 7. 关键配置

全部变量与安全示例见 [.env.example](.env.example)。要点：

| 变量 | 作用 |
|---|---|
| `LAW_REVIEW_DATA_DIR` | 私有数据目录（数据库、上传、导出、checkpoint 的根） |
| `LAW_REVIEW_AUTH_MODE` | `local`（默认，仅回环）/ `token`（远程必须） |
| `LAW_REVIEW_API_TOKENS_JSON` | principal 定义：名称、admin、case_ids、可选 `permissions` |
| `LAW_REVIEW_ALLOWED_HOSTS` | 允许的 Host 列表 |
| `LAW_REVIEW_LLM_URL` / `LAW_REVIEW_EMBEDDING_URL` | 模型/embedding 端点（默认回环；远端需 `LAW_REVIEW_ALLOW_REMOTE_MODELS=1` + https） |
| `LAW_REVIEW_OIDC_*` | 组织登录（保持为空即禁用） |
| `LAW_REVIEW_JSON_LOGS` / `LAW_REVIEW_LOG_LEVEL` | 结构化日志开关与级别 |

## 8. 运维

- **健康检查**：`GET /api/health`（存活）；`GET /api/system/health`（就绪：启动完成 + 数据库可读才 200，Redis 状态单独报告，批量导入能力随之启停）。
- **备份恢复**：`scripts/data_snapshot.py` 基于停写快照，含完整性/外键校验、清单哈希与恢复工具；Redis 与 `.env` 不在默认备份内。恢复演练流程见[发布手册](docs/runbook/release.md)。
- **日志与排查**：`LAW_REVIEW_JSON_LOGS=1` 输出结构化日志；日志不含原始卷宗内容、令牌或连接串。批量导入问题按运维手册的 503/排队排查节处理。
- **升级**：SQLite schema 有序迁移（当前 v12），拒绝未来版本；跨版本升级前先做备份，迁移失败自动回滚。

## 9. 质量保障与评测

- **测试**：603 项离线测试（另有 8 项跳过）+ 分支覆盖率 86.6%（门槛 70%），CI 禁网运行，JUnit/覆盖率报告随构建产出。本地复现：

```bash
uv run --locked ruff check app tests scripts
uv run --locked mypy
uv run --locked pytest tests scripts/test_engineering.py scripts/test_data_snapshot.py \
  scripts/test_snapshot_runtime.py -p pytest_cov -p pytest_socket --disable-socket --allow-unix-socket \
  --cov=app --cov-branch --cov-report=term-missing
node --check app/static/app.js
```

- **评测分层**（四层指标不可互相替代，详见[评测协议](docs/benchmarks/README.md)）：

| 层级 | 用途 | 入口 |
|---|---|---|
| 模型 LawBench | 法律任务混合指标，不等于应用准确率 | `unified_benchmark_runner.py` |
| 项目 RAG | 页级召回和排序，不等于答案事实正确率 | `rag_project_benchmark.py` |
| 法条检索 | 冻结 gold 上的配对排序评测（hit@5 / MRR@10 / nDCG@10），衡量是否检索到正确法条与条号，不等于法条现行有效性 | `scripts/statutory_hybrid_experiment.py`、`scripts/statutory_gold_report.py` |
| 运行时对比 | 双运行时结构、契约、性能与恢复 | `compare_agent_runtimes.py` |

- **数据口径警示**：2026-09-05 的 1,000 题结果见[验收记录](docs/runbook/validation-20260905.md)；更早的历史万题报告存在漏传任务说明等缺陷，其结论已由[优化编年史](docs/history/optimization-chronicle.md)第 9 节裁决，不得引用。`current_law` 占位集与 RAG 合成数据不构成已验证法律知识或人工标注。独立人工验收集（律师标注）尚未建立，属 G1 试点门槛。

## 10. 文档索引

| 文档 | 内容 |
|---|---|
| [文档总入口](docs/README.md) | 全部文档导航 |
| [架构说明](docs/architecture/system.md) | 组件、数据流与运行时边界 |
| [运维手册](docs/runbook/operations.md) | 配置、鉴权、健康检查、故障排查 |
| [发布手册](docs/runbook/release.md) | 发布白名单、升级与恢复演练 |
| [质量门禁](docs/runbook/quality.md) | 依赖锁定、干净安装、测试约定 |
| [组件许可](docs/runbook/licenses.md) | 第三方组件来源、许可与治理 |
| [42 项修复映射](docs/runbook/improvement-checklist.md) | 历史改进逐项落点 |
| [企业化改进报告](docs/runbook/enterprise-improvement-report.md) | 分阶段目标、工作包与验收证据（第 10–11 节为已实施记录） |
| [历史验证记录](docs/runbook/validation-20260905.md) | 2026-09-05 验收快照 |
| [优化编年史](docs/history/optimization-chronicle.md) | 已归档早期报告的正/负优化与口径裁决 |

## 11. 维护、支持与合规

- **维护模式**：个人维护项目；问题与变更经本地分支 + CI 验证后合入 `main` 并打版本标签。
- **已知待验收项**（不因代码合入而视为达标）：真实 OIDC issuer 连通、独立外部审计存储、组织数据保留政策、律师标注验收集、真实硬件容量与 SLA、法律合规评审。进展见企业化改进报告。
- **许可**：项目尚未指定开源许可证，公开分发前由维护者选择；LawBench 题库等第三方数据的再分发条件见[许可清单](docs/runbook/licenses.md)，题库本体有意不入库。
- **隐私**：`.gitignore` 与发布白名单排除案件数据、密钥、模型与运行产物；仓库不含真实卷宗。部署方需按所在辖区法律自行建立数据保留与删除流程。

[架构](docs/architecture/system.md) · [运维](docs/runbook/operations.md) · [发布](docs/runbook/release.md) · [评测](docs/benchmarks/README.md)
