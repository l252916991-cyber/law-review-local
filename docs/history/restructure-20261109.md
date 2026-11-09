# 2026-11-09 代码与项目结构重组

目的：把平铺在 `app/` 下的 88 个模块和 `scripts/` 下的 65 个脚本按职责整理，删掉没有调用者的代码，拆掉 1854 行的 `services.py`，并清零类型检查豁免。原则是**只移动和重命名，不改行为**：每一步都用全量测试、规则门禁逐题比对和函数级 AST 比对确认。

## 提交

| 提交 | 内容 |
|---|---|
| `2b96efd` | 删除没有调用者的脚本、模块与函数 |
| `40314fb` | `app/` 按领域拆成 11 个子包，改用绝对导入 |
| `412bf7f` | 按职责拆分 `services.py` |
| `c144cc0` | 合并逐字节相同的规范 JSON 摘要与复核角色判断 |
| `bcad281` | 脚本按用途分目录，根目录评测入口移入 `scripts/benchmarks/` |
| `c853700` | 清零 mypy 豁免名单，补齐直接依赖声明 |

## 删除的代码

逐个按名称全仓库检索、确认只剩定义本身后删除。源码在 `0197bed` 仍可查到。

- 8 个实验脚本：`browser_driver`、`compare_forge_readonly`、`compose_anchor_runs`、`correction_sweep`、`eval_38_arms`、`multiturn_context_eval`、`pilot_annotate`、`pilot_model_annotator`；它们的结果已记录在 `docs/history`。
- 只服务于 `correction_sweep` 的 `app/correction_candidates.py` 及其测试。
- 死函数与属性：`agents.py` 的 `AnalysisAgent`、`CrossExaminationAgent` 兼容外壳；`security.py` 的 `_session_key`、`_purge_sessions`、`oidc_session_principal`；`services.py` 的 `current_turn_key`、`EXPORT_BLOCK_TYPES`；`db.py` 的 `UPLOAD_DIR`、`EXPORT_DIR`；`benchmark_metrics` 的 `extract_labels`、`extract_crime_amount`；`ablation_analysis.main_report`；`collaboration_ablation.dataset_case_ids`；PostgreSQL 游标的 `fetchmany`。

## `app/` 路径对照

`app/main.py` 不动，新增 `app/paths.py`（`ROOT`、`APP_DIR`）。除下表注明的重命名外，模块名不变。

| 子包 | 原模块 → 新位置 |
|---|---|
| `api/` | `routes/` → `api/routes/`、`schemas`、`frontend` |
| `core/` | `config`、`logger`、`security`、`audit_trail` |
| `storage/` | `db`、`db_postgres`、`schema_postgres.sql`、`postgres_migrations/` |
| `documents/` | `tasks`、`bank_transactions`、`vlm_ocr` |
| `retrieval/` | `rag`、`rag_chunks`、`rag_child_index`、`search_backend`、`statutory_retrieval`、`legal_corpus` |
| `review/` | `services`、`context`、`agents`、`agent_tools`、`answerability`、`langgraph_agents`、`tool_loop`、`review_collaboration`、`review_jobs`、`chat_idempotency` |
| `cases/` | `case_reports`、`case_walls`、`conflicts`、`human_tasks`、`members`、`report_basis`、`report_delivery` |
| `llm/` | `model_ports`、`model_router`、`llm_usage`、`pricing`、`local_backends`、`forge_governance` |
| `integration/` | `runtime_ports`、`runtime_adapter`、`runtime_comparison`、`forge_bridge`、`forge_readonly`、`forge_runtime`、`forge_sources`、`forge_worker`、`host_integration`、`host_integration_worker`、`host_ports`、`host_writes`、`domain_integration`、`domain_writes`、`candidate_domain`、`model_domain`、`mcp_server` |
| `evaluation/` | `answer_eval`、`answer_review`、`answer_judge_client`、`eval_gate`、`evaluation.py` → `evaluation/rag_eval.py`、`rag_benchmark_dataset`、`model_comparison`、`collaboration_ablation`、`ablation_analysis`、`ablation_review` |
| `benchmarks/` | `benchmark_amount.py` → `benchmarks/amount.py`、`benchmark_event_tools.py` → `benchmarks/event_tools.py`、`benchmark_metrics.py` → `benchmarks/metrics.py`、`benchmark_postprocess.py` → `benchmarks/postprocess.py`、`benchmark_rag_solver.py` → `benchmarks/rag_solver.py`、`benchmark_reporting.py` → `benchmarks/reporting.py`、`benchmark_retrieval.py` → `benchmarks/retrieval.py`、`benchmark_solver.py` → `benchmarks/solver.py`、`benchmark_summary_tools.py` → `benchmarks/summary_tools.py`、`benchmark_weak_tasks.py` → `benchmarks/weak_tasks.py`、`lawbench`、`statutory_benchmark`、`statutory_gold`、`statutory_hybrid`、`statutory_index`、`corpus_audit`、`corpus_canonical` |

