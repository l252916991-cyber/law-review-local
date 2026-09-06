# 优化编年史：2026-09-02 至 2026-09-05 一次性报告永久档案

> **来源**：本档案提取自项目根目录 52 份历史报告/分析文件与 13 个一次性脚本（均已删除，本文件是其唯一载体）。报告时间跨度 2026-09-02 至 2026-09-05，产生于现行 docs/runbook 体系（validation-20260905、L01–L42 改进清单）之前，属"改进清单前"的历史快照。
> **使用规则**：引用本文任何数字时，连同「来源文件名 + 日期 + 口径（评分器版本/模型/题数）」一起引用，并遵循第 9 节裁决表；被裁决表取代的结论不得再引用。

## 0. 时间线总览

| 日期 | 事件 | 主要报告 |
|---|---|---|
| 2026-09-02 | 证据 CRUD（Phase 4）、可视化（Phase 3）、26 个单元测试、LexEval 集成、首个模型对比 | PROJECT_COMPLETION_REPORT, TEST_SUMMARY, COMPREHENSIVE_TEST_REPORT, PHASE3/PHASE4_COMPLETION, MODEL_COMPARISON_REPORT |
| 2026-09-03 | 四阶段测试体系（92 测试/80.1% 覆盖率）、13 问题安全审计、修复 13 项、1000 题测试、评分器两轮大修 | HANDOVER, SECURITY_AND_BUG_REPORT, FIXES_COMPLETED, FINAL_PROJECT_SUMMARY, PROJECT_COMPLETION_SUMMARY, analysis_1000_final.txt |
| 2026-09-04 | 修复后对比（+62%）、10,000 题全量、LangGraph 真实模型对比（16 次调用）、混合架构结论 | PARTIAL_COMPARISON_REPORT, 10K_TEST_FINAL_REPORT, LANGGRAPH_COMPARISON_REPORT, LANGGRAPH_VS_STANDARD_REPORT |
| 2026-09-05 | LangGraph vs 原生 100 题/1000 题框架对比 | FRAMEWORK_COMPARISON_REPORT_100 / _FINAL_REPORT_1000 |
| 2026-09-05 之后 | L01–L42 改进 + 队列/鉴权/引用/恢复修复，251 pytest 通过（现行有效基准） | docs/runbook/validation-20260905.md |

## 1. 正向优化清单

### 1.1 评分器修复（app/benchmark_metrics.py）

1. **拒答判定逻辑修复（P0）**：原 `if not actual: abstained=True` 把参考答案解析结果为空当成"模型拒答"，45.23% 的有效答案被误标为拒答（1,525 个完整答案被丢弃）。修复为 `abstained = not prediction`，位置 `app/benchmark_metrics.py:255`。1,000 题重评分验证：拒答率 45.23% → 0%。（来源：FINAL_PROJECT_SUMMARY.md、PROJECT_COMPLETION_SUMMARY.md；诊断脚本 check_abstain_logic.py）
2. **答案提取规则改进（P1）**：`extract_option_set()` 从 3 种格式扩展到 7 种（"正确答案是：**B**"、"选B"、"B项正确"、加粗、行首单独字母等，过滤无效字符）；`extract_labels()` 升级为三级匹配（精确 → 去空格/标点模糊 → jieba 关键词 60% 阈值）。3,372 题重评分：**平均分 0.2096 → 0.3050（+45.5%）**，满分题 11.4% → 20.6%（+81.1%），高分题(>0.5) 18.8% → 29.5%，低分题(<0.1) 62.5% → 54.2%，383 题得分提升（11.4%）。任务级：2-3 司法要素 0.0487→0.2382（+388.8%）、2-2 争议焦点 0.2620→0.6180（+135.9%）、1-2 选择题 0.3540→0.4800（+35.6%）、2-4 0.0940→0.1180（+25.5%）。位置 `app/benchmark_metrics.py:102-195`。（来源：PROJECT_COMPLETION_SUMMARY.md、rescore_with_improved_extraction.py）
3. **多选截断修复（P1）**：正则 `(?:选|应选|答)\s*([{allowed}])` 只匹配单字母，gold='AC'、pred='AC' 只提取 'A' 判 0 分（对答判错），gold='A'、pred='AC' 判 1.0（错答判对）。改为 `([{allowed}]+)`，位置 `app/benchmark_metrics.py:125`。影响约 150 题（任务 1-2/2-8/3-6）。（来源：SECURITY_AND_BUG_REPORT.md 问题 3、FIXES_COMPLETED.md）
4. **千分位陷阱修复（3-7 犯罪金额）**："8,500元" 被拆为 [8.0, 500.0] 导致误判；新增 `extract_crime_amount()`：先去 `,`/`，`，再匹配 `([\d.]+)\s*(万|千)?元` 做单位换算。验证 `score('8,500元','8500元')=1.0`。位置约 `benchmark_metrics.py:344-365`。（来源：SECURITY_AND_BUG_REPORT.md 问题 5、quick_fixes.py）
5. **中文数字刑期修复（3-4/3-5）**："三年六个月" 无法解析导致拒答（3-4 拒答率曾达 49.8%）。引入 `cn2an`，`extract_months()` 支持中文数字 `年*12+月` 换算。验证：`extract_months('三年六个月')=42`。位置约 `benchmark_metrics.py:320-342`。（来源：SECURITY_AND_BUG_REPORT.md 问题 6、FIXES_COMPLETED.md）
6. **新增 `score_prediction()` 统一评分入口**：解决双入口、新数据集多处改代码的问题，含 dataset 路由与 exact_match 兜底。（来源：SECURITY_AND_BUG_REPORT.md 问题 10）
7. **correction_f05 评分过严（记录为待改进，未修复）**：2-1 法律文件校对均分仅 0.056（零分率 75.2%），建议放宽编辑匹配或换指标。（来源：10K_TEST_FINAL_REPORT.md）

