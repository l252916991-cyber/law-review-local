# E01 项目与 AI 入口总表（2026-10-22）

依据：[执行计划书 §5.1 E01](execution-plan-20261022.md)、[迁移台账](ledger.md)。

状态：**已完成（含 3 项未决项目）**。主责 PM/LV；本文由自动化代理整理，实际承担人由 PM 指定（计划书 §3）。

方法：两份**只读**清点（律所 `app/`；电商 `src/forgecommerce/`）+ **机械交叉核查**：从装饰器提取全部路由（律所 62 条 `@app.*` + 2 条 `@router.*`，见 `output/migration/e01-entry-inventory-20261022/routes.txt`）并与清点表逐项对照；模型调用点、工具入口、后台 worker 按文件:行号逐一核对。未确认项目列为未决，**不宣称范围已全部覆盖**。

## 1. 项目登记与范围

| 项目 | 角色 | 当前版本/绑定 | 范围决定 | 理由 |
| --- | --- | --- | --- | --- |
| LexVault「本地律所」 | 第一迁移对象（宿主） | 0.3.0；框架 0.6.0（`vendor/`，未接线） | **纳入** | 有权限、审计、后台任务、双运行时与真实业务表；台账已登记 |
| ForgeCommerce「电商agent」 | 第二实际应用 | 0.1.0；框架 0.5.0（`vendor/`） | **纳入**（E21 起） | 实际应用、已绑定框架、有独立 CI 与私有远端；升级验收在 M7 |
| ERP-AGENT-open-source-main | 候选目录 | 无 uv.lock（requirements 形态） | **未决**（PM 确认迁入或排除） | 疑似开源参考源码而非业务系统；未核实自有数据/身份/产品入口 |
| AI视频 | 内容目录 | 不适用 | **未决**（倾向排除，PM 确认） | 脚本/提示词/产出目录，无 Agent 运行时与业务库 |
| deepseek-harness | 开发工具 | 不适用 | **未决**（倾向排除，PM 确认） | 开发工具检出，非业务迁移对象 |

未决项的处置截止节点：E24 前（PM）。

## 2. 目标治理方式定义（本文使用的口径）

| 代号 | 治理方式 | 适用 |
| --- | --- | --- |
| **RT** | 框架 RuntimeControl（`start/inspect/resume/status`，能力声明驱动） | Agent 编排、单 Agent、固定 DAG、多 Agent |
| **RP** | 受控读端口 HostReadPort（E04 七项方法） | 检索、读页、记忆、法条、目录、既有疏漏 |
| **WP** | 受控写端口 HostWritePort（E12，六类动作 + 幂等键 + 回执） | 运行/步骤/疏漏/草稿记忆/会话消息/审计 |
| **MP** | 受控模型端口（E05：预算、deadline、用量、失败语义、后端记录） | 生成模型调用；embedding/reranker 经检索端口 |
| **EV** | 评测端口（固定版本、权限、输入身份） | 评测/基准入口（E07、E17） |
| **HOST** | 宿主确定性实现（不接框架，登记在案） | 普通 CRUD、导入、文件服务、业务审批、纯规则分析 |
| **JOB** | 后台任务（归属宿主队列；状态经投影/回执反映） | 批量导入、后台阅卷（E13 收敛） |

## 3. LexVault AI 入口总表

落库列只列**业务表**；"审计"列指是否写 `audit_log`。权限以中间件 `security.py:212-219` 为基线（POST/PATCH/PUT/DELETE→edit、GET→view、`/export`→export），另注 handler 内检查。

