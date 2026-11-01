# 回答可靠性与使用者核验执行记录（S14～S19）

日期：2026-10-24（北京时间）。计划书：[回答可靠性与使用者核验下一步计划](reliability-review-plan-20261024.md)（`540b2ad`）。执行分支 `codex/reliability-review-execution`（起点另含并存的 `b7f7c46` 多轮追问计划，本任务未改动其文件）。证据目录为 Git 忽略的私有目录 `output/migration/reliability-review-20261024/`。

**摘要：** 完成原文参考清单、24 次 v10 回答的逐项核验、评分口径修正、候选 v11 与 2 个未用官方案例 7 题的新保留集；在用户授权暂停无关 LawBench 基准后，完成 35 次题目评估、70 条双对照复验（回归 22 次、新保留 7 次、原保留转开发材料 6 次）。核验未发现 P0（无虚构事实、无与原文矛盾、无引用身份错误、无主体错置）；3 项 P1 中，“公司类别遗漏”已由 v11 实测关闭（`employment-transfer-facts` 由失败转为通过），“培训地点/日期”与“‘陷阱条款/无效’认定”确认为单文档 4 页检索上限导致的事实未进入上下文，未关闭。1 条评分口径错误已按 v11 清单修正；复验另发现 1 条空白字符导致的评分误判，按新清单版本离线重评、未重跑推理。无独立复核人、无法律背景试用者，使用者核验与专业意见均未执行；**完整 G2/E07 仍不通过，生产入口未切换**。

## 1. 授权、范围与身份

- 用户于 2026-10-24 指示“执行”本计划，并选择“暂停 LawBench 10k 基准、先独占完成本轮复验、再恢复基准”。基准在 **3013/10000** 处暂停（19:01:12 的暂停快照为 2998，退出前又完成到 `2-5_0012`），复验结束后从 `2-5_0013`（3014）以 `--resume` 恢复；暂停与恢复边界记录在 `s18-rerun/gpu-handoff/benchmark-state-handoff-corrected.json`。
- 本轮仅在本机 loopback 隔离服务推理；未外发材料、未调用付费模型、未安装依赖、未修改用户全局设置、未切换生产路由、未联系外部人员。
- 执行者：Codex（实现维护者）。独立事实复核者为未落实；本记录按路径 B 记为“**开发者原文自审**”，不称独立评审。`semantic_entailment_checked=false`，`g2_accepted=false`，`e17_qualified=false`，`production_qualified=false`。
- 材料为官方公开案例摘要，不是完整卷宗；结论只支持其文本范围，不代表法律适用正确性、律所业务效果或 E17 专业终评。

## 2. 材料、参考清单与冻结

| 集合 | 内容 | 身份与状态 |
| --- | --- | --- |
| 回归集 | v10 冻结的 6 案 18 题，22 次（`training-facts`、`noncompete-arbitration-result` 各 3 次） | 数据库 `7084a902…` 与题面、预期、内容锚点保持不变；仅评分清单升版 |
| 新保留集 v4 | 最高法“案例五 王某诉薛某清算责任”、北京人社局“案例4 生育津贴”，2 案 7 题 | v11 推理前冻结；作者选题，**独立复核与确认待补** |
| 原保留集 v3 | 案例三（健身退费）、案例3（离职补偿陷阱条款）6 题 | 自本轮起转为**开发材料**，不再作为保留集报告 |
| v10 回答证据 | 24 次主运行报告、逐题结果、宿主渲染引用 | 只读复制到 `s14-prep/`，报告与结果 SHA-256 逐题登记 |

- 参考清单：31 条（24 道已见题＋7 道新保留题），每题登记问题、预期、必答事实/页/短引文与资料范围限制，见私有 `s14-prep/reference-list.json`。已见题的页/短引文在本次复核时按原文补齐，未修改任何题面、预期或锚点；这构成事后偏见风险，已如实登记。
- 新保留集 v4 的题目、必答锚点、角色检查、材料页哈希、来源链接与获取日期见 `s17-holdout-v4/`；其中“瑜伽店会员/预付款”“注销前后日期”“未经依法清算/虚假”“产假起止与天数”“生育津贴性质”“仲裁支持”均为推理前冻结。

