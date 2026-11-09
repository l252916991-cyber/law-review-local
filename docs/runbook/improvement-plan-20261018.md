# LexVault 下一阶段改进计划书：证据保真、引用图谱与可评测阅卷

计划日期：2026-10-18（Asia/Shanghai）
计划对象：`/Users/xiaoy/Documents/本地律所`（LexVault 0.3.0 工作树）
主要读者：项目负责人、后端/RAG、前端、测试、试点律师、运维
状态：**规划稿**。本文件只定义目标、设计、验收与证据口径，**不表示任何能力已实施或验收通过**。

---

## 0. 执行摘要

我查看了 GitHub 上 20 余个相邻项目，并区分了三类：法律文档/合同审阅平台、文档 RAG 与文档解析、评测与可观测性。结论是：

1. **没有找到与中国刑事阅卷完全对等的成熟开源产品。** 最接近的是 OpenContracts（法律文档管理 + 引用图谱 + 人工标注）、RAGFlow/Kotaemon（文档 RAG 与引用问答），以及 ChatLaw（中文法律咨询的多 Agent SOP）。它们证明方向成立，但不能直接替换 LexVault。
2. **LexVault 的差距不在“再多一个 Agent 框架”，而在证据保真和可评测性。** 当前解析是页级文本，引用是页级格式校验，没有精确字符区间/版面坐标/版本摘要；没有律师标注基准；运行轨迹与律师反馈没有形成闭环。
3. **下一阶段应做四件事，而不是换框架：**
   - **证据保真**：结构化文档模型 + 可选高质量解析器 + 精确引用（字符区间、版面、版本）。
   - **引用与证据图谱**：中文法律引用解析、案件内引用/证据边、AI 提议 + 律师复核。
   - **可评测阅卷**：LexVault Review Benchmark、Bad Case 库、律师反馈闭环、离线 CI 门槛。
   - **复核工作台**：按案由的检查清单、表格化审阅、PDF 精确高亮、逐条接受/驳回。
4. **部署与运维收口（原 E33）仍是发布门槛，不是本轮的主攻方向。** Docker/TLS、备份恢复、容量与审计锚点不完成，不应宣称可生产使用。
5. **继续保留原生 DAG 作为默认，不整体替换为 ForgeHarness，也不引入第三个 Agent 运行时。** 外部项目只借设计，不引入平台级依赖。

预期结果：

- **3 个月**：精确引用与结构化解析上线；20–30 案基准与 Bad Case 库建立；律师反馈可记录；试点可演示“引用可点开、可核对、可驳回”。
- **6 个月**：引用/证据图谱、表格化审阅、PDF 高亮、运行轨迹与反馈闭环；G2 部署验收完成。
- **12 个月**：30–60 案冻结基准（含 holdout）、分任务质量报告、容量与恢复数据；再决定是否进入 G3。

---

## 1. 调研范围与方法

- 检索方向：法律文档审阅/合同分析、中文法律 RAG/法律大模型、PDF/版面/表格解析、RAG 评测、Agent 评测、LLM 可观测性、文档管理系统、法律引用解析。
- 样本：选取 20 个仓库阅读 README/API 元数据，最终在正文引用 12 个最相关项目；星数为 GitHub API 在 **2026-10-18** 的读数，只用于判断社区规模，**不是质量或安全性证明**。
- 许可：逐项核对 README/API 的 license 字段；涉及 AGPL/GPL/ELv2/模型权重限制的项目，只借鉴设计，不复制代码或权重。
- 局限：我没有运行这些项目的完整业务链路；对它们的能力判断来自公开 README、文档和元数据。LexVault 的现状判断来自本仓库源码、测试和文档。

### 1.1 一个关键判断

GitHub 上的“法律 AI”大致分四层：

| 层 | 代表 | 与 LexVault 的关系 |
|---|---|---|
| 法律文档平台/DMS | OpenContracts、Paperless-ngx | 最接近“卷宗管理 + 标注 + 权限 + 检索”的产品形态 |
| 文档 RAG/问答 | RAGFlow、Kotaemon | 最接近“页级检索 + 引用 + 多文档问答”的技术形态 |
| 中文法律模型/咨询 | ChatLaw、DISC-LawLLM、LaWGPT | 领域知识与评测参考，不是阅卷工作流产品 |
| 解析/评测/可观测性 | Docling、MinerU、RAGAS、DeepEval、Langfuse | 上游能力与工程基础设施 |

LexVault 的差异化不在“又一个法律聊天机器人”，而在**案件级证据治理、页级引用、法条核验、律师复核与本地部署**。下一阶段的投入应加固这条差异化链路。

---

## 2. GitHub 对标：值得学什么、不该照搬什么

### 2.1 核心对标项目

