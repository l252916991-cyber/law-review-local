# ForgeHarness 迁移台账

编制日期：2026-10-21（M0 启动）

依据：[LexVault → ForgeHarness 框架替换计划书](../architecture/forgeharness-migration-plan-20261021.md)。本台账是计划书第 5 节要求的迁移记录入口：登记全部实际项目、发行物与版本、里程碑状态、既有失败与 Git 前置条件。计划书第 2 节要求"两份计划中的同一工作只实施和计费一次"，本台账同时登记框架侧 [REMAINING_WORK_PLAN_2026-10-20](/Users/xiaoy/Documents/多agent协作/docs/REMAINING_WORK_PLAN_2026-10-20.md) R0–R8 的对应状态，避免重复实施。

台账只记录事实与状态，不代替验收结论。所有摘要均为 SHA-256。

2026-10-23 领域接入执行更新：[实施记录](domain-integration-execution-20261023.md)、[设计](domain-integration-design-20261023.md)。D00 来源记录、D01 设计及 D02/D03 隔离领域写入/持久图已完成；本机三版各 911/0/3，真实进程故障与领域引用校验通过。用户已确认 500 页/200 万字符超限拒绝，且仅授权合成材料调用已有本机模型服务。生产候选适配、实际后端/真实材料 G2 及正式路由仍单独验收。

2026-10-23 Git 与后续计划更新：律所已按用户授权建立本地 `main` 基线 `54b7d7c9d4eee2e3c3ade9d633324a3519231f0b`，仓库级作者 `Codex <codex@local.invalid>`；范围及验证见 [Git 初始化记录](git-initialization-20261023.md)。此前“无 Git”保留为历史状态，本次不推定框架侧 Git 条件已解决。新编[下一步计划书](domain-integration-plan-20261023.md)安排 D00–D07：来源记录、领域写契约与持久图、模型治理、双对照和验收收口；本次仅编制文档，E12 全范围、E13 实际领域链路、E07/G2、E14 状态不变。

2026-10-23 宿主接入更新：[实施记录](host-integration-execution-20261023.md)、[设计](host-integration-design-20261023.md)。schema v16、六类写入与持久回执、原子映射、实际 wheel 的合成持久图/恢复/投影已交付隔离原型；H06 合成两类对照各 8/8 一致，来源校验 7 成功/1 无来源拒绝。生产容量仍待确认，E12 全范围、E13 实际领域图及 E07/G2 未验收，正式入口仍未切换。最终验证与源码身份以本次实施记录及私有证据为准。

2026-10-23 R2 更新：[R2 实施记录](r2-execution-20261023.md)。P00–P07 离线工程完成，E09/E10 适用框架契约及 E11/G3 通过；当前绑定 **0.8.3**，92/92 门禁、框架源码/干净副本各 1178 测试及宿主三版各 828/0/3 通过。律所持久恢复、业务写入与正式入口仍留 E12–E14，E07 及业务资格未验收。

2026-10-23 上一批：[下一批实施记录](next-batch-execution-20261023.md)。N00–N07 离线工程收口时绑定 0.7.1；E08/R1 与 E06 工程边界通过，E05 离线接口完成但整体仍进行中。下文旧候选记录保留历史身份。

2026-10-22 更新：[M2 离线工程接入记录](m2-engineering-20261022.md)。已实现独立框架进程及原专家/检索对照，合成验证通过；M2 真实材料与模型验收仍未完成。下文保留各批次历史证据，不能把早期“未开始”或审计失败记录当成最新总体状态。

## 1. 项目登记

| 项目 | 路径 | 角色 | 当前版本 | 源码身份（本轮 M0 登记） | 状态 |
| --- | --- | --- | --- | --- | --- |
| LexVault「本地律所」 | `/Users/xiaoy/Documents/本地律所` | 律所宿主（第一迁移对象） | lexvault-local 0.3.0 | 发布允许清单口径 tree_digest，见 §2（M0 收口 `852b73d4…` / M1 收口见 §2 表）；另有 2026-09-16 源码归档 `lexvault-v0.3.0-source.zip`（sha256 `25752ff03d48…`，完整值见基线记录） | M0/M1 已收口；M2 离线工程接入通过，真实材料/模型未验收（2026-10-22） |
| ForgeHarness「多agent协作」 | `/Users/xiaoy/Documents/多agent协作` | 统一 Agent 运行底座 | **0.8.3（SDK API v1）** | `a124764a94b5c519f9efa051b682cae782913028557db24bd8abe54e56ecb6cf`（`reports/release-0.8.3-r1/source.json`） | **92/92 工程门禁，源码/干净副本各 1178 测试；R1/R2 本批框架工程通过；真实业务/生产未验收** |
| ForgeCommerce「电商agent」 | `/Users/xiaoy/Documents/电商agent` | 第二实际应用（M7 验证对象） | forgecommerce 0.1.0 | 参考性 tree_digest `ac48da0ec971e73d82d4e13031d9555b7715ea3667c8fd10b958982257d13e0b`（79 文件，口径：根文件+src/tests/scripts/docs+vendor；权威身份口径由该项目在 M7 自行确认） | 已绑定框架 0.5.0 wheel（见 §2），未开始 M7 |
| ERP-AGENT-open-source-main | `/Users/xiaoy/Documents/ERP-AGENT-open-source-main` | 待确认 | 未知（无 uv.lock，requirements.txt 形态） | 未登记 | 疑似开源参考项目而非迁移对象；待项目负责人确认后登记或排除 |
| AI视频 | `/Users/xiaoy/Documents/AI视频` | 待确认 | 不适用 | 未登记 | 内容制作目录（脚本/提示词/输出），非 Agent 运行项目；待确认后排除 |
| deepseek-harness | `/Users/xiaoy/Documents/deepseek-harness` | 开发工具 | 不适用 | 未登记 | DSH 开发工具检出，非业务迁移对象；待确认后排除 |