## 3. 原文核验（24 题）

方法：先读冻结原文页，写出问题要求的必答事实与页/短引文；再读模型正文（`candidate_answer_body`）和宿主渲染的全部引用逐项判断支持性、完整性、主体与语境、可定位性、边界表达。评分清单只作筛选，最终结论以逐项核验为准；**未用 v11 清单回写历史通过率**。

结果：P0 = 0；P1 = 3；评分口径错误 = 1（非模型缺陷）；其余 20 题内容可接受。按 v10 自动断言为 20/24，按逐项人工复核为 21/24（`gym-refund-court-order` 模型正确、断言错误）。四条问题记录见 `s15-verification/issue-cards.json`：

| 题 | v10 结果 | 人工判定 | 原因 | 处理 |
| --- | --- | --- | --- | --- |
| `training-facts` | 失败 | P1：缺原地点/受训截止日、2020-06-22 告知函与 6-30 期限 | 页 2 未进入上下文；运行只渲染 4 页（页 1/6/7/8） | v11 提示可覆盖可见页，本批未关闭；检索上限另案 |
| `employment-transfer-facts` | 失败 | P1：缺“控股子公司”类别词 | 类别词已在上下文页 1，模型省略 | v11 提示要求写出类别，复验关闭 |
| `severance-trap-clause` | 失败 | P1：缺“陷阱条款”“无效”认定结论；另有 P2 归属偏差（把“案例评析”写成“仲裁委员会认定”） | 页 8 未进入上下文；运行只渲染 4 页（页 2/6/7/10） | v11 提示要求写出结论词与主体；本批未关闭；检索上限另案 |
| `gym-refund-court-order` | 失败 | 模型正确：判决句写明审理法院，主张句写明张某 | v10 `role_check` 对含“1600”的主张句也要求“法院”，口径错误 | v11 清单 `applies_to=disposition`；旧结果不回写 |

引用可定位：24 题全部引用均指向正确材料与原页，未发现引用身份错误；两条历史 unknown 调用不在本 24 次内，继续保留未决、永久未知用量、不重发。

## 4. 评分口径修正

1. **裁判主体口径（v11 清单）。** `role_checks[].applies_to="disposition"` 时，只有陈述处理结果（判决/裁决/裁定/判令/认定/支持/驳回/处理）的 finding 才触发“必须写裁判主体”的检查；未标注的检查保持 v10 的 `any` 语义。受影响：`gym-refund-court-order` 及两处裁决主体检查加显式标注；题面、预期、锚点不变。
2. **空白字符归一（v12 清单，离线重评）。** v11 运行中 `graduate-certificate-holdout` 写“2023 年 1 月 20 日”，日期正确但被字面匹配误判为失败。v12 规则为“匹配前去除正文与锚点的全部空白字符”，对同一批已记录输出离线重评，**不重跑推理、不改答案、不改题面**；原 v11 字面结果与变化清单同时保留。

## 5. 候选 v11 与修复映射

代码提交 `043481a`（`feat(answerability): 要求回答写出所问具体项与认定结论`）。协议由 `candidate-answerability-v10-content` 升为 `candidate-answerability-v11-detail`：

- 新增两条硬要求：问时间/地点/金额/数量/公司或主体类别/认定结论时必须写出资料中的对应内容，不得用“很远”“关联公司”“转嫁风险”一类概括代替；问“如何认定、是否有效、如何处理”时必须写出结论或处理结果并写明认定主体。
- 未改语法（`omlx-guided-grammar-v1`）、宿主解析、来源范围校验、拒绝语义与编排；仍为单节点、失败关闭、不重发。
- 新增协议级断言锁定维度词、结论词、概括替代词、JSON-only 约束与版本号；全量测试结果见第 7 节。

