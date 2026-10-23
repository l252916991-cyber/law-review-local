# LexVault 改进清单逐项映射

范围仅为 LexVault 原清单 L01–L42，不包含 Career Lab 或个人面试资料。状态区分“实现/配置”“安全替代”“保留边界”，不把不适用建议硬改成新风险。远程 CI 尚需首次推送执行；具体通过数以本轮原始测试产物为准，不引用历史“全部完成”报告。

| ID | 当前处理 | 验证入口与限制 |
|---|---|---|
| L01 | 已初始化本地 Git；未提交、未推送 | `git status`；维护者审核暂存内容后再提交，不能自动公开 |
| L02 | 安全替代：模型留在原处，忽略并排除发布包 | `.gitignore`、`scripts/setup_models.py`；不搬动正在使用的权重 |
| L03 | 安全替代：原始报告保留，规范文档与历史索引分离 | `docs/history/README.md`；默认发布白名单不包含旧结项报告 |
| L04 | 面试材料不进入默认源码发布包 | `.gitignore`、`scripts/package_release.py`；未删除用户资料 |
| L05 | 旧“完成凭证”排除版本与发布，不再作为验收依据 | `.gitignore`；保留原件，不改写历史 |
| L06 | 源码备份排除版本与发布 | `*.backup`、`*.before_extraction_fix`；不删除手工回退证据 |
| L07 | 原始 benchmark JSON 保留本机，排除发布 | `.gitignore`；新的产物使用 `output/<run>/` |
| L08 | 新维护脚本集中到 `scripts/`；一次性脚本标为历史 | `docs/benchmarks/README.md`；未执行旧 auto-fix/清理脚本 |
| L09 | 完善私有数据、环境、缓存、模型与产物忽略 | `.gitignore`；忽略规则不会撤销已经跟踪的敏感文件 |
| L10 | 安全替代：不清空案件数据，使用白名单发布与备份恢复 | `scripts/package_release.py`、`scripts/data_snapshot.py` |
| L11 | `output/` 保留原始实验但排除提交/发布 | 不默认清理或覆盖历史 run |
| L12 | 修正“模型路由”为统一模型与 Critic 超时降级 | `README.md`、`app/config.py`；不伪造第二模型 |
| L13 | 文档统一；同时修复 Redis pool/队列名称真实故障 | `app/tasks.py`、任务测试；基础功能无需 Redis |
| L14 | README 取消易失真的编号式功能列表 | 当前 README 无跳号 |
| L15 | 去重 API、数据模型说明 | API schema 与 README 单一入口 |
| L16 | README 模块导航覆盖实际应用/任务/评测/权限模块 | 详细代码以目录和自动生成 schema 为准 |
| L17 | 证据标注 CRUD 后端与前端补齐 | `app/main.py`、`app/static/app.js`；沿用证据归属鉴权 |
| L18 | 删除闲置 Chroma/LangSmith 配置占位 | `app/config.py`；不误删 LangGraph checkpoint 实际配置 |
| L19 | 维护文档收敛到 docs 的 architecture/benchmarks/runbook | 老路径保留，`docs/README.md` 为规范索引 |
| L20 | FTS 异常记录诊断，区别空命中与检索失败 | `app/rag.py`；同时移除查询路径全量重建 FTS |
| L21 | 模型可用性探测增加诊断 | `app/services.py`；日志不得暴露敏感提示词或凭证 |
| L22 | LLM 与 Critic 配置在使用时读取，消除 import 时重复默认 | `app/config.py`、`app/agents.py`、配置测试 |
| L23 | 文件索引移到受限线程并发，事件循环不做长时间同步 OCR | `app/tasks.py`、`LAW_REVIEW_INDEXING_CONCURRENCY`；并发需按磁盘/OCR实测调节 |
| L24 | 统一推荐 CLI：`unified_benchmark_runner.py`，离线审计 `scripts/audit_benchmark.py` | 历史循环及原始回答保留；1000 题完成后独立 v3 重评，未覆盖冻结的 v2 |
| L25 | 测试使用独立临时库与动态路径作用域 | `tests/support.py`、`app/db.py`；不再以 import 顺序偶然决定测试库 |
| L26 | 增加任务、配置、评测、鉴权、后台任务及引用契约等回归 | `tests/`；原“零测试”判断已纠正，覆盖范围看 coverage |
| L27 | CI 固定官方 action SHA、锁安装、合成协议夹具、禁网络测试、真实 JUnit/coverage 产物 | `.github/workflows/test.yml`；不依赖本机题库，无本地模型调用、无 continue-on-error；托管执行结果未冒充完成 |
| L28 | 分支覆盖率配置与 70% 门槛 | `pyproject.toml`、CI artifacts；不能为隐藏失败随意调低 |
| L29 | 保留输入边界安全测试名称，补真实身份与案件权限测试 | 输入验证本就属于安全测试，不做无意义改名 |
| L30 | pyproject + uv.lock；arq/redis 有范围及解析后的精确版本 | `uv sync --locked`、`uv pip check`；独立环境避免污染推理服务 |
| L31 | 开发依赖入锁并导出完整 requirements-dev | pytest/httpx/coverage/ruff/mypy/fakeredis/pytest-socket/pre-commit |
| L32 | 增加 Ruff、mypy、pre-commit | 当前 Ruff 强制关键正确性；mypy 先覆盖维护脚本，不宣称全业务严格类型完成 |
| L33 | 增加 `.env.example` 与部署说明 | 实际 LLM/Redis/上传/embedding/auth 配置；需显式加载而非假称自动 dotenv |
| L34 | local 回环默认 + token 身份、案件范围、Host/跨站 API 请求防护 | `app/security.py`；覆盖 GET 导出；仍需 TLS、凭证管理，非组织级 IAM |
| L35 | 后台任务 + 202/轮询，单跑前端刷新续追 | `app/review_jobs.py`；compare/resume 保持同步；无 SSE，不宣称分布式队列 |
| L36 | 准确标明数据成熟度：既有 240 合成 RAG 可用，current-law 仍 WIP | 未伪造新人工标注题库或现行法验证；真实标注需另行数据授权与法律审校 |
| L37 | 统一模型检查/固定 commit 下载入口 | `scripts/setup_models.py`；默认 dry-run、显式执行、不猜备选模型；既有本地量化来源不能补造 |
| L38 | 不盲目共享 SQLite 连接或迁移 PostgreSQL | 保留短事务/WAL与单机边界；增加备份恢复，先测锁等待再谈连接池 |
| L39 | 与 L23 合并：共享并发上限、文件级状态和幂等导入键 | 不会重复导入已完成文件；持久化每个文件用于恢复，不以牺牲可靠性换取更少 UPDATE |
| L40 | 缓存身份覆盖模型/后端/维度/文本版本及显式 revision | `app/rag.py`；替换同名权重须修改 `LAW_REVIEW_EMBEDDING_REVISION` |
| L41 | 请求总量/文件数量/逐文件大小限制与分块 staging | `app/main.py`；索引受限并发，临时文件失败清理；不是无限容量分片协议 |
| L42 | 运行/节点结构化错误诊断与安全展示 | `app/agents.py`、`app/langgraph_agents.py`；原始 traceback 作为私有诊断，不向普通客户端泄露 |

## 原清单遗漏但本轮补入

- 证据删除 NameError、测试路径隔离与现有失败回归。
- 引用格式/来源支持/律师复核契约分开，模型失败或不合格答案不得写入长期记忆。
- `legacy` 记忆保留但不自动召回，`draft` 不是经过律师确认的事实。
- RAG 评测必须绑定对应案件 ground truth；检索片段覆盖与答案引用支持不能混称。
- 业务库、checkpoint、uploads/exports 的可信停写备份与新目录恢复；保持 Redis 和密钥配置边界显式。

## 明确保留的外部决策

不代用户选择开源许可证、不公开推送、不删除案件/模型/原始报告、不改动 Career Lab，不编造新法律标注数据或既有模型转换来源。多实例部署、完整组织身份系统、人工审批 interrupt、生产静态加密与独立业务标注仍需单独规划和验收。