登记规则（计划书 §1）：其他实际项目在启动阶段登记后才进入排期；"两个应用通过不能代替全部项目通过"。上表"待确认"项由项目负责人逐项确认迁入或排除，确认结果记入本台账。

负责人登记（计划书 §8，角色可由同一人承担，自审与独立评审须如实区分）：

| 角色 | 承担人 | 备注 |
| --- | --- | --- |
| 项目负责人 | 待用户确认 | 冻结范围/数据使用/预算/验收指标，批准切换窗口 |
| 框架维护者 | 待用户确认 | R1/R2 公共执行与恢复、版本契约（R7）、共同门禁（R8） |
| 律所集成人员 | 待用户确认 | 宿主端口、落库、前端与部署适配 |
| 验证/审查人员 | 待用户确认 | 隔离测试、故障注入、干净复验 |
| 律师/业务评审人 | 待用户确认 | 证据支持、法律质量、实际流程验收；未落实前业务质量保持"未验收" |

## 2. 发行物与版本记录

| 发行物 | 版本标签 | sha256 | 构建源码身份 | 验证证据 | 资格状态 | 绑定方 |
| --- | --- | --- | --- | --- | --- | --- |
| **forgeharness-0.8.3-py3-none-any.whl（R2 最终候选）** | **0.8.3** | `1d5880ed2ba1cabcb20b0970a55727bcca7b68f9b4beace70ce7600d58ba7555` | `a124764a94b5c519f9efa051b682cae782913028557db24bd8abe54e56ecb6cf` | `reports/release-0.8.3-r1/verification/summary.json`：92/92、各 1178 测试、纯分支 85.94%；43 项 R2 专项 | 适用框架 G3 工程通过；真实模型/业务/生产未验收 | **LexVault 当前绑定；宿主仍为只读接入，发布仅交付 0.8.3** |
| forgeharness-0.8.0 / 0.8.1 / 0.8.2（未最终放行） | 历史候选 | 各原报告记录 | 各独立冻结清单 | `reports/release-0.8.0-r1` 88/92；0.8.1、0.8.2 自动 92/92 后分别发现 R2-005 / R2-006；原报告与复现保留 | 已由 0.8.3 修复并取代；不能以旧自动门禁作为最终放行 | 无实际宿主绑定 |
| **forgeharness-0.7.1-py3-none-any.whl（R1 历史候选）** | **0.7.1** | `1db08d3d0dece82b7af3dabd306c6a6dca9b11e1f021b16e0b40e72d438c1eec` | `838f2cd2e3156570e48f9d5a52ee6458d1d9aa4e02b0b9c1c5d1a54c18a58177` | `reports/release-0.7.1-r1/verification/summary.json`：87/87、各 1135 测试 | 当时 R1 工程通过、R2 未实现 | 上一批 LexVault 绑定；本机保留 wheel/环境，正式允许清单已换为 0.8.3 |
| forgeharness-0.5.0-py3-none-any.whl（2026-10-17 资格化构建） | 0.5.0 | `58579893b83368a36d0251393ef7c2d742b947bf184c434558987972732f56fa` | `1312bcf11a8ad360…`（378 文件） | `多agent协作/reports/request-preflight-2026-10-17/verification/runs/20261017T075541Z-aa788289/` | engineering_slice_qualified（2026-10-17）；business 未评 | ForgeCommerce `vendor/`（`docs/FRAMEWORK_BINDING.json`） |
| forgeharness-0.5.0-py3-none-any.whl（2026-10-21 R0 后候选构建） | 0.5.0（历史标签冲突方之一） | `8275baba6a08bb5968b42111c6d5c074f608f991f7f907c7c45184624fe0ee81` | `783e5fbd7c9ab13f…`（420 文件） | `多agent协作/reports/framework-next/verification/runs/20261021T054049Z-ff445d75/`（87 门禁 81 过 6 失败，失败全部为可选依赖漏洞审计） | qualified=false；状态审查 I-06 判定不满足正式绑定条件 | ~~LexVault vendor/~~ **已于同日换绑 0.6.0**；权威副本保留在框架验证 run 目录 |
| forgeharness-0.5.0.tar.gz（同上构建） | 0.5.0 | `57cfbf2ada02c5b8c11c99ab6e92a7f9cc84a94421a34c11fbcc26cb0294d497` | 同上 | 同上 | 同上 | — |
| **forgeharness-0.6.0-py3-none-any.whl（2026-10-21 r1 候选构建）** | **0.6.0** | `69ce604c51325085ae179917605c9479e423c1d437970758733d4f580e367a16` | `13d50704639062208558cc6b94670808d071f6ad1c0a6b1f154380d1d458f39c`（420 文件，`reports/release-0.6.0-r1`） | `多agent协作/reports/release-0.6.0-r1/verification/runs/20261021T125950Z-8663a4b9/`：**87/87 门禁全过**、qualified=true、1127 测试 0 失败（源码+干净副本）、纯分支 86.74%、依赖审计修复（aiohttp 3.14.5 / certifi 2026.10.19 / pillow 13.0.0 + 对照环境锁；pyproject pillow 约束放宽 >=13,<14；唯一豁免仍为 NLTK FH-019） | 工程资格化；business_qualified=false（R6 真实模型评测未做，设计边界） | **LexVault `vendor/`（当前绑定，`vendor/FRAMEWORK_BINDING.json`）；发布包含该 wheel（B3 路径）；运行路径尚未接线，M2 起使用**。首轮诊断候选 `release-0.6.0`（83/87，wheel `de16f0f7…`）仅作内部证据，不得对外引用 |
| forgeharness-0.6.0.tar.gz（同上构建） | 0.6.0 | `cbea0088924398feac117ae17b1feae4d95126325721404ddbeb0d8f663c29cd` | 同上 | 同上 | 同上 | — |
| lexvault-v0.3.0-source.zip（2026-09-16 源码归档） | 0.3.0 | `25752ff03d486c77679b5f0bd0df39670f9928defbf06b0448f460caae78475f` | 归档内 196+ 文件（早于 10 月的 app/ 改动） | 根目录归档本体 | 历史回滚参考 | — |

