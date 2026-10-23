# M2 只读接入：工程实施与验证记录

日期：2026-10-22（北京时间）。依据：[改进计划 A–C](status-and-improvement-plan-20261021.md)、[迁移台账](ledger.md)。

**状态：M2 的离线工程链路已实现并验证，M2 完整验收仍在进行中。** 独立框架进程已调度原规划器、原混合检索、事实/证据/矛盾/疏漏/法条专家和原 Critic；验证仅使用新建合成数据。真实案件、真实模型、专业评审、完整产品接线和切换尚未完成。

## 1. 本轮实现

| 文件 | 职责 |
| --- | --- |
| [forge_readonly.py](../../app/forge_readonly.py) | 可信主体与案件范围、只读源连接、单案件私有快照、固定节点调用、源版本复核、精确引文映射及旧路径对照 |
| [forge_bridge.py](../../app/forge_bridge.py) | 启动独立解释器，校验 wheel 摘要，使用一次性认证、序号和有界 JSON 行协议执行节点调用；处理超时及进程清理 |
| [forge_worker.py](../../app/forge_worker.py) | 仅在框架解释器运行；使用 ForgeHarness `Harness`/`RunKernel` 调度节点，执行只读策略、工具预算、严格引文校验及轨迹完整性检查 |
| [setup_framework.py](../../scripts/setup_framework.py) | 校验依赖清单摘要，以带 hash 的固定版本依赖及精确 wheel 安装新的 Python 3.12 独立环境 |
| [compare_forge_readonly.py](../../scripts/compare_forge_readonly.py) | 显式运行本地离线工程对照，报告按 0600 权限保存到指定新文件 |
| [test_forge_readonly.py](../../tests/test_forge_readonly.py) | 单案件隔离、身份/撤权、真实框架进程、引文/法条版本、规模及协议失败用例 |

框架沿用已绑定的 **0.6.0 / SDK API 1**，wheel 摘要 `69ce604c51325085ae179917605c9479e423c1d437970758733d4f580e367a16`。没有修改框架核心或把法律规则移入核心。依赖导出 `vendor/forgeharness-runtime-requirements.txt` 来自对应框架锁文件的核心依赖；摘要登记在 `FRAMEWORK_BINDING.json`，随精确允许清单交付。

宿主进程不导入 ForgeHarness。框架进程通过固定 `lexvault_node` 工具调用宿主原节点；工具参数只有枚举节点名，问题、主体、案件和节点输入由宿主预先绑定。没有任意导入、SQL、shell、路径或网络工具接口。此处是受信代码的进程与环境分离，不是用于执行不受信 Python 的操作系统沙箱。

## 2. 数据和权限边界

- 源 SQLite 使用 `mode=ro`、`query_only` 和一致读事务；不会为实验迁移源库。源 schema 必须已由所属应用升级到 v15。
- 每个实验创建 0700 私有临时目录，只复制授权案件的 cases/documents/pages/evidence/runs/memories/gaps。会话、凭证、任务、业务审计、其他案件和源文件不复制；文档 `stored_path` 清空。
- 原检索产生的向量缓存、原协调器对照产生的运行/步骤/疏漏/审计，仅落到临时副本。实验结束清理临时库和进程状态；指定报告保存框架事件及工具证据，供复核。
- 每次节点调用前后检查当前主体、view 权限、案件范围及源身份；主体丢失、撤权、案件删除或数据变化立即终止。旧式全局 DB_PATH 覆盖可能破坏作用域时，在任何快照写入之前拒绝执行。
- 受控法条使用独立校验并固定的 `LegalCorpus` 对象；每次调用重核 manifest 与文档完整性。旧全局缓存不决定本轮法条版本；新旧对照使用同一对象。未配置语料保持原有 needs_review；配置错误、篡改或变化停止实验。

默认上限：500 页、案件正文 200 万字符；可显式设定至 2000 页/2000 万字符，单页仍不超过 20000 字符。框架仅接收检索命中的页包，因此 100 页以上案件不需要截断全案；命中页包仍受框架 100 页/20 万字符限制，超限明确失败。公共法条限制 1000 个文档、总文件大小 2000 万字节、manifest 100 万字节。

源身份包含授权案件记录和法条 manifest 身份。已有运行、材料或记忆在实验中发生变化也会停止本轮；需要稳定材料版本，不是持续更新业务库的在线比较器。只读 SQLite 连接可能影响 WAL 的读标记；验证采用逻辑数据摘要，未将数据库物理文件 hash 当成零写入的唯一依据。

## 3. 领域对照和引用校验

先通过实际宿主混合检索冻结检索结果与记忆，再分别交给框架调度的原专家和隔离副本中的原协调器；比较答案、引用、校验结果及全部专家输出。报告明确标为 `shared_retrieval_and_memory_snapshot`，用于检验编排/领域接口等价，不能当成两套独立检索算法的效果比较。

原 UI 引文可能压缩空白并加省略号。接入层将该显示片段定位为原页的连续原文，保留真实空白；无法定位就失败，不以另一段文字代替。原文按 `(document_id, document_page_no, text_sha256)` 绑定，并映射为框架页号；同名文档/不同文档的相同页码不混淆。框架 `BundleCitationVerifier(strict_quotes=True)` 校验 `[page:N]「原文」`。

这证明页身份和原文跨度可追溯，不证明结论被引文语义支持、金额计算正确或法律适用正确。纯法条问题没有案件原页时，工程链路可以正确结束为失败/证据不足；“领域输出一致”不能覆盖该状态。

## 4. 验证结果