| 问题 | 修复 | 身份 |
| --- | --- | --- |
| `employment-transfer-facts` 缺类别词 | 提示 v11 要求写出所问类别 | 代码提交 `043481a`；v11 复验通过 |
| `gym-refund-court-order` 断言口径 | 评分清单 `applies_to=disposition` | 私有 v11 清单；v3 开发材料复验通过 |
| 日期空格导致误判 | 评分清单 v12 空白归一 | 私有 v12 清单；对已记录输出离线重评 |
| `training-facts`、`severance-trap-clause` 缺事实 | 未修复：检索覆盖上限 | 记录原因与成本，列入下一轮 |

## 6. 复验

### 6.1 回归集（18 题 / 22 次）

| 指标 | v10 | v11 |
| --- | --- | --- |
| 结果分布 | 15 success / 7 source_rejection | 15 success / 7 source_rejection |
| 候选系统失败 / 未知用量 | 0 / 0 | 0 / 0 |
| 总量 token 比 | 0.9567 | 1.0047 |
| 逐题 token 比 | ≤1.0240 | ≤1.0598 |
| p95 时延比 | 0.7879 | 0.8518 |
| 内容断言（刚运行的字面匹配） | 18/22 | 18/22 |
| 内容断言（v12 归一再评） | — | 19/22 |
| 配对工程检查 | 21/22（`graduate-certificate-holdout` 一次无效） | 22/22 |
| 未闭合内容项 | `training-facts`（3 次）、`employment-transfer-facts` | `training-facts`（3 次）；`graduate-certificate-holdout` 为空白字符误判，归一再评通过 |

`employment-transfer-facts` 的 v11 正文已写出“后来被安排转入其在北京的控股子公司——某销售公司工作”，类别词缺失关闭；`training-facts` 三次均未写出页 2 的日期/地点，与检索覆盖限制一致。`graduate-certificate-holdout` 的 v11 正文为“2023 年 1 月 20 日，冯某正式取得毕业证书”，事实正确，仅因数字与日期字符间有空格被字面锚点判失败；v12 归一后通过，原 v11 结果保留。

### 6.2 新保留集 v4（2 案 / 7 题）

| 题 | 预期 | 结果 | 内容（v11 字面 / v12 归一） | token 比 | 配对 |
| --- | --- | --- | --- | --- | --- |
| `yoga-members-amount` | success | success | 通过 / 通过 | 0.9058 | 有效 |
| `yoga-closure-dates` | success | success | 失败 / 通过 | 1.0449 | **无效**（原生 critic 回落，见下） |
| `yoga-closure-holding` | success | success | 通过 / 通过 | 1.0082 | 有效 |
| `yoga-repayment-unknown` | source_rejection | source_rejection | 通过 / 通过 | 0.9690 | 有效 |
| `maternity-leave-period` | success | success | 通过 / 通过 | 1.0450 | 有效 |
| `maternity-allowance-nature` | success | success | 通过 / 通过 | 1.0304 | 有效 |
| `maternity-arbitration-result` | success | success | 通过 / 通过 | 1.0440 | 有效 |

- 7 次全部成功/按预期拒绝，0 次候选系统失败、0 次未知用量；v12 归一后内容 7/7。`yoga-closure-dates` 的两句日期均正确（页 1、页 2），字面失败同样来自空格。
- 有效同链路配对 6/7：`yoga-closure-dates` 的原生侧 critic 两次以 `lawyer_review_present` 失败后回落为规则汇总（`native_llm_used=false`），该题的**用量与时延不作同链路比较**，单列如上；候选侧正常成功，其内容仍按 v12 判定通过。有效配对总量 token 比 **1.0016**、逐题最高 **1.0450**、p95 比 **0.7243**，均在冻结阈值内。
- 新保留集为**作者选题、作者标注**，尚无独立复核确认；只能作为开发证据，不称独立保留集，不据此声称 E17 或生产就绪。
- 逐条人工复核未见 P0。`yoga-closure-holding` 的“欺诈行为”是对原文“虚假清算报告骗取注销”的概括（P2 措辞，不影响“未经依法清算/虚假”两项必答）；其余答案的事实、主体与拒绝均与原文一致。