**版本标签冲突（已解决，2026-10-21 晚）**：曾有两个内容不同的 wheel 同标 0.5.0（电商绑定 `58579893…` 与 `8275baba…`）。框架侧已发布 **0.6.0**（`69ce604c…`，87/87 门禁）作为区分版本；LexVault 已换绑 0.6.0，ForgeCommerce 保持其 0.5.0 绑定至 M7。身份指认规则不变：任何版本标签必须附带 sha256 + 源码身份。框架 R7 的完整版本策略（API/事件/持久状态版本发布规则）仍待成文。

LexVault 源码身份口径：`scripts/migration_source_manifest.py`（与发布允许清单 `scripts/package_release.py source_files()` 同一文件集，算法与框架侧 `benchmarks/framework_snapshot.py` 一致）。M0 冻结值见 [m0-baseline-20261021.md](m0-baseline-20261021.md)；每个里程碑收口时重冻并在此登记：

| 里程碑 | LexVault 源码清单（tree_digest 以证据文件为准） | 冻结时间 | 备注 |
| --- | --- | --- | --- |
| M0 | `output/migration/m0-baseline-20261021/lexvault-source-manifest.json`（收口值 `852b73d404af364fd47d4f1e7b5f216682ee09b9b2e2cf10823156500319b75e`，201 文件） | 2026-10-21 | 含迁移台账、基线记录与清单工具；M1 代码改动前状态；4 个被改文件的原始副本经此清单逐字节校验（pre-change/） |
| M1 | `output/migration/m1-regression-20261021/lexvault-source-manifest.json`（收口值 `5089ecf83f8b99b1568cf68daf5f527ed2365793ae9e4e4056f6b2fcf58ece77`，206 文件） | 2026-10-21 | 含契约模块、旧运行时适配器、schema v14、43 项新测试、M1 契约文档与 vendor 绑定记录（0.5.0 初冻） |
| M1+状态审查 A/B 批次 | `output/migration/a-batch-closing-20261021/lexvault-source-manifest.json` | 2026-10-21 | 协议 v2/事件 v2/schema v15、恢复的冻结实验配置、部署验证脚本、vendor 换绑 0.6.0 并入发布允许清单、状态审查计划书入库；收口全量回归 755/0/3 |

## 3. 兼容矩阵

| 应用 | 应用版本 | 框架发行物（sha256 前 8 位） | 框架源码身份（前 8 位） | Python | 验收状态 |
| --- | --- | --- | --- | --- | --- |
| ForgeCommerce | 0.1.0 | `58579893`（0.5.0，2026-10-17） | `1312bcf1` | >=3.12,<3.14 | 工程切片资格化；业务未评；M7 时升级 0.6.0 线 |
| LexVault | 0.3.0 | **`1d5880ed`（0.8.3，92/92）**；独立只读框架进程 | `a124764a` | 宿主 3.11/3.12/3.14 已复验；框架 3.12 | 协议/事件/结果 v2/v2/v1；三版各 828/0/3、发布重建通过；宿主持久恢复/写入/正式入口待 E12–E14 |

版本约束事实：LexVault 宿主声明 `>=3.11,<3.15`，ForgeHarness 声明 `>=3.12,<3.14`；计划书 §2 决定首期使用独立 Python 3.12 进程与锁定环境（本机两侧 venv 均为 3.12.13），同进程嵌入另行验证。R3 要求补 3.13 安装矩阵。

## 4. 既有失败与未决项登记（M0，2026-10-21）

以下为本轮实际执行结果，与历史报告区分。详细命令与环境见 [m0-baseline-20261021.md](m0-baseline-20261021.md)。

### LexVault（本机 Python 3.12.13，隔离数据目录，离线）