### 1.2 评测运行器修复（unified_benchmark_runner.py）

8. **question 参数缺失（P0）**：`unified_benchmark_runner.py:324` 调用 `score_lawbench_item` 漏传 `question`，2-1 任务（法律文本纠错需原句）50 题全部误判 0 分。修复后 2-1 均分 0.000 → 0.385（验证口径）。（来源：SECURITY_AND_BUG_REPORT.md 问题 2、FINAL_COMPLETION_REPORT.md）
9. **instruction 字段被忽略（P0）**：`unified_benchmark_runner.py:302-305` 只传 question 不拼 instruction，"所有任务的 instruction 被忽略，全部 1000 题测试无效"；一轮 271 题运行（`seq_NO_INSTRUCTION_271items_invalid`）整体作废。修复为 `prompt = f"{instruction}\n\n{question}"`。（来源：FIXES_COMPLETED.md 第 1 项）
10. **统一评测运行器为新建交付物**：585 行，支持多数据集、`--resume` 续跑、每题检查点、重试、超时、错误分类。（来源：HANDOVER.md、FINAL_DELIVERY_REPORT.md）

### 1.3 前端修复（app/static/app.js）

11. **前端 824 处语法错误修复（P0）**：821 处中文弯引号 + 3 处多余右括号导致 SyntaxError，UI 完全崩溃。修复：sed 全局替换 + 删多余括号 + eslint --fix。（来源：SECURITY_AND_BUG_REPORT.md 问题 1、FIXES_COMPLETED.md 第 2 项）

### 1.4 后端 API / 服务修复

12. **async 阻塞 OCR（P1）**：上传 PDF 同步执行 `pdf2image + pytesseract`（7 秒+）阻塞事件循环；实测上传期间 `/api/health` 延迟 15ms → 984ms（65 倍）。采用 `asyncio.to_thread(extract_pages_from_pdf, ...)`（方案 B；arq 队列方案 A 记录为生产推荐）。功能级验证当时标记"需手动测试"。（来源：SECURITY_AND_BUG_REPORT.md 问题 4）
13. **批量导入 Redis 健康检查（P2）**：Redis 不可用时批量导入返回 200 但任务永远卡 `queued`。修复：startup ping 检测、新增 `GET /api/system/health`、Redis 失联时批量导入 503、前端轮询 5 分钟超时提示。（来源：SECURITY_AND_BUG_REPORT.md 问题 7；现行 operations.md 语义更严格）
14. **safe_filename 防御纵深（P3）**：原只处理正斜杠，反斜杠 `..\..\etc\passwd` 实测保存为 "....etcpasswd"。增强：`[/\\]+` 统一替换、去连续点、去前导 `._`、截断。（来源：SECURITY_AND_BUG_REPORT.md 问题 11 及附录 B）
15. **审计日志补全（P3）**：删除证据端点补 `audit_log(case_id, action='删除证据', detail=标题)`。（来源：SECURITY_AND_BUG_REPORT.md 问题 13）
16. **CDN 依赖本地化（P3）**：vis-network 与 echarts（约 1.9MB）下载到 `app/static/vendor/`，解决离线时图谱与图表失效。（来源：FIXES_COMPLETED.md 第 13 项）
17. **重评分脚本不覆盖原始数据（P2）**：`rescore_results.py:67` 原直接覆盖 checkpoint；改为写 `<name>_rescored.json` 保留原件。（来源：SECURITY_AND_BUG_REPORT.md 问题 9）