### 6.3 原保留集 v3（6 题，开发材料）

| 题 | 预期 | 结果 | 内容（v11 / v12） | token 比 | 配对 |
| --- | --- | --- | --- | --- | --- |
| `gym-membership-facts` | success | success | 通过 / 通过 | 0.9700 | 有效 |
| `gym-refund-court-order` | success | success | 通过 / 通过 | 1.0234 | 有效 |
| `gym-refund-unknown` | source_rejection | source_rejection | 通过 / 通过 | 0.9939 | 有效 |
| `severance-arbitration-result` | success | success | 通过 / 通过 | 1.0219 | 有效 |
| `severance-trap-clause` | success | success | 失败 / 失败 | 1.0405 | 有效 |
| `severance-account-unknown` | source_rejection | source_rejection | 通过 / 通过 | 1.0153 | 有效 |

- `gym-refund-court-order` 在 v11 清单下通过，`applies_to=disposition` 的裁判主体修正得到实测确认；旧 v10 失败结果未回写。
- `severance-trap-clause` 仍未通过：v11 正文已写出“属于无效条款”（与全案页 8 的结论一致），但没有写出“陷阱条款”，且页 8 仍未进入上下文；该“无效”结论是从可见页 6/7 的争点与理由**推得**，显式依据在未检索到的页 8，登记为“结论正确但引用支撑不足”，不据此判为 P0。
- 6 次全部有效配对；总量 token 比 **1.0119**、逐题最高 **1.0405**、p95 比 **1.0655**，均在阈值内。

### 6.4 使用者试用与专业意见

**未执行。** 本轮没有可用的法律背景试用者，也没有独立事实复核者；按计划路径 B，仅完成开发者原文自审。因此：

- 没有“使用者能否在无开发者提示下定位依据”的实测证据，不能声称使用者核验完成。
- 没有专业律师意见，不能推断法律适用正确性或 E17 终评。
- 补齐责任：项目负责人落实至少 1 名法律背景试用者（60～90 分钟试用＋约 30 分钟复核），并优先覆盖四个已知问题与至少四道新保留题；独立事实复核者确认 v4 选题与参考清单。

## 7. 工程检查

- 静态：`uv run --locked ruff check app tests scripts` 通过；`uv run --locked mypy` 通过（42 个源文件）；`compileall` 通过；四份 `app/static/*.js` 的 `node --check` 通过。
- 离线测试（CI 参数：隔离数据目录、`LAW_REVIEW_LLM_URL=127.0.0.1:1/v1`、`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`、`pytest-socket --disable-socket`、`LAW_REVIEW_FORGE_PYTHON` 指向 s06 实装环境）：`pytest tests scripts/test_engineering.py scripts/test_data_snapshot.py scripts/test_snapshot_runtime.py` 为 **1005 passed / 0 failed / 3 skipped**，分支覆盖率 **85%**。3 项跳过仍是两份冻结法条语料未检出与可选 `mlx_lm` 未安装；框架进程测试未跳过。
- 第一次运行有 4 项失败，均为本机 `node` 不在 PATH（`FileNotFoundError: 'node'`），与本改动无关；将 Node 22.23.4（`output/migration/next-batch-execution-20261023/node22-official`）加入 PATH 后重跑，结果如上。
- 这是单机 Python 3.12 的一次完整离线运行，不等同于 CI 的 3.11/3.14 矩阵，也不是发布包重建、依赖导出一致性或真实框架多版本验收；这些项目本轮未执行，不能声称完整 CI 通过。

## 8. 结论与下一步