| 项目 | 定位与可借鉴点 | 不照搬的部分 | 许可 |
|---|---|---|---|
| [OpenContracts](https://github.com/Open-Source-Legal/OpenContracts)（约 1.5k stars） | 把文档、引用、标注做成**引用图谱**：文档是节点，引用是边，人工标注是 ground truth；AI 提议、人工接受；PDF 精确文本-坐标映射；同一图谱通过 REST/GraphQL/MCP 暴露；语料版本、权限、历史。 | Django 平台、公共语料/社区、完整 MCP 服务；LexVault 只需案件级子集。 | MIT |
| [ChatLaw](https://github.com/PKU-YuanGroup/ChatLaw)（约 7.6k stars） | 中文法律咨询的**多 Agent + SOP**：信息收集 → 法律检索 → 综合建议 → 咨询报告；知识图谱与人工筛选提升可靠性；按完整性、逻辑、正确性、语言、指导性、权威性评估。 | MoE/微调模型、AGPL 代码；LexVault 不训练模型。 | AGPL-3.0 |
| [RAGFlow](https://github.com/infiniflow/ragflow)（约 92k stars） | “Quality in, quality out”：深度文档理解、**模板化切分**、知识编译（wiki/graph/tree/timeline）、Agentic Retrieval、可视化切分与可追溯引用、异构数据源。 | Go/完整 RAG 平台、Docker 栈；只借切分、知识编译、引用可视化的设计。 | Apache-2.0 |
| [Kotaemon](https://github.com/Cinnamon/kotaemon)（约 26k stars） | 文档 QA 的**引用体验**：PDF 内高亮、相关度分数、低相关警告、问题分解、ReAct/ReWOO、GraphRAG、混合检索与重排。 | Gradio 通用聊天 UI；LexVault 只借“引用可核对”和“低置信提示”。 | Apache-2.0 |
| [Docling](https://github.com/docling-project/docling)（约 69k stars） | 结构化文档表示：版面、阅读顺序、表格结构、公式、图片分类、OCR、统一 `DoclingDocument`、Markdown/JSON 导出、本地/气隙运行。 | 全格式矩阵与重依赖；作为**可选解析适配器**，不作为核心依赖。 | MIT |
| [MinerU](https://github.com/opendatalab/MinerU)（约 82k stars） | 复杂 PDF/Office → LLM-ready Markdown/JSON；版面、表格、OCR；中文文档场景覆盖好。 | 作为 Docling 的对照/备选解析器；许可为 Apache-2.0 + 附加条件，需法务确认。 | 自定义（Apache-2.0 基础） |
| [eyecite](https://github.com/freelawproject/eyecite)（约 291 stars） | 法律引用的**抽取、聚合、标注、清洗**：full/short/reference/supra/id 引用、前向解析、在原文中插入链接；被 CourtListener/CAP 大规模使用。 | 美国判例法 reporter 数据库；LexVault 需要中文法条/案号解析器。 | BSD-2-Clause |
| [RAGAS](https://github.com/vibrantlabsai/ragas)（约 16k stars） | RAG 指标：context precision/recall、faithfulness、answer relevance；合成测试数据；反馈闭环。 | LLM-as-judge 作为唯一门槛；需要人工校准。 | Apache-2.0 |
| [DeepEval](https://github.com/confident-ai/deepeval)（约 19k stars） | “评测即测试”：pytest 风格、CI/CD、Agent 轨迹指标（任务完成、工具正确性、计划遵循、步骤效率）、RAG 指标、可解释分数。 | 商业平台绑定；LexVault 只借指标/测试组织方式。 | Apache-2.0 |
| [Langfuse](https://github.com/langfuse/langfuse)（约 36k stars） | Trace/评估/数据集/Prompt 版本/人工反馈/Playground；自托管。 | 完整平台与 `ee` 目录；LexVault 先做本地 span + 可选导出。 | MIT（`ee` 除外） |
| [promptfoo](https://github.com/promptfoo/promptfoo)（约 26k stars） | 声明式评测 + **红队/漏洞扫描** + CI + 本地隐私；适合把提示注入、越权、数据外泄变成可重复用例。 | Node/平台依赖；可借 YAML 用例和红队清单。 | MIT |
| [Paperless-ngx](https://github.com/paperless-ngx/paperless-ngx)（约 47k stars） | 成熟 DMS：OCR、标签/通信方/文档类型、自定义字段、保存视图、工作流、重复检测、邮件摄入、REST API。 | GPL 代码与通用 DMS 范围；借“处理状态 + 可筛选视图 + 批量操作”的 UX。 | GPL-3.0 |

### 2.2 补充样本

| 项目 | 可借鉴点 | 备注 |
|---|---|---|
| [Tabular_Review](https://github.com/jamietso/Tabular_Review)（约 85 stars） | 法律文档的**表格化审阅**：批量导入、定义抽取列、逐行复核。 | 与“证据矩阵/流水核对”高度契合。 |
| [AI-WorkDeck](https://github.com/AI-WorkDeck/aiworkdeck)（约 167 stars） | 可审阅的 Agent 编辑、来源关联工作、三方文档合并。 | 借“AI 修改必须可接受/驳回/合并”的交互。 |
| [LexReviewer](https://github.com/LexStack-AI/LexReviewer)（约 21 stars） | PDF + 混合检索 + grounded review。 | 小项目，仅作形态参考。 |
| [chinese-law-corpus](https://github.com/lttxzmj/chinese-law-corpus)（约 31 stars） | 中国法律/司法解释逐条 JSON、指导案例/公报案例，CC0。 | 可做法条语料结构参考；仍需核验版本与现行有效性。 |
| [LegalBench](https://github.com/HazyResearch/legalbench)、[CUAD](https://github.com/The-Atticus-Project/cuad) | 法律任务分类、标注协议、合同条款抽取。 | 辖区/任务不同；借任务 taxonomy 与标注纪律，不复制数据。 |

### 2.3 三条可执行结论

1. **法律产品的护城河是“来源可核对 + 人工可复核”，不是模型参数。** OpenContracts、Kotaemon、eyecite 的共同点是把引用做成可点击、可定位、可审阅的对象；LexVault 已有页级引用，但还没做到精确 span/坐标和高亮。
2. **文档解析是上游质量瓶颈。** Docling/MinerU 的价值不是“多一个依赖”，而是把表格、阅读顺序、版面坐标变成结构化数据；否则检索和引用都只能停留在页级文本。
3. **评测与可观测性是工程分水岭。** RAGAS/DeepEval/Langfuse/promptfoo 的共同点是让失败可分类、可复现、可回归；LexVault 已有评测脚本，但缺少律师标注基准和轨迹级反馈。

### 2.4 明确不采纳

- 不把 OpenContracts/RAGFlow/Paperless 作为平台替换 LexVault；它们的部署、许可和业务边界都不匹配。
- 不把 Phoenix 作为核心依赖：其许可为 Elastic License 2.0，不是 OSI 开源；如需要，只用 OpenTelemetry/本地 JSONL。
- 不引入 Marker 的模型权重作为默认解析器：代码 Apache-2.0，但权重为 OpenRAIL-M，商业使用有门槛。
- 不把 RAGAS/DeepEval 的 LLM-as-judge 当作唯一质量门槛；法律场景必须以人工标注和确定性检查为主。
- 不因“多 Agent”而增加自治循环；ChatLaw 的 SOP 值得学，但其模型/微调路线不适合 LexVault。

---

## 3. 现状判断：长板与短板

### 3.1 已有长板

- 本地优先、单机 SQLite、FastAPI + worker，符合律所隐私与运维边界。
- 页级混合检索（FTS5/BM25 + 法律词项 + 向量 + RRF + 重排）与来源多样性控制。
- 法条核验 fail-closed：显式法名/条号精确匹配、版本歧义拒绝、纯文本检索不返回 `ok`。
- 证据治理：原件哈希、同案去重、审批归属、编辑后审批失效、标注、疏漏检测、资金流水结构化。
- 双运行时（原生 DAG 默认 + LangGraph 可选）、后台任务、权限矩阵、审计、备份恢复工具。
- 已有 E01–E33 的企业化路线图与大量离线测试；E31 P1–P3 子智能体、E32 模板结案包、E33 部分部署能力已落地（E31 P3 真实流水验收、E33 目标机验收仍待试点）。

### 3.2 六个关键差距

| # | 差距 | 现状证据 | 影响 |
|---|---|---|---|
| 1 | **解析仍是页级文本，缺版面/表格/坐标** | [`app/documents/ingest.py`](../../app/documents/ingest.py) 用 `pdftotext -layout` 逐页取文本，仅当去空白后少于 24 字符才 OCR；[`extract_docx_pages`](../../app/documents/ingest.py) 把表格拼成 `【表格】` 文本。 | 多栏、跨页表格、扫描件、印章/手写页会丢结构；引用只能到页，不能精确高亮。 |
| 2 | **引用是页级 + 格式校验，不是精确 span/版本校验** | [`validate_review_answer`](/Users/xiaoy/Documents/本地律所/app/review/agents.py#L53) 检查引用编号范围、document_id/page_no 存在、quote 非空；[`_citation`](/Users/xiaoy/Documents/本地律所/app/review/agents.py#L75) 只输出 document_id/page/quote/url，没有 char_start/end、bbox、page_sha256。 | 无法证明“引用的这句话确实在原文这个位置”；页面更新后旧引用可能指向不同文本。 |
| 3 | **没有案件内引用/证据图谱** | [`evidence_relations`](/Users/xiaoy/Documents/本地律所/app/storage/db.py#L173) 是人工关系；没有“文档页 → 法条/案例/证据/争点”的统一边表。 | 跨文档矛盾、证据链、法条适用只能靠聊天和人工记忆，难以累积成可复用知识。 |
| 4 | **没有律师标注的阅卷基准** | [`README.md`](/Users/xiaoy/Documents/本地律所/README.md#L222) 明确“独立人工验收集尚未建立”；现有 RAG 240 题为演示语料派生，不是独立标注。 | 改进无法证明是否真的提升；容易用单题涨分替代业务质量。 |
| 5 | **运行轨迹与反馈不足** | [`agent_runs`/`agent_steps`](/Users/xiaoy/Documents/本地律所/app/storage/db.py#L226) 只有节点级输入输出/耗时；[`llm_provenance`](../../app/review/generation.py) 只写入聊天消息，未覆盖 Agent run；没有律师接受/驳回/修改的结构化反馈。 | 失败无法归因到检索、重排、模型或提示；无法形成“bad case → 修复 → 回归”闭环。 |
| 6 | **复核体验仍是文本对话框** | [`app.js`](/Users/xiaoy/Documents/本地律所/app/static/app.js#L657) 的 `openPage` 打开的是页文本对话框，不是 PDF 高亮；没有表格化审阅/检查清单。 | 律师仍要手动对照原文，效率低；AI 建议缺少“逐条接受/驳回”的闭环。 |

### 3.3 与既有路线图的关系

- 本计划**不另立冲突路线图**，而是接续 E01–E33：
  - E31（领域子智能体）、E32（模板结案包）已实施；E33（多人部署）部分完成，Docker/TLS/目标机验收仍待外部环境。
  - 新工作包编号为 **E34–E40**，优先补“证据保真 + 可评测性”，不重复已有权限/审计/导出工作。
- 现有 `docs/runbook/improvement-plan-20260907.md` 的 E33 部署验收仍是发布门槛；本计划把 E40 作为收口，而不是把部署工作当作新增功能。

---

## 4. 改进原则与非目标

### 4.1 原则

1. **证据优先**：任何结论必须能回到页/块/字符区间；无法核验就标 `needs_review`，不猜。
2. **人类在环**：AI 只提议，律师接受/驳回/修改；不接受“模型自审即通过”。
3. **确定性优先**：能用规则/哈希/精确匹配解决的，不交给 LLM 判断；LLM 只做语义辅助。
4. **本地优先**：案件数据不出本机；解析器、评测器、trace 默认本地运行；云服务必须显式配置。
5. **适配器而非平台**：重依赖（Docling/MinerU、Langfuse）放在可选 extra 或独立进程，核心运行时不被绑定。
6. **分开报告**：检索、解析、引用、答案语义、法律适用、性能、人工效率分别报告，不合并成单一分数。
7. **先基线后承诺**：所有数值门槛在基线测量后冻结；不用未测数据做宣传。

### 4.2 非目标

- 不整体替换原生 DAG/Agent 框架；不引入第三个 Agent 运行时。
- 不引入 Qdrant/Postgres/微服务；单机 SQLite 仍是默认。
- 不做自动多 Agent 辩论/自治规划；不训练/微调模型。
- 不做通用 MCP 平台、公共语料社区、完整 DMS。
- 不承诺法律结论正确率、生产 SLA、等保或“AI 可替代律师”。
- 不把 LLM-as-judge 作为唯一质量门槛。

---

## 5. 工作包

### E34 结构化文档模型与解析适配器（P0，M）

**目标**：在不破坏现有页级 API 的前提下，增加“页 → 块”的结构化表示，支持表格、阅读顺序、字符区间和版面坐标，为精确引用和高亮打基础。

**设计要点**

- 新增 `document_blocks` 表：`document_id, page_no, block_no, kind(heading/paragraph/table/figure/footnote/header/footer), text, bbox_json, reading_order, char_start, char_end, table_json, source_hash, parser, parser_version`。
- 保留 `pages.text` 作为页面 canonical text；块级 `char_start/char_end` 必须指向 `pages.text` 的可验证区间。
- 定义 `DocumentParser` 协议：
  - `LegacyTextParser`：现有 `pdftotext`/`python-docx` 路径，保证兼容。
  - `DoclingParser`：作为可选 extra 或独立进程调用；输出统一块模型；失败时回退 Legacy，并记录 `parser=legacy_fallback` 与原因。
  - 可选 `MinerUParser` 作为对照，不默认启用。
- 解析结果带 `parser_version`、`source_hash`、`page_sha256`；页面内容变化时旧块/引用失效。
- 新 API：`GET /api/documents/{id}/blocks?page=`、`GET /api/documents/{id}/pages/{page}/blocks`；旧 `pages` API 不变。
- 前端先不做大改；为 E39 的 PDF 高亮提供数据。

**交付物**

- schema v14 迁移（`document_blocks` + 索引）。
- `app/document_parser.py`（协议 + Legacy 实现 + Docling 适配器骨架）。
- 解析基准脚本 `benchmarks/document_parsing/` 与 30–50 份合成/授权样本。
- 兼容性测试：旧页级引用、检索、导出不回归。

**验收标准**

- 基准集 100% 文档可解析或明确失败；无静默截断。
- 页级文本覆盖率不低于现有基线；表格单元格抽取在合成表格集上达到预先冻结的门槛（基线后确定，建议 ≥0.80）。
- 所有块级 `char_start/char_end` 通过 `page_text[start:end]` 校验；不允许负索引/越界。
- Docling 路径失败时 100% 回退 Legacy，并在 trace 记录原因；不影响主流程可用性。
- 现有完整离线测试通过；新增解析/回退/坐标测试。

**依赖/风险**

- Docling 依赖较重，Python 3.14 兼容性需实测；默认独立进程/可选 extra，避免污染主环境。
- 中文扫描件、印章、手写、跨页表格仍是难点；基准集必须包含这些类型，但不能承诺全部识别。
- 许可：Docling MIT；MinerU 为 Apache-2.0 + 附加条件，需法务确认后再作为可选适配器。

**估算**：M（2–3 周，含基准）。

---

### E35 中文法律引用解析与精确核验（P0，M）

**目标**：把“引用”从字符串检查升级为可解析、可定位、可版本核验的法律引用对象；所有法条/案例引用必须能解析到受控语料，否则 fail-closed。

**设计要点**

- 新增 `app/legal_citations.py`：
  - 解析《法律名称》第X条/第X款/第X项、法释〔年份〕号、指导案例第X号、案号 `(2024)最高法民终X号` 等。
  - 法名别名/简称归一化；版本选择；条款/项层级。
  - 输出 `LegalCitationRef`：`raw, law_name, article, paragraph, item, version_date, status(ok/candidate/needs_review), source_url, corpus_hash, resolved_text, confidence`。
- 与现有 `statutory_retrieval` 合并：显式引用精确匹配；纯文本检索只返回 `candidate`；歧义/缺失/版本不明返回 `needs_review`，绝不以相近条文替代。
- 引用校验升级：
  - `validate_review_answer` 增加 `exact_span` 与 `legal_citation_resolved` 两项。
  - 每条 `[资料N]` 必须能映射到 `document_id/page/block/char_start/char_end`，且 `page_text[start:end] == quote`。
  - 每个法条引用必须解析到 `LegalCitationRef`；无法解析时答案不得标记为 `citation_check=passed`。
- 引用测试集：200+ 条合成/授权引用，覆盖全称/简称/条/款/项/法释/指导案例/案号/错误条号/版本歧义。
- 新增 API：`POST /api/legal-citations/parse`（调试/评测用），不直接暴露给普通用户。

**交付物**

- `app/legal_citations.py` + 单元测试 + 200 条引用测试集。
- `statutory_retrieval` 与 `validate_review_answer` 集成。
- 引用解析报告：解析准确率、解析失败类型、歧义处理、版本分布。

**验收标准**

- 200 条测试集解析准确率 ≥ 预先冻结门槛（建议 ≥0.95；基线后确认）。
- 对错误条号/不存在的法名/版本歧义，100% 返回 `needs_review`，0 次“用相近条文替代”。
- 所有 `citation_check=passed` 的答案，其法条引用 100% 可解析；否则整体不通过。
- 现有法条核验/检索测试不回归；新增对抗测试（伪造条号、跨版本、同名法）。

**依赖/风险**

- 法条语料的版本与现行有效性仍是外部责任；系统只保证“在受控语料中精确定位到某版本”，不保证现行有效。
- 中文法名简称/别名复杂；需要律师参与维护别名表。
- 案号/指导案例解析需要额外规则，先覆盖常见格式，其他标 `needs_review`。

**估算**：M（2–3 周）。

---

### E36 案件引用/证据图谱与人工复核（P1，L）

**目标**：把“文档页、法条、证据、事实、争点”之间的关系变成可查询、可审阅的边；AI 提议，律师接受/驳回，接受后的边才参与检索与分析。

**设计要点**

- 表：
  - `citation_edges(id, case_id, from_type, from_id, to_type, to_id, relation, source_document_id, source_page, source_block, source_char_start, source_char_end, confidence, status(proposed/accepted/rejected), proposed_by, reviewed_by, created_at, reviewed_at, note)`
  - 复用 `evidence_relations`，不重复造关系；新增 `edge_reviews` 审计。
- 节点类型：`document_page`、`document_block`、`evidence`、`fact`、`issue`、`statute_article`、`case_citation`。
- 关系类型：`supports`、`contradicts`、`cites`、`derived_from`、`same_entity`、`amount_matches`、`time_sequence`。
- 生成：从检索结果、专家节点输出、法条解析结果中提取候选边；低置信度进入复核队列。
- 消费：检索/分析只使用 `accepted` 边；`proposed` 仅展示；`rejected` 永不参与。
- API：`GET /api/cases/{id}/citation-graph`、`POST /api/citation-edges/{id}/review`、`GET /api/cases/{id}/review-queue`。
- UI：案件页新增“引用/证据图谱”面板；节点可点开到页/块；边可接受/驳回；支持按关系/状态/文档过滤。

**交付物**

- schema v15：`citation_edges` + 索引 + 审计。
- 边生成器（规则 + 可选 LLM 提议；LLM 只提议，不自动接受）。
- API/UI + 复核队列。
- 图谱查询性能测试（单案 1k 边以内 p95 可接受）。

**验收标准**

- 100% 边有来源 span；无来源的边不得入库。
- `rejected` 边不参与任何检索/分析/导出；测试覆盖。
- 跨案件访问被拒绝；图谱 API 遵守案件权限。
- 复核动作全部进审计；同一案件内无静默覆盖。
- 单案 1k 边、10 并发查询的 p95 有记录（不承诺 SLA）。

**依赖/风险**

- 边生成质量是难点；先规则（法条引用、金额/日期匹配、证据来源）后 LLM。
- 图谱 UI 可能拉高前端复杂度；先做表格 + 简单图，不做通用图编辑器。
- 与 E35 强依赖；E35 未完成前只做“证据-事实”边。

**估算**：L（4–6 周）。

---

### E37 LexVault Review Benchmark 与 Bad Case 库（P0/P1，L）

**目标**：建立律师标注的、按任务分项报告的阅卷基准；让每次改进都能用同一把尺子验证，而不是只看单题涨分。

**任务分类（首版 8 类）**

1. 案件/文档摘要
2. 事实抽取与时间线
3. 人物/主体/角色
4. 证据目录与充分性
5. 矛盾/不一致
6. 金额/日期核对
7. 法律争点与法条引用
8. 引用支持与拒答（含不可回答问题）

**数据集**

- 首版 30 案：20 dev + 10 holdout；每案 ≥8 个任务，合计 ≥240 个样本。
- 来源：合成案优先；真实案必须取得明确授权并脱敏。
- 标注：每案至少 1 名律师；holdout 至少 20% 双人标注 + 分歧裁决。
- 格式：`case_id, task_type, question, gold_answer_points, gold_citations[{document_id,page,block,char_start,char_end,quote}], amounts, issues, refusal, split, source, annotation`。
- 参考答案不进入推理/候选选择；holdout 不用于调参。

**指标（分开报告）**

- 检索：Recall@k、MRR、nDCG、页精度、来源多样性。
- 引用：精确 span 通过率、引用支持率（claim-level）、无法核验率。
- 答案：结构契约、金额/日期一致、拒答正确率、人工评分（完整性/逻辑/正确性/指导性/权威性，参考 ChatLaw 的维度但按本项目定义）。
- 性能：p50/p95 延迟、模型调用数、token、失败/降级率。
- 人工效率：从打开案件到接受/导出答案的时间（先测量，不承诺提升）。

**Runner**

- `benchmarks/legal_review/run.py`：离线/在线两种模式；输出 JSON + Markdown 报告；固定 seed、模型、提示、索引版本。
- CI：离线子集（确定性模型 + hashed-local）跑硬断言；真实模型/律师评审单独报告。
- Bad Case：失败样本保存输入、检索候选、引用、模型输出、trace、分类标签；支持 `--only-cases` 重放。

**交付物**

- 数据集 v1 + 标注协议 + 双人标注记录。
- Runner + 基线报告 + Bad Case 库。
- CI 接入（离线子集，不调用外部服务）。

**验收标准**

- 30 案、8 类任务、240+ 样本冻结；holdout 未在调参中使用。
- 每项指标单独报告；不出现“总分”替代分项。
- 至少 20% holdout 双人标注；分歧有裁决记录。
- CI 离线子集稳定；真实模型报告标记 `measurement_valid` 与适用范围。
- 任何“提升”结论必须绑定源码/模型/提示/索引版本与数据集哈希。

**依赖/风险**

- 标注成本最高；先合成 + 1 名律师，逐步扩展。
- LLM 评委偏差；人工标注为主，LLM 只做 triage。
- 数据授权与脱敏；没有授权就不纳入。

**估算**：L（4–6 周，持续迭代）。

---

### E38 运行轨迹、提示版本与律师反馈闭环（P1，M）

**目标**：让每个答案都能回答“用了哪个模型/提示/索引/检索候选/工具，哪一步失败，律师是否接受”；形成可回归的反馈闭环。

**设计要点**

- 扩展运行轨迹：
  - `run_spans(id, run_id, parent_id, span_type(retrieval/rerank/llm/tool/db/parse), name, status, input_digest, output_digest, model, prompt_version, index_version, input_tokens, output_tokens, latency_ms, error_code, started_at, finished_at)`。
  - `agent_runs` 增加 `provenance_json`；`agent_steps` 保持兼容，UI 可继续读节点级摘要。
- Prompt 版本表/文件：`prompt_versions(name, version, sha256, template, variables_json, model_params_json, created_at)`；每次 LLM 调用记录 `prompt_version`。
- 反馈表：`answer_feedback(id, run_id, message_id, actor, verdict(accept/edit/reject), reason_codes_json, corrected_answer, citation_issue, created_at)`。
- UI：答案下方“接受/修改/驳回”按钮；原因码（引用不对/事实错/漏证据/金额错/法条错/其他）；修改后的答案进入 Bad Case 候选。
- 导出：本地 JSONL + 可选 OpenTelemetry OTLP；默认不接云。
- 隐私：trace 不含原始卷宗全文；只存摘要/哈希/必要片段；沿用现有脱敏与日志纪律。

**交付物**

- schema v16（`run_spans`/`answer_feedback`/`agent_runs.provenance_json`）。
- trace 写入与投影；CLI/页面的 run 详情。
- 反馈 API + UI + Bad Case 导出。

**验收标准**

- 任取一个 run，可重建：问题 → 检索候选 → 重排 → 上下文 → 模型/提示版本 → 工具调用 → 引用 → 最终答案。
- 100% LLM 调用有 `model`、`prompt_version`、`index_version`（如适用）。
- trace 中不出现凭证/完整卷宗；有自动化检查。
- 反馈可查询、可导出、可回放；驳回样本进入 Bad Case 队列。
- 对现有 `agent_steps`/UI 不破坏。

**依赖/风险**

- 轨迹表增长快；需要保留策略/归档（与案件保留策略一致）。
- OpenTelemetry 只做可选导出，不引入云依赖。
- 反馈原因码需要律师参与定义，先小范围试点。

**估算**：M（2–3 周）。

---

### E39 引导式阅卷工作台与 PDF 精确高亮（P2，L）

**目标**：把“聊天 + 引用卡片”升级为“检查清单 + 表格化审阅 + 原文高亮 + 逐条复核”的工作台，让律师不用来回对照。

**设计要点**

- 案由模板：金融犯罪/刑事/经济纠纷/合同等；每类给出检查清单（主体、时间线、金额、证据类型、争点、法条）。
- 工作流状态：`未开始 → 材料整理 → 清单核验 → 分析草稿 → 律师复核 → 结案导出`；每步有负责人/时间/审计。
- 表格化审阅：可定义抽取列（当事人、金额、日期、账户、合同条款、证据类型），批量从文档/流水中填充；人工修正。
- PDF 高亮：基于 E34 的 bbox/char span，用 PDF.js 或现有 PDF 端点做 overlay；点击引用跳到页并高亮；支持多页 span。
- 复核：每条 AI 建议（事实/矛盾/疏漏/法条/金额）可接受/驳回/修改；驳回原因进入 E38 反馈。
- API：`/api/cases/{id}/checklists`、`/api/cases/{id}/review-tasks`、`/api/cases/{id}/tabular-review`；权限沿用 `view/edit/approve/export`。
- UI 先做桌面端；移动端保持现有能力，不强行功能对等。

**交付物**

- 检查清单模板 + 工作流状态机 + API。
- 表格化审阅 MVP（证据矩阵/流水核对）。
- PDF 高亮 MVP。
- 试点案件端到端脚本与录屏/截图证据。

**验收标准**

- 1 个试点案件从导入到导出全流程可完成；每步有审计。
- 引用点击后 PDF 定位到正确页并高亮正确 span（合成样本 100%；真实样本抽样）。
- AI 建议 100% 可接受/驳回/修改；无静默覆盖。
- 权限/案件隔离测试通过；移动端不回归。
- 律师试用反馈记录；不以“UI 好看”替代业务验收。

**依赖/风险**

- 前端工作量大；E34/E36/E38 是前置。
- PDF 坐标/扫描件对齐可能不准；无法定位时明确显示“仅页级定位”，不假装精确。
- 检查清单内容需要律师确认；工程只保证流程与审计。

**估算**：L（6–8 周，分 MVP/增强）。

---

### E40 发布与运维收口（P1/P2，M）

**目标**：完成 E33 遗留的部署验收，确保新能力在真实环境可用、可恢复、可审计。

**内容**

- Docker 镜像实际构建；compose（web/worker/redis）在目标机启动。
- TLS 反向代理、Host/转发头校验、认证模式、CSP/安全头实测。
- 备份→恢复演练：业务库、原件、checkpoint、导出；记录 RPO/RTO 实测值。
- 容量：50 并发读/5 并发模型请求的 p95、错误率、内存/磁盘；给出支持范围，不承诺 SLA。
- 外部审计锚点：至少把关键审计哈希写到独立存储/只追加文件；明确“不是防管理员篡改的完整方案”。
- 监控：请求/队列/模型/OCR/磁盘指标；告警阈值与负责人；日志脱敏检查。

**验收标准**：现有 `docs/runbook/deployment-acceptance.md` 矩阵逐项留证；未完成项明确标注，不用代码合入替代部署验收。

---

## 6. 里程碑与排序

| 阶段 | 时间 | 工作包 | 出口条件 |
|---|---|---|---|
| Phase 0：快赢与基线 | 第 1–2 周 | 精确 span 校验、低置信提示、反馈按钮、10 条对抗用例、20 案基线 | 硬断言通过；基线报告冻结；不改变默认行为 |
| Phase 1：证据保真 | 第 3–8 周 | E34、E35、E37 v0 | 解析基准通过；200 条引用测试通过；20–30 案基准可跑 |
| Phase 2：图谱与轨迹 | 第 9–16 周 | E36、E38、E39 MVP | 图谱/复核队列可用；trace 可重建；PDF 高亮 MVP |
| Phase 3：工作台与收口 | 第 17–24 周 | E39 完整、E37 holdout、E40 | 试点案件端到端；holdout 报告；部署验收矩阵 |
| Phase 4：扩展（可选） | 第 7–12 月 | 多案分析、可选 MCP/API、容量优化 | 有真实需求与数据；不默认开启 |

**排序理由**

- 没有精确引用，图谱和 PDF 高亮都没有可靠输入；所以 E34/E35 先做。
- 没有基准，无法判断 E36/E39 是否真的改善业务；所以 E37 与 E34/E35 并行启动。
- E38 可以并行，因为它是观测/反馈基础设施，不阻塞主链路。
- E39 依赖 E34/E36/E38，放在 Phase 2/3。
- E40 是发布门槛，不能后置到“功能做完再说”。

---

## 7. 度量与验收

### 7.1 硬不变量（必须 100%）

- 被标记为 `citation_check=passed` 的引用，必须通过 `page_text[char_start:char_end] == quote` 校验。
- 所有法条引用必须解析到受控语料，或明确 `needs_review`；不得用相近条文替代。
- `rejected` 的图谱边不得进入检索/分析/导出。
- 跨案件访问必须被拒绝；所有复核/接受/驳回必须进审计。
- trace 不得包含凭证或完整卷宗正文。

### 7.2 质量指标（分项报告）

| 维度 | 指标 | 首版口径 |
|---|---|---|
| 解析 | 页文本覆盖、表格单元格准确率、阅读顺序准确率、失败/回退率 | 基准集人工核对；数值门槛基线后冻结 |
| 检索 | Recall@k、MRR、nDCG、页精度、来源多样性 | 复用现有 RAG 评测口径，新增案件级 |
| 引用 | 精确 span 通过率、引用支持率、无法核验率 | 精确 span 自动化；支持率人工抽样 |
| 答案 | 结构契约、金额/日期一致、拒答正确率、人工评分 | 人工评分为主，LLM 只做 triage |
| 运行 | 模型调用数、token、p50/p95、失败/降级率 | 绑定模型/提示/索引版本 |
| 人工 | 复核时间、接受/驳回率、修改原因分布 | 先测量，不承诺提升 |

### 7.3 证据纪律

- 每个结论标注：已实现 / 离线验证 / 部署验收 / 需律师裁决。
- 每个报告绑定：源码版本/哈希、模型与权重身份、提示版本、索引身份、数据集哈希、运行参数。
- 不用单题涨分、不同数据合并、或未冻结 holdout 证明改进。
- 不把“引用格式正确”表述为“引用支持事实”，不把“检索命中”表述为“答案正确”。

---

## 8. 资源、风险与依赖

### 8.1 资源建议

- 后端/RAG：1 人（E34/E35/E36/E37 主力）。
- 前端：0.5–1 人（E39/E38 UI）。
- 律师/领域专家：0.2–0.3 FTE（引用规则、检查清单、标注/裁决）。
- 测试/运维：0.2–0.3 FTE（基准、CI、部署验收）。
- 硬件：现有 Mac 即可；Docling/MinerU 基准可 CPU 运行，是否需要 GPU 由实测决定。

### 8.2 风险

| 风险 | 影响 | 缓解 |
|---|---|---|
| 解析器质量不达预期 | 引用/图谱输入不可靠 | 先基准后接入；Legacy 回退；人工修正；不承诺全格式 |
| 标注成本高/律师时间不足 | E37 延期 | 合成案先行；20% 双人标注；分批冻结 |
| LLM 评委偏差 | 错误结论 | 人工标注为主；确定性检查优先；评委仅 triage |
| 性能/存储增长 | 体验下降 | 异步解析；分页/归档；预算与压测 |
| 许可/合规 | 不能分发/商用 | Docling MIT 优先；MinerU/Marker 先法务确认；不复制 AGPL/GPL 代码 |
| 范围蔓延 | 交付失焦 | 非目标清单；每阶段只开一个主工作包 |
| 数据隐私 | 案件外泄 | 本地优先；trace 脱敏；云服务显式开关；权限测试 |
| 用户不买账 | 功能闲置 | 先做律师可感知的引用/高亮/清单；试点反馈驱动 |

### 8.3 外部依赖

- Docling（MIT，可选）；MinerU（Apache-2.0 + 附加条件，需确认）；PDF.js（前端，需确认版本/许可）。
- 评测：优先自研 runner；RAGAS/DeepEval 只作为指标设计参考或开发依赖。
- 可观测性：OpenTelemetry + 本地 JSONL；Langfuse 可选，不作为核心依赖。

---

## 9. 未来两周的 10 个动作

1. **精确 span 校验**：在 `_citation` 中增加 `char_start/char_end`，用 `page_text.find(quote)` 校验；不通过则 `citation_check=failed`。这是最小、最高价值的改动。
2. **引用 API 兼容**：旧字段保留，新字段可选；前端先展示“精确/仅页级”标识。
3. **低置信提示**：当检索分数低、引用覆盖不足或法条 `needs_review` 时，在答案上方显示明确警告。
4. **反馈按钮**：答案下方加“接受/修改/驳回 + 原因码”，先存表，不做复杂分析。
5. **10 条对抗用例**：提示注入、跨案提问、伪造法条、错误金额、缺证据、超长输入、恶意文件名、重复引用、版本歧义、越权引用。
6. **20 案基线**：从现有合成案 + 4 个固定问题扩展；记录当前引用/检索/答案指标，作为后续对照。
7. **Run provenance**：给 `agent_runs` 增加 `provenance_json`，至少记录模型、提示版本、索引身份。
8. **评测协议**：写 `docs/benchmarks/legal-review-v1.md`，明确任务分类、标注、holdout、报告口径。
9. **Docling 小试**：在独立 venv/进程中解析 10 份复杂 PDF，比较文本覆盖、表格、阅读顺序、耗时；不接入主流程。
10. **中文引用语法草案**：列出 20 种常见法条/案号写法，和律师确认优先级；作为 E35 输入。

---

## 10. 结论

这轮 GitHub 调研的最大收获不是“找到一个可以替换的框架”，而是确认了三条工程规律：

- **法律 AI 的可信度来自证据链，而不是模型大小。**
- **文档解析和引用定位是上游瓶颈，必须先修。**
- **没有律师标注的评测，就没有真正的改进。**

因此，下一阶段最值得投入的是 **E34 结构化解析 + E35 精确引用 + E37 可评测阅卷**，然后是 **E36 图谱 + E38 轨迹/反馈 + E39 工作台**，最后用 **E40** 完成部署收口。继续保留原生 DAG 和现有权限/审计/法条核验能力；外部项目只借设计，不引入平台级依赖。

本计划书不声称任何能力已实施，也不承诺法律正确率、SLA 或生产就绪。每阶段的结论必须以对应源码、测试、基准报告和人工评审为准。

---

## 附录 A：GitHub 样本与许可（截至 2026-10-18）

| 项目 | Stars | 许可 | 用途 |
|---|---:|---|---|
| [OpenContracts](https://github.com/Open-Source-Legal/OpenContracts) | 1,509 | MIT | 引用图谱、人工标注、PDF 定位 |
| [ChatLaw](https://github.com/PKU-YuanGroup/ChatLaw) | 7,624 | AGPL-3.0 | 中文法律 SOP/多 Agent、评测维度 |
| [RAGFlow](https://github.com/infiniflow/ragflow) | 92,415 | Apache-2.0 | 模板化切分、知识编译、引用可视化 |
| [Kotaemon](https://github.com/Cinnamon/kotaemon) | 25,808 | Apache-2.0 | PDF 引用高亮、低相关警告、问题分解 |
| [Docling](https://github.com/docling-project/docling) | 69,052 | MIT | 结构化解析、版面/表格/阅读顺序 |
| [MinerU](https://github.com/opendatalab/MinerU) | 81,971 | Apache-2.0 + 附加条件 | 中文复杂 PDF 解析对照 |
| [eyecite](https://github.com/freelawproject/eyecite) | 291 | BSD-2-Clause | 引用抽取/聚合/标注架构 |
| [RAGAS](https://github.com/vibrantlabsai/ragas) | 16,056 | Apache-2.0 | RAG 指标与合成测试 |
| [DeepEval](https://github.com/confident-ai/deepeval) | 18,934 | Apache-2.0 | 评测即测试、Agent 轨迹指标 |
| [Langfuse](https://github.com/langfuse/langfuse) | 35,990 | MIT（`ee` 除外） | trace/数据集/Prompt 版本/反馈 |
| [Phoenix](https://github.com/Arize-ai/phoenix) | 11,896 | Elastic License 2.0 | 仅参考 OpenTelemetry trace 设计，不嵌入 |
| [promptfoo](https://github.com/promptfoo/promptfoo) | 26,107 | MIT | 声明式评测、红队清单 |
| [Inspect AI](https://github.com/UKGovernmentBEIS/inspect_ai) | 3,036 | MIT | 样本/评分器/日志模型 |
| [Paperless-ngx](https://github.com/paperless-ngx/paperless-ngx) | 46,832 | GPL-3.0 | DMS 工作流/保存视图/处理状态 |
| [Tabular_Review](https://github.com/jamietso/Tabular_Review) | 85 | MIT | 表格化法律审阅 |
| [AI-WorkDeck](https://github.com/AI-WorkDeck/aiworkdeck) | 167 | AGPL-3.0 | 可审阅 Agent 编辑/来源关联 |
| [LegalBench](https://github.com/HazyResearch/legalbench) | 647 | 未声明 | 任务分类/标注协议 |
| [CUAD](https://github.com/The-Atticus-Project/cuad) | 571 | 未声明 | 合同条款抽取任务设计 |

> 星数与许可来自 GitHub API/README，可能变化；使用前需重新核对。AGPL/GPL/ELv2 项目只借鉴设计，不复制代码。

## 附录 B：当前代码地图

| 区域 | 文件 | 本计划关注点 |
|---|---|---|
| 文档解析 | [`app/documents/ingest.py`](../../app/documents/ingest.py) | E34 结构化解析/表格/坐标 |
| Agent 与引用 | [`app/review/agents.py`](/Users/xiaoy/Documents/本地律所/app/review/agents.py#L53) | E35 精确引用/法条解析 |
| LangGraph 运行时 | [`app/review/langgraph_agents.py`](/Users/xiaoy/Documents/本地律所/app/review/langgraph_agents.py#L91) | 保持兼容；不替换 |
| 数据库/迁移 | [`app/storage/db.py`](/Users/xiaoy/Documents/本地律所/app/storage/db.py#L158) | E34/E36/E38 schema |
| 检索/法条 | [`app/retrieval/rag.py`](/Users/xiaoy/Documents/本地律所/app/retrieval/rag.py) / `app/retrieval/statutory_retrieval.py` | E35/E36 输入 |
| API | [`app/main.py`](/Users/xiaoy/Documents/本地律所/app/main.py#L1391) | 新端点/权限/审计 |
| 前端 | [`app/static/app.js`](/Users/xiaoy/Documents/本地律所/app/static/app.js#L657) | E39 PDF 高亮/表格/复核 |
| 评测 | [`docs/benchmarks/README.md`](/Users/xiaoy/Documents/本地律所/docs/benchmarks/README.md) | E37 基准/报告口径 |
| 权限/审计 | [`app/core/security.py`](/Users/xiaoy/Documents/本地律所/app/core/security.py) | 所有新端点复用 |
| 既有计划 | [`docs/runbook/improvement-plan-20260907.md`](/Users/xiaoy/Documents/本地律所/docs/runbook/improvement-plan-20260907.md) | E31–E33 已落地部分 |
| 企业路线图 | [`docs/runbook/enterprise-improvement-report.md`](/Users/xiaoy/Documents/本地律所/docs/runbook/enterprise-improvement-report.md) | E01–E33 口径 |

## 附录 C：建议的 schema 草图

```sql
-- E34
CREATE TABLE document_blocks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  page_no INTEGER NOT NULL,
  block_no INTEGER NOT NULL,
  kind TEXT NOT NULL,
  text TEXT NOT NULL DEFAULT '',
  bbox_json TEXT NOT NULL DEFAULT '[]',
  reading_order INTEGER NOT NULL DEFAULT 0,
  char_start INTEGER,
  char_end INTEGER,
  table_json TEXT NOT NULL DEFAULT '[]',
  source_hash TEXT NOT NULL DEFAULT '',
  parser TEXT NOT NULL DEFAULT '',
  parser_version TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  UNIQUE(document_id, page_no, block_no)
);

-- E36
CREATE TABLE citation_edges (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  case_id INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
  from_type TEXT NOT NULL,
  from_id TEXT NOT NULL,
  to_type TEXT NOT NULL,
  to_id TEXT NOT NULL,
  relation TEXT NOT NULL,
  source_document_id INTEGER REFERENCES documents(id) ON DELETE CASCADE,
  source_page INTEGER,
  source_block INTEGER,
  source_char_start INTEGER,
  source_char_end INTEGER,
  confidence REAL NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'proposed',
  proposed_by TEXT NOT NULL DEFAULT '',
  reviewed_by TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  reviewed_at TEXT NOT NULL DEFAULT '',
  note TEXT NOT NULL DEFAULT ''
);

-- E38
CREATE TABLE run_spans (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
  parent_id INTEGER,
  span_type TEXT NOT NULL,
  name TEXT NOT NULL,
  status TEXT NOT NULL,
  input_digest TEXT NOT NULL DEFAULT '',
  output_digest TEXT NOT NULL DEFAULT '',
  model TEXT NOT NULL DEFAULT '',
  prompt_version TEXT NOT NULL DEFAULT '',
  index_version TEXT NOT NULL DEFAULT '',
  input_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  latency_ms INTEGER NOT NULL DEFAULT 0,
  error_code TEXT NOT NULL DEFAULT '',
  started_at TEXT NOT NULL,
  finished_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE answer_feedback (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
  actor TEXT NOT NULL,
  verdict TEXT NOT NULL,
  reason_codes_json TEXT NOT NULL DEFAULT '[]',
  corrected_answer TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
```

> 以上仅为设计草案；实际迁移需按现有 `app/storage/db.py` 的有序迁移机制、回滚策略、索引与保留策略评审后实施。schema 版本号（v14/v15/v16）是相对当前 v13 的建议顺序，最终以实际合并顺序为准。