- 工程门禁：uv sync --locked + pip check、两份 requirements 导出一致性、doctor、compileall、ruff、mypy（38 文件）、发布清单 dry-run 全部通过。
- 离线测试（含 CI 三个工程脚本，node v24.21.0 可用时）：**682 passed / 3 failed / 3 skipped**，覆盖率 86.31%（门槛 70%）。
- 既有失败 3 项，同一根因：`tests/test_statutory_experiment.py` 的 3 个测试读取 `benchmarks/statutory-experiment-v1.json`，该冻结实验配置在本机不存在（不在工作树、不在 v0.3.0 源码 zip、不在任何 output 快照；全盘检索无果）。~~本机无法恢复~~ **【2026-10-21 晚已解决，见 §4b B1 处置】**：从公开原仓库经 GitHub API 恢复，3 项测试转绿。
- 既有跳过 3 项：2 × 冻结法条语料未检出（test_corpus_audit / test_corpus_canonical），1 × `mlx_lm` 未安装（test_sft_pool，可选依赖）。
- 环境偏差：CI 矩阵为 Python 3.11/3.14 + node 22；本机为 Python 3.12.13 + DSH 附带 node v24.21.0（系统 PATH 无 node，首轮 4 个 test_mobile_ui 测试因缺 node 失败，补 node 后全过）。本机结果不能替代 CI 矩阵结论。

### ForgeHarness（引用 2026-10-21 13:40–13:46 框架自有验证 run `20261021T054049Z-ff445d75`，非本轮重跑）

- 候选源码身份 `783e5fbd…` 与验证时活树一致（`source_unchanged: true`）。
- 87 项门禁：**81 通过 / 6 失败**。通过的含：全量测试 1127 项 0 失败（source 与 clean 副本各一遍）、纯分支覆盖 86.74%（≥85%）、format/lint/mypy、锁一致性、业务契约 120 案（合成，measurement_valid=false）、框架对比 33/33、wheel/sdist 构建与最小安装、全部 demo 冒烟。
- 失败 6 项全部为 pip-audit 依赖漏洞审计（all-extras ×2、deepagents 对照 ×2、pydantic 对照 ×2），共涉及 19 条通告记录：
  - `aiohttp==3.14.3`：CVE-2026-111063/111064/111065/111066/111748/111749/111750、GHSA-2g87-pp6c-29x5（修复版本 3.14.4；经 qdrant-client/llama-index 等可选 extra 引入）
  - `certifi==2026.7.22`：PYSEC-2026-4320（修复版本 2026.10.19；同时命中 deepagents/pydantic 两个对照环境审计）
  - `pillow==12.3.0`：CVE-2026-112395/112397/112400/112404/112895/112896/112897/112899（修复版本 13.0.0；knowledge extra）
  - 核心运行时依赖审计（audit-core）通过；已接受例外仍只有 NLTK PYSEC-2026-3740（FH-019）。这些是 2026-10-17 资格化之后新发布的通告，处置（升级锁定或按 FH 流程豁免）属框架维护者 R8 审查范围，在处置前该候选 `qualified=false`，不得称为"完整资格化发行物"。
- R0 发现处置状态（`reports/framework-next/r0/review.json`）：R0-001 全树格式化（fixed_pending_full_verification）、R0-002 测试数量口径（fixed）、R0-003 CI 发布入口指向旧候选（high，fixed_pending_full_verification）、R0-004 文档与实现矛盾（fixed）。今日验证 run 即 R0 修复后的全量验证，除上述审计外全绿。
- **F1c-2（R1 共享工具执行器 / R2 自定义编排挂起与恢复）未开工**：当前 RunKernel 自定义编排为 one-shot、只读（写/审批工具抛 KernelToolDenied），`Harness.approve/resume` 对自定义编排显式抛 ScopeError。这是 M3 的硬依赖；M2 只读接入不受阻。

### 跨项目未决项

| 未决项 | 影响 | 处置路径 |
| --- | --- | --- |
| 两侧均无 `.git`。**LexVault 原仓库已核实**：`github.com/l252916991-cyber/law-review-local`（公开；HEAD `58dacddb…`=2026-09-16 v0.3.0 交付合并，与本机 zip 同日；策略基线锚点提交 `b2856be5…` 存在于该仓库；本机可达 api.github.com/raw.githubusercontent.com，github.com:443 git 协议当前不通） | 提交历史恢复受网络限制；本地树领先仓库 HEAD（10 月改动未推送） | 计划书 §2：Git 基线恢复/初始化仍需负责人授权与 git 协议连通；在此之前以内容寻址清单 + pre-change 文件级回滚基线为准，不伪造提交哈希 |
| ~~`benchmarks/statutory-experiment-v1.json` 缺失~~ **已恢复（2026-10-21）** | ~~3 个既有失败~~ 已消除 | 见 §4b B1 处置行 |
| ~~框架版本标签冲突（两个 0.5.0）~~ **已解决** | ~~绑定歧义风险~~ | 0.6.0 本地发行物已构建并换绑（未声明远端发布）（§2）；R7 完整版本策略仍待成文 |
| ~~6 项依赖审计失败~~ **已解决** | ~~候选发行物 qualified=false~~ | 根锁 + 两个对照环境锁升级（aiohttp 3.14.5 / certifi 2026.10.19 / pillow 13.0.0），0.6.0 r1 验证 87/87、qualified=true；无新增豁免 |
| 负责人/评审人未落实 | 业务质量与切换窗口无法验收 | 项目负责人确认 §1 角色表 |

### 4b. 状态审查问题处置（2026-10-21 晚，依据 [status-and-improvement-plan-20261021.md](status-and-improvement-plan-20261021.md)）