| 入口 | 类型 | 当前路径（文件:行号） | 模型/工具 | 落库 | 审计 | 目标治理 | 主责 | 迁入理由 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `POST /api/cases/{id}/chat` | HTTP | `chat` `main.py:1363` → `services.chat` `services.py:791` | 生成模型（可选）+embedding+reranker | conversations, messages, embedding_cache | 有 | **MP**（模型）+**RP**（检索）+WP | LV | 普通问答入口，计划书 §1 明确要求覆盖 |
| `POST /api/cases/{id}/agent-chat` | HTTP | `agent_chat` `main.py:1391` | 生成模型+embedding+reranker+只读工具 | agent_runs, agent_steps, gap_detections, memories | 有 | **RT**+RP+WP（现存旁路见 §6-8） | LV | Agent 阅卷主入口；原生/LangGraph 双运行时 |
| `POST /api/cases/{id}/agent-jobs` | HTTP→后台 | `submit_review` `review_jobs.py:105` → `_execute` `:55` | 同 agent-chat | review_jobs, conversations, messages + Agent 各表 | 有 | **RT**(后台)+JOB+WP | LV | 后台阅卷入口；E13/E14 收敛其私有 `_start_run` 修改 |
| `GET /api/agent-jobs/{job_id}` | HTTP | `review_status` `review_jobs.py:155` | 无（读 trace） | 无 | 无 | RT(status)+RP | LV | 后台状态轮询 |
| `POST /api/cases/{id}/agent-tools/call` | HTTP | `call_readonly_agent_tool` `main.py:1248` → `execute_readonly_tool` `agent_tools.py:134` | search→embedding+reranker；其余纯 SQL | audit_log | 有 | **RP**（工具面收敛到读端口） | LV | 工具调用入口；计划书点名 |
| `POST /api/cases/{id}/analyze` | HTTP | `analyze` `main.py:1262` → `auto_analyze_case` `services.py:987` | 无（纯正则） | evidence, audit_log | 有 | **HOST**（登记为确定性函数） | LV | 计划书点名；确认无模型调用 |
| `POST /api/cases/{id}/agent-compare` | HTTP | `compare_agent_runtimes` `main.py:1482` | 生成模型×2+embedding×2+reranker | 两套 agent_runs/steps/gaps | 有 | **RT**+RP+WP | LV | 对照入口；计划书点名 |
| `POST /api/cases/{id}/vector-index` | HTTP | `build_vector_index` `main.py:1531` → `rag.ensure_vector_index` `rag.py:236` | embedding+reranker（硬编码远程） | embedding_cache | 有 | **RP**（检索端口记录后端/用量） | LV | 向量索引入口；计划书点名 |
| `POST /api/cases/{id}/evaluate-rag` | HTTP | `evaluate_rag` `main.py:1574` → `evaluation.evaluate_case` `evaluation.py:77` | embedding+reranker（硬编码远程） | rag_evaluations | **无** | **EV**（补审计与端口） | LV | 评测入口；计划书点名 |
| `POST /api/agent-runs/{id}/resume` | HTTP | `resume_agent_run` `main.py:1437` | 生成模型+embedding+reranker | agent_runs/steps/gaps/memories + checkpoint | 有 | **RT**(resume 按能力路由) | LV | 恢复入口；E14 按保存版本路由 |
| `GET /api/agent-runs/{id}` | HTTP | `agent_run_trace` `main.py:1564` | 无 | 无 | 无 | RT(inspect) | LV | 轨迹读取 |
| `POST /api/cases/{id}/search` | HTTP | `search` `main.py:879` → `services.search_pages` `services.py:547` | 无（词法） | 无 | 无 | HOST（或 RP 词法通道） | LV | 确认无模型；登记以便对照 |
| `GET /api/cases/{id}/gap-analysis/preview` | HTTP | `preview_case_gap_analysis` `main.py:1230` → `GapDetectionAgent.run` `agents.py:365` | 无（确定性启发式） | 无 | 无 | HOST（领域节点复用） | LV | 领域启发式，非模型 |
| `POST /api/cases/{id}/gap-analysis/detect` | HTTP | `detect_and_save_case_gaps` `main.py:1239` | 无（启发式） | gap_detections, audit_log | 有 | WP(gap_detection) | LV | 疏漏写入入口 |
| `POST /api/model-config/test` | HTTP | `test_model_config` `main.py:460` | 探测 `{base}/models` | model_settings.json（PUT/DELETE） | security_events | **MP**（统一出口校验；现自建回环判断 `main.py:470`） | LV | 模型配置入口；旁路 §6-7 |
| `GET /api/health` | HTTP | `health` `main.py:403` → `local_llm_available` `services.py:627` | 探测 `/models`（白名单） | 无 | 无 | MP（健康探测登记） | LV | 探测也算模型面 |
| `POST /api/cases/{id}/documents` | HTTP | `upload_documents` `main.py:656` | tesseract 子进程（无模型） | documents, pages, audit_log | 有 | HOST/JOB | LV | 导入与解析；OCR 子进程登记 |
| `POST /api/cases/{id}/batch-import` | HTTP→arq | `batch_import_documents` `main.py:685` → `tasks.process_batch_import` `tasks.py:82` | 无模型（OCR） | batch_imports/files/dispatch | 无（worker 侧写） | JOB | LV | 后台导入队列 |
| `GET /api/benchmarks/lawbench` | HTTP | `lawbench_dataset` `main.py:490` | 无（本地数据集） | 无 | 无 | EV（数据集登记） | LV | 评测数据入口 |
| `ReadonlyCaseSession` / `run_framework` | Python（M2） | `app/forge_readonly.py`、`app/forge_bridge.py` | 离线；NoModel 拒绝推理 | 仅私有临时快照 | 无（内存 access_log） | **RT**(`forgeharness-readonly`，E04 已实现控制面) | LV | 当前唯一框架执行链路 |
| 基准/评测脚本族 | CLI | `benchmark_solver.py`、`rag_project_benchmark.py`、`compare_agent_runtimes.py`、`unified_benchmark_runner.py`、`scripts/*` | 生成模型/embedding（**自建出口校验或直连**） | 输出文件；部分写 embedding_cache | 无 | **EV**+MP（旁路 §6-2/6-3） | LV | 评测与基准必须纳入治理或明确排除 |