| 验证 | 本轮结果 |
| --- | --- |
| 本地完整离线范围，Python 3.12.13 | **775 passed / 0 failed / 3 skipped**，覆盖率 **86.44%** |
| 同范围，Python 3.11.15 | **775 / 0 / 3**，覆盖率 **86.42%** |
| 同范围，Python 3.14.6 | **775 / 0 / 3**，覆盖率 **86.41%** |
| M2 新增测试 | **17 项通过**，包含真实独立框架进程；8 道合成问题采用逐题断言 |
| 独立保存的 8 题工程实验 | 答案/引用/校验/专家输出 **8/8 一致**；7 题有来源校验成功，1 题按预期证据不足；源库逻辑摘要不变 |
| 工程检查 | doctor、compileall、Ruff、Mypy（41 文件）、依赖兼容性、两份宿主依赖导出一致性、4 个 JS 语法检查通过 |
| 发布包重建 | 从允许清单构建/解压，校验 wheel 与依赖清单，独立框架环境安装和 SDK 冒烟；再以解压后的宿主源码实际运行合成 M2 对照、检查源数据不变 |

测试范围与 CI 一致：`tests`、`scripts/test_engineering.py`、`scripts/test_data_snapshot.py`、`scripts/test_snapshot_runtime.py`，启用覆盖率及 pytest-socket 禁网配置。3 项跳过仍是两项未检出的冻结法条语料检查和一项未安装的可选 mlx_lm 检查。

本机 Node 为 **24.19.0**，CI 配置为 22；本轮是本地 POSIX 环境及现有宿主依赖环境的验证，没有运行托管 Ubuntu CI。框架解释器固定 3.12。发布验证新建框架环境，宿主部分使用当前已安装依赖和解压源码，不能称为全新宿主环境安装通过。

收口时为报告补充框架已脱敏的输入消息，3.12 再跑完整套件仍为 775/0/3；3.11/3.14 重跑受影响的 17 项 M2 测试均通过。两侧矩阵的全套证据保留在 `matrix-311/`、`matrix-314/`，最后的局部复检见 `closing-311-integration.log`、`closing-314-integration.log`。

第一次离线安装因 uv 缓存缺少 rpds-py 而停止；另建新环境，按固定版本与 hash 下载后通过，没有覆盖失败目录或放宽锁。测试与合成实验没有调用真实模型、远程 embedding/rerank 或付费服务。

## 5. 顺带修正的接入前问题

- 原适配器把“没有收到回调”当成“没有落库”，与协调器在提交后发回调的事实不符。现在只有执行开始前的构造失败可释放声明；进入执行后结果未知且无关联时保留声明，返回 `needs_reconciliation=True`，同键重试进入对账，不盲目重跑。新增落库后丢失回调的回归用例。
- 重放除了授权实际关联 run，还核对案件、运行时和问题；后台状态也核验实际关联 run。授权范围内的损坏关联不能返回错误运行。
- 公共 start/inspect/resume/status 固定调用开始时的数据库作用域；轨迹读取在一个 SQLite 事务中读取运行和步骤，避免组合出不同时间的快照。

本轮没有增加 schema 版本；v15 保持不变。内容修订号仍是 opaque hash，不能比较新旧大小；未来消费者必须避免并发轮询的过期响应覆盖新响应。

## 6. 使用与后续工作

在宿主锁定环境中准备一次新的框架环境：

```sh
uv run --locked python -m scripts.setup_framework --output .venv-framework-local
export LAW_REVIEW_FORGE_PYTHON="$PWD/.venv-framework-local/bin/python"
```

已有环境不要覆盖；可复用已验证的解释器，或选择新的目录。缓存齐全时安装可加 `--offline`。离线测试前设置上述变量，实际进程测试才会执行；CI 已增加准备步骤，不依赖手工配置。

本地已授权资料的工程对照示例（报告包含案件内容，须选择私有位置）：

```sh
LAW_REVIEW_DATA_DIR=/absolute/authorized-data \
uv run --locked python -m scripts.compare_forge_readonly \
  --case-id 1 --question '核对审批矛盾与证据疏漏' \
  --framework-python "$LAW_REVIEW_FORGE_PYTHON" \
  --output /absolute/private-reports/new-readonly-report.json
```

CLI 使用当前宿主认证模式；token 模式需要由已认证宿主调用 Python 接口，不通过命令行传主体或凭证。不要切换认证模式绕过保护。

当前是固定节点的最小只读边界，未宣称完整实现 HostReadPort 七项公共方法或 RuntimeControl 的框架版本。`runtime="forgeharness-readonly"` 仅用于显式工程实验，原 HTTP/UI 和后台任务没有接线。LLM/远程 embedding、记忆保存、后台执行、自定义模型/预算覆盖显式拒绝；节点串行调度，没有并行性能或恢复能力声明。

M2 后续：经授权的真实材料对照、受治理模型链路、独立端到端检索对照、完整宿主端口及专业验收。M3 继续依赖框架 R1/R2；M4 才处理实际 API/后台/页面与状态收口。真实模型预算、律师/业务评审人和切换窗口落实前，业务资格保持未验收。

## 7. 证据与变更记录

全部证据位于 [output/migration/m2-engineering-20261022](../../output/migration/m2-engineering-20261022)：`closing-312/`、`matrix-311/`、`matrix-314/` 保存命令、JUnit、覆盖率与日志；`synthetic-acceptance-summary.json` 和逐题报告保存对照与源库摘要；`static-results.json`、`deployment-closing.log`、`cli-smoke.log` 保存工程验证。原始测试准备脚本也保留，目录为忽略的本地实验产物，不随源码发布。

源码变更前副本在同目录 `before/`，源码清单与差异检查在 `source-before.json`、`source-final.json`、`change-review.json`。两侧目录仍无 Git 元数据；按仓库规则没有自动初始化基线，也未创建提交。后续需要恢复原仓库历史或明确授权建立基线，才能使用原子 Git 提交管理这些变更。