| 编号 | 级别 | 处置状态 | 证据/说明 |
| --- | --- | --- | --- |
| I-01 重复请求缺少原请求绑定 | P0 | **已修复** | schema v15 绑定列 + `compute_request_digest`（方案 lexvault-request-binding/1）；重放四态判定；返回旧关联前对实际映射 run 的案件重核验授权；冲突消息零泄露。测试：跨案/换问题/换运行时/换参数/换主体/撤权/篡改映射/v14 无摘要行/崩溃窗口/并发（tests/test_runtime_adapter.py RequestBindingTests 等） |
| I-02 可信身份契约未落实 | P0 | **已修复** | actor 断言与实时 `current_principal()` 逐字段比对（`IdentityMismatchError`）；作者经 `actor_name()` 归一化进入协调器与摘要；token 模式身份丢失 → `AccessDeniedError` 不回退匿名；租户白名单显式拒绝。测试：TrustedIdentityTests（含记忆作者=主体名断言） |
| I-03 事件去重与可变节点冲突 | P1 | **已修复** | `RunTrace.trace_revision` 内容寻址修订号；RUN_EVENT_VERSION=2 快照语义（身份 = (run_id, revision, seq)）；消费者规则文档化（m1-contracts §3）。测试：TraceRevisionTests（失败→首读→重启重读→恢复→再读全链） |
| I-04 离线基线缺冻结实验配置 | P0 | **已解决（可信恢复）** | 从公开原仓库 `l252916991-cyber/law-review-local` 经 GitHub API 恢复 `benchmarks/statutory-experiment-v1.json`：459 字节，blob `abb8a84d81969d6676596ebee63068b23e4fc6e2`，内容 sha256 `32316c2c09b2eb0862b01c6aacc48a339b6e144f3299f8d00f838a15e4208864`，最后修改提交 `e6591521…`（2026-09-10 "feat(statutory): 添加版本感知混合检索与四组配对实验"）；仓库 HEAD `58dacddb…`（v0.3.0 交付合并）与策略基线锚点 `b2856be5…` 均核实存在。恢复后 `test_statutory_experiment.py` + `test_statutory_hybrid_experiment.py` 23 项全过（指纹/身份断言未改动，证明为原件）。已纳入发布允许清单（EXACT_FIXTURES） |
| I-05 源码包不能重建框架绑定 | P0(交付前) | **已解决** | 交付路径（唯一正式）：精确 wheel `vendor/forgeharness-0.6.0-py3-none-any.whl` + `vendor/FRAMEWORK_BINDING.json` 纳入发布允许清单（EXACT_FIXTURES 精确文件，不整包 vendor/）；`scripts/verify_deployment_release.py` 重建验证 **PASS**（211 文件发布包→解压→摘要校验→隔离 venv 安装→SDK/lexvault/契约冒烟，零开发机路径依赖），证据 `output/migration/b3-delivery-20261021/deployment-verification.json`；覆盖范围声明见 m1-contracts §6 |
| I-06 发行身份冲突及框架审计未通过 | P0(正式绑定前) | **已解决** | 0.6.0 发行（tree `13d50704…`，wheel `69ce604c…`）：87/87 门禁、qualified=true、1127 测试双副本零失败；依赖修复 aiohttp 3.14.5 / certifi 2026.10.19 / pillow 13.0.0（pillow 约束放宽 >=13,<14）+ 两个对照环境锁 certifi；evals/comparison/protocol.json forge arm 版本重封印；首轮 83/87 诊断候选 `release-0.6.0` 原样保留；LexVault vendor 已换绑，电商 0.5.0 绑定不受影响（M7 再升级） |
| I-07 真实运行入口未统一 | P1 | 按计划属 M4 | 本轮未接线 HTTP/UI；review_jobs 猴子补丁未动（移除属 M4）；A 批次契约/适配器已为其备好接口 |
| I-08 只读迁移不能只关记忆保存 | P1 | 工程隔离已落实，完整 M2 验收进行中 | 2026-10-22 单案件快照/只读源连接、原节点与独立框架进程、源逻辑摘要不变验证通过；真实案件/模型和完整宿主端口仍待验收，见 [M2 记录](m2-engineering-20261022.md) |
| I-09 自定义编排写入及恢复缺失 | P1 | 阻塞等待框架 R1/R2 | 台账 §5 既有登记；不在律所侧旁路再造 |
| I-10 状态文档与版本控制未收口 | P2 | 部分推进 | 原仓库来源已核实（§4 未决项行）；Git 基线恢复仍待授权与 git 协议连通；总计划"待实施/拟新增"字样待 M2 收口时统一修订 |

### 4c. 执行计划任务登记（[execution-plan-20261022.md](execution-plan-20261022.md)，2026-10-22 起）