## 4. ForgeCommerce AI 入口总表（按代码清点，E21 复核）

| 入口 | 类型 | 当前路径 | 模型/工具 | 落库 | 目标治理 | 主责 |
| --- | --- | --- | --- | --- | --- | --- |
| `POST /api/conversations/{id}/messages` | HTTP | `main.py:781` → `agent.submit` `agent.py:839` | 云端对话模型（compatible）+`consult_products`；offline 规则路由 | requests/messages/conversations | **RT**（框架 SessionService 已在用） | EC |
| `POST /api/requests/{id}/resume` | HTTP | `main.py:814` | 同上（恢复同一 turn） | requests | RT(resume) | EC |
| `GET /api/requests/{id}/events` | HTTP | `main.py:822` | 无（SDK trace） | 无 | RT(inspect) | EC |
| `POST /api/intents/{id}/confirm` | HTTP | `main.py:880` | `create_handoff_ticket`（写工具） | tickets/effect_keys/intents | RT+**WP** | EC |
| `GET /api/retrieval` / `POST /api/rag/diagnose` | HTTP | `main.py:671` / `diagnostics.py:45` | embedding+rerank+qdrant | 只读 | **RP**（检索端口） | EC |
| `POST /api/records/{id}/reindex` / 索引守护 | HTTP/后台 | `main.py:586` / `index_worker.py:118` | embedding+qdrant | rag_jobs/rag.sqlite | RP+JOB | EC |
| 知识审核发布（imports/records/transition/content/simulate-publish） | HTTP | `main.py:598/646/666/1022/1031` | 无模型（框架 RevisionStore） | revisions/resources/publications | HOST（框架记录层） | EC |
| `POST /api/strategies/{id}/evaluate` | 后台 | `main.py:1243`（BackgroundTasks） | offline 规则 | evaluation_runs | EV | EC |
| CLI：`forgecommerce-evaluate`、`scripts/check.sh`、`qualify.py` | CLI | `evaluation.py:181`、`scripts/check.py:21` | offline；`--live` 才云端 | 报告文件 | EV | EC |

电商事实要点：框架 0.5.0 wheel 未被修改（118 个模块哈希与安装副本一致）；默认 offline、三类云出口默认关闭；`.state/` 全部 SQLite，权限 700/600；**该目录是 git 仓库**（main 干净，远端私有 GitHub，HEAD `6a752d845e01`）——E02 §5 记。

## 5. 未纳入本轮（明确排除或未决）

- 普通 CRUD、商品/证据/案件维护、导出、模板、银行流水导入、训练题评分（规则）等：**HOST**，不接框架。
- OCR（tesseract 子进程）：宿主外部进程，登记在案，不属模型端口。
- 未决项目（§1）在 PM 确认前不计入估算，也不宣称覆盖。

## 6. 旁路登记（当前未受统一治理的位置）

