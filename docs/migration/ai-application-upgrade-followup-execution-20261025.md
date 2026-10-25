# A05.1 / A07.1 / A08.1 / A13.1 续行记录

日期：2026-10-25。开始版本 `ff0eb21`，分支 `codex/ai-app-upgrade`。按用户“执行”推进；合成数据和真实模型、工程与人工业务验收分别记录。A00 继续暂缓，不对外发布。

## A05.1：协议实跑与真实模型负结果

新增 `scripts/validate_application_acceptance.py`：默认只在临时合成库执行 stdio 子进程协议探针，按 initialize 响应→initialized→操作的顺序执行；`--live` 才调用明确的 HTTP 回环模型，预算上限为 3 轮 tools 与 2 次问答调用。临时环境退出后恢复；不复用现有库。协议版本协商及工具语义参考 [MCP lifecycle](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle) 与 [tools](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)。

| 验收层 | 结果 |
| --- | --- |
| 协议与权限单元测试 | 保留；新增独立进程、默认不推理、端点凭据/远端拒绝检查 |
| 独立 stdio 进程 | 8 项检查通过：退出、通知无响应、初始化、9 工具目录、正常调用/data_notice、未知工具拒绝、跨案拒绝、token 模式拒启动；协商为 2024-11-05 |
| Cursor / Claude Desktop | 本机 `/Applications`、用户 Applications 和 PATH 未找到；**未验收**，协议探针不能替代桌面客户端 |
| 9B 真实 tools | `Qwythos-9B-v2-OptiQ-4bit`，omlx `127.0.0.1:8000/v1` 返回 HTTP 400；错误体为 `prefill_memory_exceeded`，不是已证实“不支持 tools” |
| 35B 真实 tools | `Qwen3.6-35B-A3B-OptiQ-4bit` 被动态内存上限拒绝加载；无有效工具响应 |
| 9B 真实问答 | 请求被服务拒绝，按传输失败记账，tokens 为 null；无有效答案 |
| 真实双模型回退 | 主请求未产生可校验答案，因此未进入“校验失败后回退”路径；**未验收** |

未关闭其他应用、清理用户模型或修改内存保护。模型负结果保存在不入库的 `output/a051/`，包含瞬时内存诊断，仅作该时点环境证据，不能作为模型质量分数。初轮 `ToolsUnsupportedError` 标签是错误分类，已由真实错误体定位并修复；最终分类回归使用同一类错误体离线复放，没有把失败改写为通过。

发现并修复：`542a5e2` 保证回退请求发往已校验的 URL；`10470dd` 明确升级固定 native 对比基线，旧报告不可混用，见[基线记录](model-endpoint-baseline-20261025.md)。另修复将任意 HTTP 400/404/422 当成 tools 不支持的问题：只有错误体明确指向不支持 tools 才走兼容路径；内存准入错误仍失败并记账。相关协议、tools、注入测试 34 项通过，Ruff/Mypy 通过。

## 当前边界

A05.1 不能标记整体完成：仍需可用的真实桌面客户端、足够内存与有效模型响应。下面继续实施与这些环境无关的数据集和评测工程；人工标签缺失时不生成“人工一致性通过”结论。