| 任务 | 状态 | 交付物/证据 |
| --- | --- | --- |
| E01 项目—AI 入口总表 | **完成（含 3 项未决）** 2026-10-22 | [e01-entry-inventory-20261022.md](e01-entry-inventory-20261022.md)；机械核查 62+2 条路由（`output/migration/e01-entry-inventory-20261022/routes.txt`）；律所 23 项 AI 入口 + 电商 9 类入口 + 11 项旁路登记；ERP/AI视频/deepseek-harness 列为未决（PM，E24 前） |
| E02 摘要/发行物/兼容矩阵 | **完成（Git 部分未解决）** 2026-10-22 | [e02-baseline-compatibility-20261022.md](e02-baseline-compatibility-20261022.md)；E02 时点律所摘要 `1dc1b323…`（225 文件）；框架 r1 `13d50704…`/wheel `69ce604c…`/锁 `9677f461…`；电商 git HEAD `6a752d84…`（本人复核）；可重建与不可重建项逐条声明；两侧 Git 未授权、未初始化 |
| E03 验收参数冻结 | **完成（8 项待负责人）** 2026-10-22 | [e03-acceptance-parameters-20261022.md](e03-acceptance-parameters-20261022.md)；已冻结 30 项，待确认 8 项均带责任人角色与截止节点；不降低门槛声明 |
| E04 HostReadPort 七方法 + 框架版 RuntimeControl | **完成** 2026-10-22 | [e04-host-ports-20261022.md](e04-host-ports-20261022.md)；[app/host_ports.py](../../app/host_ports.py)（类型化读端口：逐调用授权与版本检查、边界与显式拒绝、访问记录）；[app/forge_runtime.py](../../app/forge_runtime.py)（`FrameworkReadonlyControl`：诚实能力声明 + `start` + inspect/resume/status 显式拒绝 + 未知结果对账）；M2 试点会话改经端口；30 项新测试（`tests/test_host_ports.py`、`tests/test_forge_runtime.py`）；**805 passed / 0 failed / 3 skipped**，覆盖率 86.77%，ruff/mypy(41)/compileall/node/发布清单全过；收口清单 `2fb2794363cf45bb51f7adb2ffb35f0b42aad947484b665240e833805ebb2ae2`（228 文件；相对 M2 收口 +9/−0/~4，逐项见 `output/migration/e04-host-ports-20261022/diff-vs-m2.txt`；适用范围：E04 验证与文档收口后的内容状态，其后仅台账自身的本次编辑会改变摘要，以清单文件本身为准） |
| E05 模型治理 | **进行中：离线接口完成** 2026-10-23 | 私有快照后端守卫、预算/deadline/调用记录、脚本模型经框架共享核心；真实后端接入/验收、旧正式入口旁路仍待后续条件和 E14，见[本批记录](next-batch-execution-20261023.md) |
| E06 来源/规模兼容 | **完成（工程边界）** 2026-10-23 | 案件/法条独立来源、纯法条、语料/引文校验、页/快照/包精确边界及实际大页包；生产容量承诺仍待 PM |
| E07 两类完整对照 | 未开始 | 依赖 E05/E06 的适用能力及 E03 对应真实材料/模型条件 |
| E08 / N01–N03 共享执行器 | **完成（R1 工程）** 2026-10-23 | ADR 030、ToolExecutor、ReAct/kernel 共用；0.7.1 为 R1 历史收口，R2 显式图能力后续由 E09–E11 验收 |
| E09 / P00–P02 R2 协议与持久状态 | **完成（框架工程）** 2026-10-23 | ADR 031、图 v1 / checkpoint v2 / 恢复 v4，精确身份与预算/审批/owner/事件事务；旧状态明确拒绝或保持原路径，见 [R2 记录](r2-execution-20261023.md) |
| E10 / P03–P05 审批、恢复与 join | **完成（适用框架契约）** 2026-10-23 | 43 项 R2 专项映射 F01–F10 与补充窗口；新进程恢复、未知效果/费用对账、精确额度、单 owner 和 join；真实宿主效果仍随 E12–E16 验收 |
| E11 / P06–P07 发行资格与宿主兼容 | **完成（本批离线工程）** 2026-10-23 | 最终 0.8.3-r1 92/92、各 1178 框架测试；律所三版各 828/0/3、独立框架环境及发布重建；托管 CI/全新宿主安装未执行 |
| E12 / H02 宿主写端口 | **合成隔离原型完成；全验收待条件** 2026-10-23 | 六类动作、当前授权/输入版本/效果核验、同事务幂等回执；真实库无开启开关，生产容量待 PM，见[宿主记录](host-integration-execution-20261023.md) |
| E13 / H03–H05 映射与恢复 | **合成隔离闭环完成；实际领域链路未验收** 2026-10-23 | 持久映射、一次性启动授权、显式图/join、真实回执 resolver、投影与生命周期；未接正式后台/领域编排，完整验证见本次记录 |
| N00–N07 本批闭环 | **完成（离线工程范围）** 2026-10-23 | [实施记录](next-batch-execution-20261023.md)；Python 3.11/3.12/3.14 + Node22 各 828/0/3，3.12 覆盖率 86.30%；静态与发布包通过；真实模型/材料/业务及正式入口未验收 |

## 5. 里程碑状态

状态口径：未开始 / 进行中 / 完成（附证据）/ 阻塞（附停止条件）。里程碑定义与退出条件以计划书 §5 为准，此处只登记状态。