**复验后的事实：** 回归集按 v12 为 19/22（唯一未过为 `training-facts` 3 次）；新保留集 v4 为 7/7；原保留集转开发材料为 5/6（唯一未过为 `severance-trap-clause`）。35 次评估、70 条双对照中 0 次候选系统失败、0 次未知用量；34/35 为有效配对（`yoga-closure-dates` 一次原生回落单列）。评分口径两处修正均经实测确认：`gym-refund-court-order` 在 v11 清单下通过、v3 开发材料复验通过；`employment-transfer-facts` 由失败转为通过。两类修复没有关闭 `training-facts` 与 `severance-trap-clause` 的 P1。

| 计划 §5 指标 | 本轮结果 | 证据 |
| --- | --- | --- |
| 参考清单覆盖 | 31/31 题有原文参考或拒绝依据 | `s14-prep/reference-list.json` |
| 事实与角色正确 | P0=0；无与原文矛盾、主体错置、引用身份错误；1 条结论有全案依据但显式页未检索，已登记 | `s15-verification/` |
| 关键事实完整 | 逐题报告：回归 17/18、v4 7/7、v3 5/6 | 6.1～6.3 |
| 引用可核验 | 全部引用可定位；身份错误 0 | `s15-verification/verification-records.json` |
| 使用者核验完成 | **未执行** | 6.4 |
| 专业意见 | **未执行** | 6.4 |
| 工程与成本 | 有效配对 34/35；总量 token 比 1.0047/1.0016/1.0119 ≤1.10；p95 比 0.8518/0.7243/1.0655 ≤1.20；0 系统失败；0 unknown | 6.1～6.3、`s19-report/valid-pairs.json` |

**本轮不通过项与判定：**

1. 可靠性：P0 = 0；P1 剩 2 项（`training-facts`、`severance-trap-clause`），均确认为**单文档 4 页检索上限**导致事实未进入上下文。该上限来自 `HybridRetriever._select_diverse`（`max(2, ceil(limit*0.6))`）；`app/rag.py` 被原生对照基线锁定，修改需重钉基线并重跑全部对照，属于协议变更。本轮只记录原因、证据与成本，未擅自修改。
2. 评分：v11 修正裁判主体口径；v12 修正空白字符误判。两处均未通过“改标准提通过率”的方式回写历史结果。
3. 使用者核验：未执行；无独立复核人、无专业意见。
4. 工程：静态与离线测试结果见第 7 节；发布包、三版矩阵与生产容量不在本轮范围。
5. 状态：`g2_accepted=false`、`e17_qualified=false`、`production_qualified=false`；E12～E20 的进入条件不变，不切换生产入口。

**下一步（按优先级）：**

1. 以最小改动修复单文档检索覆盖（例如单文档不设 4 页上限），重建原生对照基线，重跑回归集与新保留集；在此之前不新增节点、不升级模型。
2. 落实独立事实复核者与法律背景试用者，优先复验 `training-facts` 与 `severance-trap-clause`，并完成 v4 选题确认。
3. 若检索修复后两题仍不关闭，再评估上下文长度、分块与输出限制，记录成本后决定是否调整架构；不以继续调提示代替检索修复。
4. 保持两条历史 unknown 未决与永久未知用量，不重发、不补零；新批次使用新的评估身份。

## 9. 证据索引（私有）

| 目录/文件 | 内容 |
| --- | --- |
| `s14-prep/` | v10 运行报告与结果复制、工作清单、参考清单、四个问题证据卡 |
| `s15-verification/` | `verification-records.json`（24 题逐项）、`issue-cards.json`、`review-dump.txt` |
| `s16-fix/README.md` | 提示词与评分口径修正、检索限制成本说明 |
| `s17-holdout-v4/` | 新保留集材料、pages、冻结清单、来源与冻结哈希 |
| `s18-rerun/` | v11 运行 preflight、逐题报告、三模式摘要、v12 离线重评、服务器日志、GPU 交接记录 |
| `s19-report/` | 汇总、v12 重评结果、本记录引用的数值 |

私有目录已被 `.gitignore` 的 `output/` 规则忽略（创建后已用 `git check-ignore` 验证）；不提交人员身份、联系方式、原始访谈、数据库或含个人信息的案例副本。