## `services.py` 拆分对照

93 个定义逐个比对 AST，与拆分前完全一致（只有 `_file_sha256` 改名为 `file_sha256`）。下表省略只在模块内部使用的下划线函数。

| 新位置 | 承接的定义 |
|---|---|
| `app/core/egress.py` | `LOOPBACK_HOSTS`、`REMOTE_MODEL_APPROVAL_ENV`、`LOCAL_MODEL_HOSTS_ENV`、`assert_model_endpoint_allowed`、`egress_opener`、`read_json_with_deadline` |
| `app/core/files.py` | `safe_filename`、`contained_path`、`_file_sha256 → file_sha256` |
| `app/core/text.py` | `concise` |
| `app/documents/ingest.py` | `MAX_PARSE_BYTES`、`MAX_PDF_PAGES`、`MAX_TEXT_CHARS`、`run_text_command`、`upload_error_message`、`extract_pdf_pages`、`extract_docx_pages`、`extract_image_text`、`extract_pages`、`infer_doc_type`、`infer_people`、`infer_dates`、`index_upload`、`get_document` |
| `app/documents/bank_ledger.py` | `persist_bank_transactions`、`persist_parsed_bank_rows`、`iter_bank_transactions`、`list_bank_transactions`、`summarize_bank_transactions`、`bank_transaction_graph` |
| `app/retrieval/search.py` | `SEMANTIC_GROUPS`、`STOP_CHARS`、`expand_semantics`、`query_terms`、`detect_route`、`search_pages`、`best_quote`、`statistics_answer` |
| `app/review/generation.py` | `LOCAL_LLM_URL`、`LOCAL_LLM_MODEL`、`local_llm_available`、`source_header`、`canonical_source_markers`、`read_sse_with_deadline`、`call_local_llm`、`resolve_model_target`、`PROMPT_VERSION`、`CHAT_SYSTEM_PROMPT`、`last_llm_usage`、`llm_provenance` |
| `app/review/chat.py` | `tool_loop_enabled`、`fallback_answer`、`chat` |
| `app/cases/gap_analysis.py` | `MAX_GAP_ANALYSIS_PAGES`、`preview_gap_analysis`、`save_gap_analysis`、`auto_analyze_case` |
| `app/cases/exports.py` | `csv_safe_cell`、`EXPORT_RENDERER_VERSION`、`DEFAULT_EXPORT_BLOCKS`、`get_export_template`、`build_export_package`、`build_export` |
| `app/storage/db.py` | `rowdict` |
| `app/llm/model_ports.py` | `strip_reasoning` |

## `scripts/` 路径对照

| 目录 | 脚本 |
|---|---|
| `scripts/evaluation/` | `eval_gate`、`replay_model_gate`、`build_answer_eval`、`build_holdout_eval`、`run_answer_eval`、`run_answer_judge`、`eval_dataset_custody`、`analyse_eval_reliability`、`pilot_kappa`、`run_collaboration_ablation`、`analyse_collaboration_ablation`、`build_ablation_blind_package`、`export_ablation_answers`、`eval_transport_faults`、`compare_local_models` |
| `scripts/benchmarks/` | `unified_benchmark_runner`、`verify_benchmark_run`、`compare_agent_runtimes`、`rag_project_benchmark`、`audit_benchmark`、`benchmark_campaign`、`benchmark_enhanced_run`、`benchmark_experiment`、`benchmark_vector_search`、`fullscale_paired`、`lawbench_coverage`、`postprocess_audit`、`score85_status`、`verify_campaign`、`statutory_gold_report`、`statutory_hybrid_experiment`、`build_2_2_sft_data`、`build_38_sft_data`、`build_38_sft_data_v2`、`audit_38_sft_data` |
| `scripts/corpus/` | `build_legal_corpus`、`build_canonical_corpus`、`build_npc_corpus`、`audit_corpus_conflicts` |
| `scripts/ops/` | `doctor`、`data_snapshot`、`setup_framework`、`setup_models`、`package_release`、`release_smoke`、`verify_deployment_release`、`generate_postgres_schema`、`migration_demo`、`migration_source_manifest`、`seed_demo_case`、`demo_identity_report`、`demo_recovery_check` |
| `scripts/acceptance/` | `local_capacity_acceptance`、`local_model_acceptance`、`local_workflow_acceptance`、`validate_application_acceptance`、`collaboration_acceptance`、`capacity_seed`、`capacity_smoke`、`delivery_smoke`、`page_flow_check`、`frontend_test_server`、`stub_model` |