### 1.5 测试体系与工程质量

18. **覆盖率从虚假到真实**：此前声称"100% 覆盖率、生产就绪"无证据；实测基线 **70.6%**，扩展后 **80.1%**（2,205/2,752 语句），+9.5%。模块明细：config/evaluation/db 100%，benchmark_metrics 91%，lawbench/rag 90%，services 71%，agents 66%，main 61%，tasks 38%。（来源：HANDOVER.md、FINAL_DELIVERY_REPORT.md）
19. **测试用例 55 → 92（+67%）**：新增 test_01_database_extended（14：事务、并发、跨案件隔离、FTS5）、test_02_api_security（23：边界值、错误处理、XSS/SQL 注入/路径穿越）、test_03_rag_extended（18：召回、引用准确性、跨案件混淆、向量索引、RRF）。（来源：HANDOVER.md）
20. **测试套件修复**：21 个失败分类为 RAG 过期 12（`k=` → `limit=`）、契约不一致 7、真实 bug 1（多选截断）、隔离问题 1（共享数据库 → `tmp_path`）；另修 `conn.lastrowid` → `cursor.lastrowid` ×2。结果 96/117 → **104/117（89%）**。（来源：FIXES_COMPLETED.md 第 7 项）
21. **端口配置统一**：三个基准脚本硬编码 8000/8080 统一改为 `LAW_REVIEW_LLM_URL`。（来源：HANDOVER.md）
22. **隔离测试环境与恶意样本库**：`output/test_data/` 独立目录；9 个 fixtures（malicious_content.txt、large_10mb.bin、corrupted.pdf、malicious.exe 等）。（来源：FILE_INDEX.md）
23. **基准数据集框架**：LawBench 10,000 题（commit `e30981bb`）+ METRICS_MAPPING.json 官方 20 类评分映射 + RAG 240 题骨架 + 现行法律 300 题骨架，合计 13,040 题框架。（来源：FILE_INDEX.md、HANDOVER.md）
24. **证据 CRUD 全栈（Phase 4A，约 493 行）**：POST/PATCH/DELETE 证据端点、Pydantic 模型（title 2-200 字符、8 类枚举、页码校验、来源归属校验）、补 `field_validator` 导入与端点 `conn.commit()`；前端编辑/删除对话框、级联统计二次确认。测试 15/15；API 响应 30-50ms。（来源：PHASE4_COMPLETION.md）
25. **证据可视化（Phase 3，约 340 行）**：ECharts 时间线、疏漏检测仪表板（完整度评分 `100 - totalGaps*5`）、Vis.js 关系图谱（状态着色、单击高亮链路、双击跳原文）。零后端修改。（来源：PHASE3_COMPLETION.md）
26. **XSS 前端防护**：新增 `escapeHtml()` 转义所有用户输入。（来源：PROJECT_COMPLETION_REPORT.md）
27. **证据 CRUD UX 修复 12 项**：空来源禁用按钮、页码验证、魔法数字提常量、Escape 关闭对话框、空状态提示等。（来源：PROJECT_COMPLETION_REPORT.md）

### 1.6 LangGraph 双运行时（架构级新增）

