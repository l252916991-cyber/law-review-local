# LexVault：本地法律卷宗阅卷原型

FastAPI + SQLite 的本地阅卷工作台，支持文档提取、页级检索、证据管理及可审计的多 Agent 分析。普通问答与 Critic 默认使用同一个本地 Qwythos 模型；这是“统一模型 + 超时规则降级”，不是自动多模型路由。AI 输出仅辅助审阅，不能代替律师判断。

## 已实现的核心能力

- PDF、DOCX、文本和图片导入；扫描 PDF/图片通过本机 Tesseract OCR，保留文档及页码。
- SQLite FTS5/BM25、法律词项与本地向量经 RRF 融合；模型不可用时使用确定性降级，并保留检索元数据。
- 原生 Python DAG（默认）与 LangGraph StateGraph（可选）复用业务 Agent；LangGraph 使用独立 SQLite checkpoint，支持失败续跑。
- Planner → 记忆召回 → 检索 → Facts/Evidence/可选 Contradiction → 可选 Gap Detection → Critic → 记忆；专家步骤与业务审计落库。
- 双运行时顺序对比，读取相同的执行前记忆快照并关闭长期记忆写入，展示引用、节点、契约和耗时差异。
- 证据事项和关系管理、时间线及疏漏展示；Redis + arq 提供可选批量导入，不是基础单文件导入的必需服务。
- 证据标注新增/编辑/删除；可选 token 登录与案件范围授权，身份由服务器配置而非用户填写的名称决定。
- 单次 Agent 阅卷使用 `202` 后台任务与节点状态轮询，刷新后可继续追踪；双版本对比与失败恢复接口仍同步执行，不宣称已实现 SSE。

SQLite checkpoint 不是业务审计替代品；故障恢复也不等于任意外部副作用天然只执行一次。生产多实例、人工审批 interrupt、数据库静态加密仍需单独设计。

## 快速启动

要求 Python 3.11–3.14、SQLite FTS5。推荐独立环境，避免与全局旧版 LangChain 或模型推理依赖冲突。

```bash
python3 -m pip install uv==0.12.0
uv sync --locked --group dev
uv run python scripts/doctor.py
uv run uvicorn app.main:app --host 127.0.0.1 --port 8765
```

打开 <http://127.0.0.1:8765>。没有模型时可以使用规则模式；真实模型服务独立运行，不由 Web 应用自动下载或安装。配置参考 [.env.example](.env.example)，修改后需显式加载环境并重启相应服务。

PDF/OCR 系统依赖：

```bash
# macOS
brew install poppler tesseract tesseract-lang
# Debian/Ubuntu
sudo apt-get install poppler-utils tesseract-ocr tesseract-ocr-chi-sim
uv run python scripts/doctor.py --require-ocr
```

仅处理 TXT/DOCX 时不需要 OCR 工具。模型与文档可能占用大量磁盘，发布前应使用独立的数据目录。

批量导入另需两个进程：

```bash
docker run -d --name lexvault-redis -p 127.0.0.1:6379:6379 redis:7-alpine
uv run arq app.tasks.WorkerSettings
```

Web 与 worker 必须共享数据目录、`REDIS_URL` 和 `ARQ_QUEUE_NAME`。Redis 未启动时基础功能仍可用，批量导入返回服务不可用。更多说明见[运维手册](docs/runbook/operations.md)。

## 测试与评测

```bash
uv run --locked ruff check app tests scripts
uv run --locked mypy
uv run --locked pytest tests scripts/test_engineering.py scripts/test_data_snapshot.py \
  scripts/test_snapshot_runtime.py --disable-socket --allow-unix-socket \
  --cov=app --cov-branch --cov-report=term-missing
node --check app/static/app.js
```

离线测试禁止网络 socket，不需要本地 LLM/Redis。装载、抽样与评分协议测试使用临时生成的合成题，不依赖本机未提交的 LawBench 文件；真实题库校验与模型实验另行执行。CI 生成真实 JUnit、覆盖率 XML/JSON/HTML 和日志，不运行需要本地模型的 LawBench。覆盖率门槛为 70%，不表示当前全仓类型检查完备：mypy 先覆盖维护脚本，ruff 首阶段执行关键正确性检查。

三类评测必须分开：

| 层级 | 用途 | 入口 |
|---|---|---|
| 模型 LawBench | 法律任务混合指标，不等于应用准确率 | `unified_benchmark_runner.py` |
| 项目 RAG | 页级召回和排序，不等于答案事实正确率 | `rag_project_benchmark.py` / RAG API |
| 原生 vs LangGraph | 同业务 Agent 的结构、契约、性能与恢复 | `compare_agent_runtimes.py` |

2026-09-05 已完成 1,000 题真实模型测试；方法见 [BENCHMARK_TEST_PLAN.md](BENCHMARK_TEST_PLAN.md)，结果和评分修正见[验收记录](docs/runbook/validation-20260905.md)。原始输出保留在私有 `output/` 中。历史万题测试漏传任务说明，不能引用其分数作为可靠模型准确率。`current_law` 占位集不代表已验证现行法律，RAG 合成数据也不能替代独立人工标注。详见[评测协议](docs/benchmarks/README.md)。

## 接口与代码导航

开发机 API schema：<http://127.0.0.1:8765/docs>。Agent 请求 `mode` 取 `multi_agent`（默认）或 `langgraph`。

```text
POST /api/cases/{case_id}/agent-chat       单次运行
POST /api/cases/{case_id}/agent-jobs       202 后台单次运行
GET  /api/agent-jobs/{job_id}              轮询任务/节点/结果
POST /api/cases/{case_id}/agent-compare    双运行时对比
POST /api/agent-runs/{run_id}/resume       恢复失败 LangGraph 运行
GET  /api/agent-runs/{run_id}              节点审计轨迹
POST /api/cases/{case_id}/batch-import     批量导入
GET  /api/batch-imports/{batch_id}         批量进度
POST /api/auth/session                   token 登录
GET  /api/auth/me                        当前身份
DELETE /api/auth/session                 退出
```

`app/main.py` 提供 API；`db.py` 管理业务 SQLite；`services.py` 处理文档/问答/导出；`rag.py` 管理混合检索与记忆；`agents.py`、`langgraph_agents.py`、`runtime_comparison.py` 提供双运行时；`tasks.py` 提供批量队列；`review_jobs.py` 管理后台阅卷；`security.py` 管理身份和案件授权；`config.py`/`logger.py` 管理配置和日志；评测模块为 `evaluation.py`、`lawbench.py`、`benchmark_metrics.py`、`benchmark_reporting.py`、`rag_benchmark_dataset.py`。前端位于 `app/static/`，自动化测试位于 `tests/` 和 `scripts/test_*.py`。

## 发布与边界

- 默认 `local` 模式仅允许回环连接；通过反向代理、局域网或公网提供服务必须改用 `token` 模式，并配置允许的 Host 与 TLS。token 和案件授权是单机最小边界，不是完整组织级 IAM。[认证配置](docs/runbook/operations.md)
- 模型服务 URL 应指向受控本机服务；改为远端会把相关提示词/证据发往该端点，不再满足纯本地边界。
- 不删除原始实验或案件来“清理仓库”；`.gitignore` 排除私有数据，发布脚本使用白名单并拒绝符号链接。[发布说明](docs/runbook/release.md)
- 未指定本项目开源许可；公开分发前应由维护者选择许可证，并核对模型及 LawBench 原始数据源的再分发条件。

[文档入口](docs/README.md) · [架构](docs/architecture/system.md) · [42 项修复映射](docs/runbook/improvement-checklist.md) · [历史报告索引](docs/history/README.md)
