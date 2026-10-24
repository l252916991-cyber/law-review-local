# AI 应用升级实施记录（A01–A13）

日期：2026-10-24。分支 `codex/ai-app-upgrade`，基线 `760a24b`。计划见[升级计划](ai-application-upgrade-plan-20261024.md)。

## 1. 提交清单

| 提交 | 包 | 结果 |
| --- | --- | --- |
| `d641fca` | A02 | `llm_calls` 表（schema v19）+ P50/P95 汇总 |
| `d7eb6e7` | A01 | SSE 流式 + 草稿撤回 + 前端打字机 |
| `01aaad3` | A03 | 注入红队 7 项 + 威胁模型 |
| `17a2296` | A04 | 5 个新只读工具 + 9 份 tools schema |
| `8e80c0c` | A04 | 原生 tool calling 循环（3 轮 / 6 次上限 / 显式回退） |
| `8f037d0` | A05 | MCP stdio server |
| `55ace29` | A06 | 轨迹展开查看输入输出 |
| `250907c` | A07/A08 | 17 题答案级评测 + 确定性评分 + judge/κ |
| `33552e8` | A09 | 回退路由 + 修复变量遮蔽回归 |
| `01422fc` | A12 | 视觉转录适配器 + Tesseract 回退 |

## 2. 验证结果

最终一次全量离线验证（本机 macOS，Python 3.12）：

```
1055 passed, 85 skipped, 2 deselected, 255 subtests passed
覆盖率 81%（门槛 70%）
ruff: All checks passed
mypy: Success: no issues found in 43 source files
```

未计入的三类：

| 项 | 原因 |
| --- | --- |
| `test_mobile_ui.py` 4 项 | 本机无 `node`；CI 安装 Node 22 后会执行 `node --check` |
| 2 项语料测试 | 本地 `output/score85/legal_corpus*` 与冻结断言不一致，属未提交的语料构建工作，与本批无关 |
| 85 项跳过 | 需要独立 Python 3.12 框架侧车、冻结法条语料或 `mlx_lm`，CI 会准备部分环境 |

新增测试 78 项：用量 10、流式 10、注入 7、工具 12、循环 11、MCP 10、轨迹 2、答案评测 18、路由 8、视觉 12。

> **2026-10-25 复核更正**：上一行"78 项"是合计笔误，正文十组相加为 100，且这 100 个 `def test_` 方法全部位于本批新增的 10 个测试文件。按统一收集口径（含参数化展开），收集数由基线 `760a24b` 的 1076 项增至 `9630e45` 的 1180 项，即新增 104 项收集用例。本节其余数字（1055 passed / 81%）属该次环境的历史陈述；本轮同口径实测为 1177 passed / 2 failed / 1 skipped，覆盖率 85.42%。"本机无 node"只对该次 PATH 成立。详见[证据口径与环境核对](ai-application-upgrade-evidence-20261025.md)。

## 3. 关键设计决定

1. **流式不绕过校验**：token 帧标 `provisional`，校验失败发 `retract`；草稿不写入消息表。流式只改传输层，`call_local_llm` 仍读完整流后返回全文，`@governed_backend` 因此仍包住整段调用。
2. **缺失 usage 记 NULL**：把"未知"记成 0 会让降级后端显得更便宜，并拉低平均值。
3. **工具作用域由服务端注入**：HTTP 中间件不覆盖进程内工具调用，`get_page` 必须自行反查 `document → case`。
4. **工具即契约**：`verify_statute` 只在唯一法名+唯一条号时返回 `ok`；`search_statutes` 永不返回 `ok`。
5. **回退不是豁免**：回退答案必须通过同一引用校验，半配置直接报错。
6. **视觉失败不回退到空白**：任何异常都交回 Tesseract，避免静默丢页。

## 4. 过程中发现的真实缺陷

- **变量遮蔽**：回退分支把局部变量命名为 `fallback_answer`，遮蔽了模块级规则摘要函数，导致关闭 LLM 的路径抛 `UnboundLocalError`。由流式端点的 `use_llm=False` 测试捕获，已重命名并回归。
- **生成器与治理账本**：若把 `call_local_llm` 改成生成器，`@governed_backend` 只包住创建时刻，预算账本会漏记。实现上保持"函数内读完流"。
- **上下文不跨线程**：流式工作线程默认拿不到 `db_scope` 与认证主体，需 `contextvars.copy_context()`；否则测试与部分部署会读到错误数据库。
- **默认参数冻结**：`executor=execute_readonly_tool` 作为默认值会在定义时绑定，替换模块属性无效；改为调用时解析。

## 5. 未完成与边界

- A10（ANN）与 A11（前端重写）未实施，原因见计划 §4；不做"看起来完成"的占位实现。
- A12 只验证了请求构造、边界与回退；**真实扫描件的识别质量未验证**。
- A00 简历数字按用户要求暂未修改；当前 README 与评测文档中的数字仍以仓库证据为准。
- 演示视频与在线体验未录制。