28. **LangGraphCoordinator 新增**：`app/langgraph_agents.py`（623 行）、`app/runtime_comparison.py`、`compare_agent_runtimes.py`、`tests/test_04_langgraph_runtime.py`（328 行）。与原生 Coordinator 共享全部业务 Agent，只对比调度与持久化；明确不引入 LangChain Agent/ReAct/LangSmith/云服务。（来源：ARCHITECTURE.md）
29. **失败恢复能力**：`POST /api/agent-runs/{run_id}/resume`；恢复仅重跑失败节点及下游；恢复后 Critic/LLM 调用次数不重复。幂等设计：步骤按 `(run_id, node_name)` UPSERT、记忆按 `source_run_id` 去重；`agent_runs` 新增 runtime/checkpoint_thread_id/resume_count 字段。（来源：ARCHITECTURE.md、LANGGRAPH_COMPARISON_REPORT.md）
30. **LangGraph 测试**：新增测试 30/30 通过；Playwright 验证单跑、双版本比较、故障恢复、390px 布局。（来源：LANGGRAPH_COMPARISON_REPORT.md）
31. **API/前端集成**：`agent-chat` 支持 `mode=multi_agent`（默认）与 `mode=langgraph`；`agent-compare` 顺序执行并关闭长期记忆写入。（来源：ARCHITECTURE.md）

## 2. 负面效果与回归

1. **提取规则改进导致 2-1 回归 -92.9%**：0.0563 → 0.0040（500 样本），疑提取规则影响编辑距离计算；建议单独调查 correction_f05，**当时未解决**。（来源：PROJECT_COMPLETION_SUMMARY.md）
2. **383 题对比中 2-2 下降 -52%、2-10 下降 -100%**：可能为样本随机性/新边界问题，未定论。（来源：PARTIAL_COMPARISON_REPORT.md）
3. **instruction bug 使整轮测试作废**：271/1000 题运行全部无效归档。（来源：SECURITY_AND_BUG_REPORT.md）
4. **早期 LawBench 100% 拒答**：首次采样 accuracy 0.0、拒答率 1.0（延迟 11ms 说明模型根本没被调用），根因 LLM_URL/MODEL 未配置。（来源：COMPREHENSIVE_TEST_REPORT.md）
5. **评分器"拒答"误判 45.23%**：项目最大的评分事故，大量有效答案被丢弃（见第 1 节第 1 条）。（来源：FINAL_PROJECT_SUMMARY.md）
6. **"LangGraph 快 10.3%"被 1000 题推翻**：100 题显示快 246ms（-10.3%），1000 题显示慢 66ms（+3.1%）。样本量效应、任务组合、热身效应；"1000 题结果更可靠"。**任何引用"LangGraph 更快"的说法均已被取代**。（来源：FRAMEWORK_COMPARISON_REPORT_100 vs _FINAL_REPORT_1000）
7. **16 次真实模型对比不能证明提速**：配对均值 -22.32%，但分轮方向相反；报告自我修正"应优先解释为模型状态、执行顺序与采样噪声"。该轮 max_tokens 仅 320（生产 900），可能截断引用。（来源：LANGGRAPH_COMPARISON_REPORT.md）
8. **答案契约通过率仅 1/8**：16 次执行中仅 1 组同时满足非空答案、有效引用、复核提示。（来源：LANGGRAPH_COMPARISON_REPORT.md）
9. **共享模型算术错误案例**：Run #40 把 842+1230+120 万元算成 3192 万元（正确 2192 万元）——模型质量问题，非框架问题。（来源：LANGGRAPH_COMPARISON_REPORT.md）
10. **500/1000 题配对测试评分全 0，该轮数据无效**：comparison_partial_report.txt 与 final_analysis.txt 的具体数字**不可引用**（配对管道/评分调用问题），仅证明该轮尝试失败。（来源：两份 txt）
11. **3-x 任务高拒答**：修复前基线 3-1/3-2/3-3/3-4/3-7/3-8 拒答 100%、3-5 84%、3-6 68%；10K 中 3-4 49.8%。原因：中文数字缺失（已修）、千分位、输出格式不符。（来源：FINAL_COMPLETION_REPORT.md、10K_TEST_FINAL_REPORT.md）
12. **测试契约残留 13 个失败**：定性为契约不一致，需明确规范后调整。（来源：FIXES_COMPLETED.md）
13. **全局 Python 环境依赖冲突未解决**：选择新建隔离 venv 解决，未动全局旧包。（来源：LANGGRAPH_COMPARISON_REPORT.md）
14. **内存压力**：修复期记录"swap 6.5/8GB"，列为中期优化项。（来源：FIXES_COMPLETED.md）
15. **CURRENT_STATUS.md 日期为模板字面量**：文件头 `$(date ...)` 未渲染——佐证这些报告多为会话中快速生成。（来源：CURRENT_STATUS.md）
16. **LexEval 251 题未导入**：任务 5_3 JSON 解析错误，13,900/14,151（98.2%），低优先级。（来源：COMPREHENSIVE_TEST_REPORT.md）
17. **评分器多选截断的双向危害**：既对答判错也错答判对——修复前数据既虚高也虚低。（来源：SECURITY_AND_BUG_REPORT.md 附录 A）