此外：`scripts/fixtures/` → `scripts/acceptance/fixtures/`；`scripts/test_data_snapshot.py`、`test_engineering.py`、`test_snapshot_runtime.py` → `tests/`；根目录 `BENCHMARK_TEST_PLAN.md` → [docs/benchmarks/benchmark-test-plan.md](../benchmarks/benchmark-test-plan.md)。运行方式从 `python scripts/eval_gate.py` 变为 `python scripts/evaluation/eval_gate.py`，`python -m scripts.setup_framework` 变为 `python -m scripts.ops.setup_framework`。

## 合并与清理

- `app/core/digest.py`：`canonical_json` 与 `json_sha256`。只合并参数完全相同的 8 处实现；用默认分隔符或不排序键登记的摘要（`report_basis`、`context`、`conflicts`、`corpus_audit` 等）保持原写法，否则已存摘要会变。`runtime_ports` 按约定只依赖标准库，也不改。
- `human_tasks` 与 `case_reports` 中相同的 `_is_reviewer` 合并为 `core.security.is_case_reviewer`。
- mypy：删除 24 个模块的 `ignore_errors` 名单，修掉 83 处错误；去掉注解和 `cast` 后逐函数比对 AST，只有几处等价改写（见 `c853700` 提交说明）。
- 依赖：`pydantic`、`starlette` 被直接导入，现在显式声明（锁定版本不变）。
- ruff 新增 `TID252`，不再允许相对导入。

## 有意不改的

| 内容 | 原因 |
|---|---|
| `app/storage/postgres_migrations/*.sql` 注释里的旧路径 | 迁移文件已冻结 |
| `.gitleaksignore` | 指纹指向旧提交里的旧路径 |
| `benchmarks/answer_eval/v2/dataset.json` 的 `case_fixture: "app.db.seed_demo"`、保留集协议里的文件摘要 | 带摘要的冻结数据；改一个字节，门禁基线就要重登 |
| `docs/benchmarks/*.json` 与文档中的文件摘要表 | 记录当时的路径和摘要 |
| `docs/history/` 正文 | 过程记录保持原文，只把链接目标指向文件现在的位置 |
| `scripts/ops/migration_demo.py` 中在历史检出（`092ecac`）里执行的代码串 | 那段代码运行在旧布局里 |

## 对已有产物的影响

- **模型对照钉住版本**从 `specialist-review-v12` 升到 `v15`：路径键改为相对 `app` 包，`services.py` 换成承接其推理代码的 `review/chat.py`、`review/generation.py`、`retrieval/search.py`、`core/egress.py`。每次升级的审阅说明写在 `NATIVE_BASELINE_REVIEW`。
- **答案质量门禁**的 `INFERENCE_MODULES` 改为相对路径，因此推理版本号会变；规则模式 60 题的回答与重组前逐题一致（只差时延）。
- **LawBench 历史实验的复核**（`verify_campaign`、`verify_benchmark_run`）按当时的源码快照校验路径和摘要。重组改了冻结评分器的导入行，重组前录制的运行要在旧提交里复核：`git worktree add ../lexvault-0197bed 0197bed`，在那里运行当时的校验脚本。

## 验证

- 全量离线测试：每一步提交前都跑过，`c853700` 时 1811 通过、24 跳过（跳过项是需要 PostgreSQL、未分发 LawBench 数据或 mlx 的用例）。
- 规则门禁：46/60，与基线相同；60 题的回答文本、引用、拒答与评分和重组前逐题一致。
- ruff、mypy（187 个源文件）、`uv lock --check`、发布清单 dry-run、`scripts/ops/doctor.py` 均通过。