| 里程碑 | 状态 | 证据/备注 |
| --- | --- | --- |
| M0 基线与范围 | **完成（E01–E03 补齐后仍有未决项）** 2026-10-22 | 项目登记 §1；摘要 §2；既有失败 §4；行为/规模/评分/预算冻结见 [m0-baseline-20261021.md](m0-baseline-20261021.md)；**E01 入口总表、E02 摘要与兼容矩阵、E03 验收参数冻结已按新计划书补齐**（[§4c](#4c-执行计划任务登记execution-plan-20261022md2026-10-22-起)）；未决项（不阻塞 M2/M3 工程，阻塞 M5/M6）：负责人确认、Git 前置处置、E03 八项待确认值 |
| M1 公共接入契约 | **完成 + 状态审查补强（A 批次）** 2026-10-21 | 契约模块 [app/runtime_ports.py](../../app/runtime_ports.py)（纯标准库，AST 边界测试强制）；旧运行时兼容接口 [app/runtime_adapter.py](../../app/runtime_adapter.py)（幂等 start/inspect/resume/status，schema v14 映射表 + v15 绑定列）；契约文档 [m1-contracts.md](m1-contracts.md)（协议 v2/事件 v2）；锁定发行物 `vendor/`（M1 初冻 `8275baba…`；B2 完成后换绑 0.6.0）；初冻退出条件三项全过（725 passed/3 既有失败），补强后回归见 §6 变更记录；证据 `output/migration/m1-regression-20261021/`、`output/migration/a-batch-regression-20261021/` |
| M2 律所只读真实链路 | **进行中：E04/E06 工程完成，E05 离线接口完成，E07 待做** 2026-10-23 | 新共享执行器候选、原领域节点、独立案件/法条来源及脚本模型协议；[本批记录](next-batch-execution-20261023.md)记录最终验证；真实材料/模型、独立检索对照与业务门禁尚未通过 |
| M3 工具与自定义恢复 | **完成（E08–E11 适用框架 G3 工程）** 2026-10-23 | 最终 0.8.3 和宿主离线兼容通过；R2 只向显式持久图开放，旧一次性编排保持限制；宿主写入/投影/正式接线继续按 E12–E16，不另建恢复调度器 |
| M4 完整律所产品接入 | **进行中：仅合成隔离原型** | H02–H05 本次交付；生产容量与实际领域图、E07/G2/E13 待验收，E14 正式入口未开始 |
| M5 冻结与业务验收 | 未开始 | 依赖 M4 + R6/R8；需律师/业务评审人 |
| M6 切换与退役 | 未开始 | 依赖 M5；备份/恢复演练与小范围试点 |
| M7 第二实际应用与推广 | 未开始 | ForgeCommerce 已绑定 0.5.0（`58579893…`）；依赖 R5 与稳定发行物 |
| 框架 R0 | 已继承至 0.6.0 工程资格，相关审计已处置 | 当前证据 `reports/release-0.6.0-r1/`（87/87）；`framework-next` 为历史候选 |
| 框架 R1/R2 | **完成（本批框架工程）** | ADR 030/031、最终 0.8.3 92/92、43 项 R2 专项；真实模型/业务/生产未验收 |
| 框架 R3 | 未开始 | 共同契约矩阵；与 M1 契约文档对齐后实施，避免重复计费 |
| 框架 R4/R6 | 未开始 | 对应 M2/M5 的宿主验证与质量评测 |
| 框架 R5 | 未开始 | 对应 M7 |
| 框架 R7/R8 | R8 对最终 0.8.3 执行 92/92；R7 已冻结本批图/checkpoint/事件/恢复兼容规则，独立接入者及全新宿主安装仍待后续 | `reports/release-0.8.3-r1/`；旧候选原报告保留，业务资格未验收 |

## 6. 变更记录

| 日期 | 动作 | 操作者 |
| --- | --- | --- |
| 2026-10-23 | P00–P07 R2 离线工程收口：显式持久图、审批/恢复/未知费用对账、owner/预算/取消/事件；旧候选发现修复复验，最终 0.8.3 92/92，源码/干净副本各 1178，43 项 R2 专项。律所换绑后三版各 828/0/3、静态与发布重建通过；宿主真实接线与业务资格继续按后续任务。最终身份见 `output/migration/r2-execution-20261023/source-final.json`。 | Codex 实现、自审与自动化验证；无 Git 元数据，未提交、未对外发布 |
| 2026-10-23 | N00–N07 离线工程批次收口：共享执行器与只读 kernel 准入、嵌套记账及 token 边界修复；0.7.1 87/87，源码/干净副本各 1135 测试；法条独立来源、大页包与精确边界；后端离线守卫和脚本模型协议；宿主三版各 828/0/3，Node22，静态与发布重建通过。E05 仍为离线接口部分完成，E07、R2、正式产品接线与业务切换未执行。Git 未初始化，无提交。最终清单位于 `output/migration/next-batch-execution-20261023/source-final.json`，不在本文自引用摘要。 | 本地实现、自审与自动化验证；未对外发布 |
| 2026-10-22（晚） | **按新执行计划书执行首批（E01–E04）**，Git 前置未解决、无提交哈希，以内容摘要 + 证据目录为记录：**E01** 建立项目—AI 入口总表（律所 23 项含计划书点名的 chat/agent-chat/agent-jobs/analyze/agent-tools/agent-compare/vector-index/evaluate-rag，电商 9 类），机械核查 62+2 条路由，登记 11 项旁路与 3 项未决项目；**E02** 保存两侧摘要/依赖锁/发行物登记与数据 schema 兼容矩阵（律所 v15 增量迁移与防降级、框架 SDK API 1/协议 v2/事件 v2/结果 v1、电商 21 表 + 框架状态库），如实标记 Git 未恢复（并**修正前提事实：电商目录是 git 仓库**，HEAD `6a752d84…`、私有远端、工作区干净，可作另两侧恢复模板）；**E03** 冻结验收参数表（已冻结 30 项、待确认 8 项带责任人与截止节点，含不降低门槛声明）；**E04** 实现 [app/host_ports.py](../../app/host_ports.py) 七项类型化读端口（逐调用身份/权限/案件校验、源版本固定检查、查询/页/上限边界、显式拒绝不支持能力、访问记录不含案件正文）与 [app/forge_runtime.py](../../app/forge_runtime.py) 框架版 RuntimeControl（`forgeharness-readonly` 单一运行时、诚实能力声明、`start` 映射治理事实、inspect/resume/status 显式拒绝、框架结果未知时按对账返回而非伪装成功/失败），M2 试点会话改经端口；新增 30 项测试（正常/越权/撤权/变化/边界/不支持能力，含真实框架进程的 start 与失败诚实性）。**最终全量回归 805 passed / 0 failed / 3 skipped**，覆盖率 86.77%，ruff/mypy(41)/compileall/node(4/4)/发布清单全过；证据 `output/migration/e04-host-ports-20261022/`（含 JUnit、覆盖率、收口清单与 M2 差异）。收口清单 `a416c06e3a656558d733c0da62b661f75a5c4b9db086ae842d191c08ddefc94a`（228 文件）；自引用限制：台账/记录文档的后续编辑会使该摘要变化，需以清单文件本身为准。真实材料/模型对照（E05–E07）与业务终评未开始 | 迁移执行（DSH 会话 + 两份只读清点子代理），待项目负责人复核 |
| --- | --- | --- |
| 2026-10-22 | M2 离线工程接入：单案件授权快照 + 只读源连接，独立 Python 3.12/ForgeHarness 0.6.0 经固定工具调度原专家、混合检索和 Critic；固定法条对象防旧缓存漂移；精确引文映射与框架严格验证；带 hash 的核心依赖导出与 CI 独立解释器准备；发布包实际对照。同步修复提交后丢回调时的盲重试窗口、关联一致性、数据库作用域与轨迹一致读。最终本机 Python 3.11/3.12/3.14 各 **775 passed / 0 failed / 3 skipped**；8 道合成题领域输出 8/8 一致，源库逻辑摘要不变。真实材料/模型、专业评审、API/UI、M3 恢复及切换未验收；无 Git 元数据，未初始化或提交。详情与证据见 [M2 记录](m2-engineering-20261022.md) | 本地执行与复核；无外部发布 |
| 2026-10-21 | 建立台账；完成两侧勘察、LexVault 本机离线基线（两轮）、ForgeHarness 今日验证 run 事实核对、ForgeCommerce 绑定核对；登记既有失败与未决项；新增 `scripts/migration_source_manifest.py` 并冻结 LexVault 源码清单（M0 收口 `852b73d4…`，201 文件） | 迁移执行（DSH 会话），待项目负责人复核 |
| 2026-10-21 | M1 实施：冻结六项契约（`app/runtime_ports.py`）；旧运行时实现兼容接口（`app/runtime_adapter.py`，协调器新增 `on_run_started` 回调、schema v14 `runtime_requests` 映射表、既有迁移测试预期同步更新）；43 项契约/适配器测试；vendor 锁定发行物 `8275baba…` + 绑定记录 + 干净 venv 安装验证；全量离线回归 725 passed/3 既有失败/3 skipped，覆盖率 87.01%；4 个被改文件的改动前副本经 M0 清单逐字节校验（pre-change/）；Git 前置未解决，本轮无提交哈希，以上述摘要与差异文件为变更记录 | 迁移执行（DSH 会话），待项目负责人复核 |
| 2026-10-21（晚） | 状态审查第一批（边界修复）完成，按主题拆分登记（Git 前置未解决，无提交哈希，以摘要+差异文件为记录）：① fix(runtime): 绑定幂等请求与实际运行权限——schema v15 绑定列、`compute_request_digest`（lexvault-request-binding/1）、重放四态、映射 run 授权重核验、冲突零泄露；② fix(runtime): 统一可信执行身份——actor 与认证主体逐字段比对、`actor_name()` 作者归一化、租户白名单拒绝、token 模式身份丢失拒绝；③ fix(runtime): 修正步骤快照的事件修订语义——`compute_trace_revision`、RunEvent v2 快照语义、消费者规则；④ 冻结输入恢复——`benchmarks/statutory-experiment-v1.json` 自公开原仓库可信恢复（blob `abb8a84d…`/内容 `32316c2c…`/提交 `e6591521…`）并入发布允许清单；⑤ 依赖修复与发行（框架侧，B2 委派子代理执行）——0.6.0 r1 候选 87/87、qualified=true，vendor 换绑 `69ce604c…`；⑥ 发布清单修复（B3）——vendor 精确文件入允许清单、`scripts/verify_deployment_release.py` 重建验证 PASS。协议 v2/事件 v2；契约+适配器测试 70 项（模块覆盖 99%）；同步 API 兼容测试 2 项（M4 接线前提）。**收口全量回归：755 passed / 0 failed / 3 skipped，覆盖率 87.82%**，ruff/mypy(39)/compileall/node(4/4)/发布清单全过；**Python 矩阵 3.11.15 与 3.14.6 各自有效 753/0/3**（4 项 mobile_ui 失败仅因矩阵首跑未带 node，补 node 后全过；结论见 `output/migration/a-batch-regression-20261021/matrix/CONCLUSION.md`，node 24 vs CI 22 偏差在案）。I-01 审查复现脚本修复后复检 fail-closed（`output/recheck-i01-20261021/`，原证据未动）。收口清单 `output/migration/a-batch-closing-20261021/lexvault-source-manifest.json` | 迁移执行（DSH 会话 + B2 子代理），待项目负责人复核 |