## 3. 关键基准与评测数据

### 3.1 模型对比（2026-09-02，MODEL_COMPARISON_REPORT.md）

Qwen3.5-9B vs **Qwythos-9B-v2**（MLX 4bit），5 项中国法律问答，关键词匹配评分：

| 指标 | Qwen3.5-9B | Qwythos-9B-v2 |
|---|---|---|
| 平均准确度 | 50% | **80%** |
| 平均响应时间 | 28.13s | **8.93s**（快 3.15 倍） |
| 成功率 | 100% | 100% |

分项：三段论 Qwen 100%/27.63s vs Qwythos 100%/**2.29s**（快 12 倍）；正当防卫 67% vs 100%；举证责任 0% vs 67%；"携带凶器"解释 50% vs 100%。结论：Qwen3.5 输出带"Thinking Process"导致关键词匹配失败；**选型定为 Qwythos-9B-v2**。详细结果：`benchmark_Qwen3.5-9B_1788304828.json`（已删除的本地产物）。

### 3.2 LawBench 1000 题（修复前基线，2026-09-03/04，analysis_1000_final.txt）

- 总均分 **0.2918**、满分率 16.0%、p50 延迟 6.0s（n=998）。分数分布：[0-0.1) 52.5%、[0.9-1.0) 17.5%。
- 任务明细（均分）：3-3 罪名预测 0.695、3-7 犯罪金额 0.660、2-5 阅读理解 0.558、2-2 纠纷焦点 0.540、1-2 司法考试 0.420、3-6 案例分析 0.440、1-1 法条背诵 0.206、2-1 校对 0.072、2-6 命名实体 0.000。
- 与旧 checkpoint 同题重评分：1-1/1-2/2-1/2-2/2-3/2-4/2-5 **分数漂移 0**，证明评分器确定性。

### 3.3 修复前后对比（2026-09-04，PARTIAL_COMPARISON_REPORT.md，383/1000 题）

| 指标 | 修复前 | 修复后（部分） | 变化 |
|---|---|---|---|
| 总均分 | 0.2912 | **0.4717** | **+62.0%** |
| 中位数 | 0.0578 | 0.5000 | +764% |
| 满分率 | 16.0% | 25.1% | +57% |
| 零分率 | 46.3% | 30.0% | -35% |

任务级：2-4 +600%、2-1 +312%（question 参数修复）、2-3 +175%、2-6 从 0.000→0.695、1-2 +71%。回归项见第 2 节。

### 3.4 LawBench 10,000 题全量（2026-09-04，10K_TEST_FINAL_REPORT.md）

- **总均分 0.2925（29.25%）、满分率 17.4%、零分率 45.5%、拒答率 3.7%、平均延迟 4.3s/题**；总耗时 ~14.7 小时。
- 高分任务：3-3 罪名预测 0.711（满分率 50.6%）、3-7 金额 0.670、2-2 焦点 0.618、2-5 0.525。低分：2-6 NER 0.022（零分率 97.8%）、2-10 触发词 0.035、2-1 校对 0.056。高拒答：3-4 49.8%、3-5 24.4%（中文数字）。
- 分数两极分化：53.0% <0.1，18.3% ≥0.9。

### 3.5 框架对比：原生 DAG vs LangGraph（Qwythos-9B-v2-4bit-mlx，种子 42，temperature 0.0）

- **1000 题（2026-09-05，FRAMEWORK_COMPARISON_FINAL_REPORT_1000.md，最可靠口径）**：平均准确率均 **43.82%**（1000 组配对 100% 相同）；原生 2,095ms vs LangGraph 2,161ms（**+3.1% 框架开销**）；checkpoint ~13-15KB/题。结论：3.1% 开销换失败恢复/可视化/可维护性"对大多数场景值得"。
- **16 次真实多 Agent 场景（2026-09-04，LANGGRAPH_COMPARISON_REPORT.md）**：核心结构等价 **8/8**；完整答案契约 **1/8**；配对耗时 -22.32% 但分轮方向相反（不可当加速证据）；checkpoint +216KB/次。
- **修复后 vs LangGraph 完整流程（LANGGRAPH_VS_STANDARD_REPORT.md，20 题 2-2）**：均分 0.400→0.500（+25%），延迟 3-5s → 29,215ms（+500-800%），ROI≈0.04；推荐任务路由混合架构（`LANGGRAPH_TASKS = ["2-2","2-8","3-1","3-2","3-3","3-6","3-8"]`）。

### 3.6 RAG 与 Agent 平台指标（demo-legal-rag-v1，4 查询，k=5）

Recall@5 **1.0**、MRR **0.75**、Citation Coverage **1.0**、平均延迟 **24ms**；Agent 6 次运行 5 成 1 败（83.3%），平均 63.0s。（来源：COMPREHENSIVE_TEST_REPORT.md）

### 3.7 LexEval 集成

NeurIPS 2024，23 任务，**13,900/14,151（98.2%）导入**；LexCog 六大认知：记忆 1,800、理解 2,400、逻辑推理 5,400、辨别 800、生成 2,500、伦理 2,500。CAIL（268 万刑法文书）**始终未集成**（长期未完成项）。（来源：COMPREHENSIVE_TEST_REPORT.md）

## 4. 安全与质量审计要点

- **13 问题审计（SECURITY_AND_BUG_REPORT.md，2026-09-03）与修复**：P0×2（前端语法、question 参数）、P1×2（多选截断、async OCR）、P2×6、P3×3——**13/13 于当日修复**（FIXES_COMPLETED.md）。安全验证：SQL 注入 ✅、路径穿越基础 ✅（反斜杠漏防→补）、XSS 前端缓解但服务端不消毒 ⚠️、async 阻塞 ❌（已修）。
- **设计决策（记录为已知限制）**：当时无认证/授权（本地单用户）；建议生产加 `bleach.clean()`。**时效**：现行仓库已落地 local/token 双模式鉴权、案件权限、会话 cookie——"无认证"结论已过时。
- **code_review.md（10 维 96.35/100）**：扣分点为复杂函数缺注释、魔法数字、无认证(-2)、无 CSRF(-1)、无 Rate Limiting(-1)、N+1 查询、无分页、无缓存、无软删除、API 无版本号、前端全局 state。其中"无认证/无 pytest 套件/覆盖率虚假"等已被后续工作解决；"分页/缓存/软删除/API 版本号"仍是长期待改进项。
- **"生产就绪"结论被推翻**：2026-09-02 自评"生产就绪"被次日实测（覆盖率 70.6%、92 测试 87 通过）推翻——小样本自评不可引用。

## 5. 架构与产品决策

### 5.1 双运行时并存（ARCHITECTURE.md，最权威定稿）

- native（默认、向后兼容）与 langgraph（StateGraph + 本地 SQLite checkpoint）并存；共享全部业务 Agent——"对比的是调度和持久化框架，而不是两套提示词或业务逻辑"。
- 执行图：START→Planner→Memory Recall→Hybrid Retrieval→Facts/Evidence/Contradiction 扇出→Gap Detection→Critic→Memory→END。
- 状态边界：ReviewState 只存 JSON 可序列化数据；DB 连接/Agent 实例/异常注入器不进 checkpoint。
- 持久化职责分离：产品审计 SQLite（agent_runs，用户可见）≠ LangGraph checkpoint（图状态）；不互相替代。
- 适用边界：SQLite saver 仅单机原型，多实例应换生产 checkpoint 后端；AI 输出必须律师复核。

### 5.2 混合架构策略（THREE_VERSION_COMPARISON_PLAN.md）

- LawBench 20 任务中 **14 个（70%）适合 LangGraph**（高复杂度 7：2-8、3-1、3-2、3-3、3-4、3-6、3-8；中复杂度 7：2-2、2-3、2-5、2-7、2-9、3-5、3-7），**6 个不适合**（1-1、1-2、2-1、2-4、2-6、2-10——纯记忆/单步检索/序列标注类）。
- 按任务复杂度动态路由的设计共识**未作为产品功能落地**（现行 API 仍由用户手动选 mode）——长期待办。

### 5.3 Phase 3/4 已知限制（当时记录，部分已过时）

- 无乐观锁（并发编辑可能覆盖）、无权限（→ v0.2.0 已解决）、删除不可逆无历史、无批量操作。
- 证据标注 CRUD 后续 L01-L42 轮已完成。

## 6. 部署与操作知识（对现行 runbook 有增量的部分）

1. **批量导入产品级限制**：单次最多 **200 个文件**、单文件最大 **200MB**；支持 PDF/DOCX/TXT/PNG/JPG；建议每批 50-100 个；扫描件 OCR 约 **30-60 秒/文件**；状态字段 queued/processing/completed/failed。（DEPLOYMENT_GUIDE.md）
2. **证据关系图谱交互约定**：边颜色（绿=印证、红=矛盾、蓝=资金链路）；单击高亮链路、双击跳原文；性能基准（<50 证据 1s 内、50-200 证据 3s 内）；Chrome 90+/Firefox 88+/Safari 14+。（DEPLOYMENT_GUIDE.md、VISUALIZATION_TEST.md）
3. **疏漏检测触发关键词**："疏漏/完整性/缺失/遗漏/缺少/待补"；`GET /api/cases/{case_id}/gap-analysis?severity=高`。（DEPLOYMENT_GUIDE.md）
4. **基准测试运行手册**：
   - 速度口径随模型/负载差异巨大（5s/题预估 → 实测 17.7s → 最终 680 题/h）。**新跑全量前先测 50 题外推**。
   - 续跑：`python3 unified_benchmark_runner.py --dataset lawbench --resume --retry 2 --timeout 180`；每题检查点，可随时中断续跑。
   - 系统要求：内存 16GB+、磁盘 10GB+、防休眠、LLM 服务全程在线。
   - `verify_model()` 强校验 `/v1/models` 含配置模型，不匹配直接 RuntimeError（不静默回退）。
   - framework_comparison_1000.py：`--limit-per-task`（默认 50=1000 题）、`--sample-seed 42`；小规模验证用 `--limit-per-task 5`。
5. **lawbench-instruction-v2 提示词契约**（framework_comparison_1000.py，绕过评分提取脆弱性的关键设计）："多选题必须返回所有正确选项格式 A,B,C；数值题返回纯数字不含单位或千分位分隔符；分类题返回准确类别标签；只返回答案本身"。

## 7. 案例样本与评测集设计

1. **30 个民生模拟案件样本库**（CASE_SAMPLES_SUMMARY.md，2026-09-04）：6 类 × 5 个（劳动争议、房屋买卖、医疗纠纷、消费者权益、物业纠纷、租赁纠纷）；`data/sample_cases/<type>/`，每案 .json + .txt 双格式；示例案号 "(2024)京01民终1001号"。合规声明：仅测试用、已脱敏、不得商用。工具三件套 download_case_documents.py / import_sample_cases.py / view_case.py（均已删除，功能可按需重写）。
2. **LawBench 采样设计**：1000 题验证集 = 20 任务 × 50 题（种子 42）；采样可复现且隐藏答案（单测 `test_lawbench_sampling_is_reproducible_and_hides_answers`）；10K 全量。
3. **LangGraph 配对测试集设计**（test_set_1000.json，已删除）：从 10,000 题筛 14 个适合任务（steps≥2、类型∈{推理,分析,综合}、复杂度∈{中,高}），每任务约 71-72 题。任务筛选标准有留档价值。
4. **长期未完成项**：RAG 240 题 + 现行法律 300 题仅骨架（540 题内容从未补齐）、CAIL 未集成、Playwright UI 测试、CI/CD（现已建成）。

## 8. 使用示例与集成要点

- **lawbench_benchmark.py**（已删除，接口最稳定）：可续跑、可审计；LexEval 伦理任务直接下载缓存（6_1 偏见 1000、6_2 道德 1000、6_3 隐私 500）；`chat_template_kwargs: {"enable_thinking": false}`、temperature 0。
- **LexEval 23 任务元数据全表**（lexeval_integration.py，已删除）：1_1 刑法选择题 500 多选……6_3 冲突场景判断 500 单选；"可自动评分的 13 个任务"为默认批量范围。如需重建，从 benchmarks/lexeval/LEXEVAL_INFO.json（仍在仓库）出发。
- **API 环境变量契约**：`LAW_REVIEW_AGENT_MAX_ITERATIONS`（默认 5）、`LAW_REVIEW_LLM_TIMEOUT`（默认 180s，超时返回中间结果）、`LAW_REVIEW_VECTOR_DB`、`LAW_REVIEW_EMBEDDING_MODEL`。
- **"重评分不重跑"方法论**：analyze_rescore.py（拒答统计）、check_abstain_logic.py（拒答取证）、rescore_with_improved_extraction.py（3,372 checkpoint 重打分对比，只读原始数据）、detect_issues.py（P0/P1/P2 分级问题检测）——工作流：先小样本配对验证修复、重评分不重跑、保留旧 checkpoint 对比漂移。

## 9. 报告间矛盾与时效裁决表

### 9.1 被后续报告取代的结论（后日期为准）

| 早期主张 | 来源与日期 | 被什么取代 |
|---|---|---|
| "100% 通过率（15/15）、96.35/100、生产就绪" | PROJECT_COMPLETION_REPORT 等（09-02） | 次日实测：覆盖率 70.6%→80.1%、92 测试 87 通过、21 失败（09-03）。**不可引用** |
| "覆盖率 100%"（无证据） | 早期自称 | 实测 80.1% → 现行 86.73%（v0.2.0） |
| "LangGraph 比原生快 10.3%" | FRAMEWORK_COMPARISON_REPORT_100（09-05 02:56） | 同日 1000 题：+3.1% 开销（更可靠） |
| "LangGraph 配对均值快 22.32%" | LANGGRAPH_COMPARISON_REPORT | 自我声明分轮方向相反，不可当稳定加速证据 |
| "拒答率 45.23%/均分 0.2096" | FINAL_PROJECT_SUMMARY（09-03） | 提取改进后 0.3050 → 修复后 0.4717（383 题） |
| "无认证/授权（设计决策）" | SECURITY_AND_BUG_REPORT 等（09-02/03） | 现行仓库已实现 local/token 鉴权、案件权限、会话 cookie（v0.1.1+） |
| "前端语法错误/async OCR 未修复" | 10K_TEST_FINAL_REPORT（09-04 早） | FIXES_COMPLETED（09-03 11:10）已 13/13 修复；10K 报告该段是复制的旧状态 |
| "21 个失败测试" | SECURITY_AND_BUG_REPORT（09-03） | 修复后 104/117 → 现行 251 → 390（v0.2.0） |
| "LangGraph 测试 30/30，全量 123 项 12 失败" | LANGGRAPH_COMPARISON_REPORT（09-04） | validation-20260905：251 全绿 → v0.2.0：390 全绿 |

### 9.2 同主题数据口径不一致（引用必须注明口径）

- **三份"1000 题"总均分**：0.268（早期评分器）→ 0.2918（现行代码口径重评分，推荐引用并注明"修复前评分器"）→ 0.2912（baseline 口径）。差异来自评分器版本，不是模型变化。
- **10K 均分 0.2925 与"修复后预期 0.35-0.40"**：修复后 1000 题完整重跑从未完成（只有 383 题 + 失败的配对轮），"+62%" 仅 383 题样本支撑。
- **模型名混用**：不同轮次用过 Qwen2.5-7B-Instruct-GPTQ-Int4 与 Qwythos-9B-v2-4bit-mlx，横向比较必须核对模型。
- **测试通过数演进**：26（09-02）→ 92（09-03）→ 104/117 → 251（09-05）→ 390（v0.2.0，2026-09-07）。单调增长，引用带日期。

### 9.3 仍然有效、值得长期保留的硬事实

- 模型选型：Qwythos-9B-v2-4bit-mlx 胜出（80% vs 50%，8.93s vs 28.13s）。
- LawBench 10K 基线全景（0.2925 / 17.4% 满分 / 45.5% 零分 / 4.3s，任务级排名完整）。
- 框架对比结论：准确率 0 差异、LangGraph ~3.1% 延迟开销、checkpoint ~14KB/题。
- LangGraph 价值定位：恢复能力与可维护性，而非速度。
- 评分器健壮性清单（多选 `+`、千分位、cn2an、7 种答案格式、三级标签匹配、question 必传、instruction 必拼）及"评分器 bug 可使基准整体失真 45%+/+62%"的教训。
- "40.7% 零分不是答错而是提取失败"的根因分析方法论。
- LANGGRAPH_COMPARISON_REPORT 的免责声明（适用全部历史模型基准）："历史实验记录……此后代码已完成队列、鉴权、引用及恢复修复……本轮未重跑，不能将新增单元测试结果当成新的模型性能或质量结论。"