2026-10-23 宿主接入补充：[隔离实施记录](host-integration-execution-20261023.md)新增显式的六动作写端口及独立合成持久图，未接正式路由；下表 4/5/8/9 不能因此标为关闭。只读私有快照新增各自检索/记忆的第二种离线对照，生成模型、embedding、reranker 的实际模型身份和旧入口治理仍待 E05/E07/E14。

2026-10-23 更新：[本批实施记录](next-batch-execution-20261023.md)。新私有快照链路已接入后端准入、调用记录与脚本模型的框架模型核心。以下表格保留原入口位置；正式 HTTP/UI/后台和评测脚本的接线仍需 E14 或对应独立适配任务，不能把新端口就绪标成这些旧入口已关闭旁路。`reranker` 的显式失败策略保留，不因迁移而静默换后端。

| # | 旁路 | 位置 | 关闭任务 |
| --- | --- | --- | --- |
| 1 | M2 检索直接调 `agent_tools._search`，不经 `execute_readonly_tool` 的 audit_log | 原 `forge_readonly.py`；**E04 已改为经 `LocalHostReadPort` 并记录 port_access_log** | 已完成（业务审计在 E12/E14 接入） |
| 2 | 基准求解器自建出口策略 | `benchmark_solver.py:78-99`；`unified_benchmark_runner.py:193`；`scripts/pilot_model_annotator.py:24`；`scripts/build_38_sft_data.py:108-119` | E05（登记排除或接入 MP） |
| 3 | embedding 出口校验仅远程分支；reranker 失败不降级 | `rag.py:150-153`、`rag.py:205-208` | E05 |
| 4 | 后台队列直接写 conversations/messages | `review_jobs.py:87-91` | E13/E14 |
| 5 | `evaluate_case` 写 rag_evaluations 无审计、无函数级权限 | `evaluation.py:116-128` | E12/E14 |
| 6 | `vector-index`/`evaluate-rag` 硬编码远程嵌入 | `main.py:1542`、`main.py:1580` | E05 |
| 7 | `model-config/test` 自建回环判断 | `main.py:470` | E05 |
| 8 | 编排层直接写业务表（runs/steps/gaps/memories） | `agents.py`、`langgraph_agents.py` | E12/E13（WP 投影） |
| 9 | LangGraph 编排状态存独立 sqlite，恢复入口仅靠中间件归属 | `langgraph_agents.py:109-110`；`main.py:1437` | E14 |
| 10 | OCR 子进程面 | `services.py:318-320`、`tasks.py:76` | 登记为宿主能力（不接 MP） |
| 11 | 在线/离线由请求体布尔值决定；无预算/deadline 记账 | `main.py:1361-1366`、`review_jobs.py:47-52` | E05（预算与 deadline） |

电商侧旁路（E21 处理）：训练题评分为确定性规则（`main.py:1085-1094`，明示待人工复核）；模型调用集中在 `agent.py` 单点（相对收敛）。

## 7. 证据

- 路由机械提取：`output/migration/e01-entry-inventory-20261022/routes.txt`（62+2 条，含行号）。
- 模型调用点/工具入口/后台 worker 机械提取：`output/migration/e01-entry-inventory-20261022/model-and-tool-entries.txt`（生成模型调用恰好 2 处 `agents.py:597`、`services.py:835`；出口校验缺口 `rag.py:153` 仅远程分支、`main.py:470` 未用统一校验；只读工具入口恰好 2 处 `main.py:1253`、`agents.py:190`；arq 注册 1 个函数、线程池 2 worker + 准入 4；编排层直写 `agent_runs`/`agent_steps`）。
- 两份只读清点原始报告见本会话记录；关键行号已抽样复核（`agent_chat` 1391、`agent-tools/call` 1248、`analyze` 1262、`agent-compare` 1482、`vector-index` 1531、`evaluate-rag` 1574、`resume` 1437、`agent-jobs` 105/155）。
- 电商：`docs/IMPLEMENTATION_STATUS.md`、`docs/FRAMEWORK_BINDING.json`、`reports/current-checks/summary.json`。

## 8. 未决与下一任务

1. PM 确认 ERP / AI视频 / deepseek-harness（截止 E24）。
2. PM 指定各条目实际承担人（角色代号见计划书 §3）。
3. E05 处理模型/嵌入旁路（§6-2/3/6/7/11）；E12/E13 处理写入与投影旁路（§6-4/5/8/9）。
