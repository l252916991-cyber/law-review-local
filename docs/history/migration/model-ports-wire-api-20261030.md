# 模型层拆分：provider、wire api 与模型声明（2026-10-30）

- 结论：模型调用从"一个内联的 OpenAI 请求字典"改为 `app/model_ports.py` 的分层——provider 管端点与凭证，wire api 管请求/响应形状，`ModelSpec` 管能力与价格列。默认行为不变，新增 `anthropic-messages` 作为第二个协议。
- 设计来源：pi-ai（`packages/ai`，MIT）文档中 provider / API 二分、模型可序列化、跨 provider 交接丢弃 thinking 三条约定。只取形状，未复制源码、未引入依赖；来源记录见 `docs/runbook/licenses.md`。
- 范围：`app/model_ports.py`（新）、`app/services.py`、`app/llm_usage.py`、`app/model_router.py`、`app/tool_loop.py`、`app/agents.py`（一行透传）、测试与运行手册。未改检索、评分、回答契约与数据库结构。

## 为什么改

改动前，协议、端点、凭证、价格列、usage 解析全部内联在 `services.call_local_llm` 里。加第二个后端必须复制这段传输守卫（egress 审批、重定向拒绝、绝对超时、SSE 体积上限），而复制出的第二份守卫才是真正的风险：漏一个，出口策略就有洞。拆分后新增协议只注册 `request_path` / `build_chat_request` / `parse_completion` / `parse_stream_frame`，守卫只有一份。

同时补上两处审计缺口：

1. **provenance 之前只有模型名**。现在记录 `api`、`provider`、`declared`、`price_column`，因此"这次答案由哪套协议、哪个 provider 服务、模型是否在目录中声明过"能随答案留存，切换后端后旧答案仍可归因。
2. **usage 逐帧覆盖**。旧实现把每帧的 `usage` 整体替换，`anthropic-messages` 在流开始时给输入 token、结束时给输出 token，替换会丢掉一半。现在按字段合并。

## 关键设计

- **provider 是运行时单元**：名称、端点、凭证（环境变量或 `model_credentials.json`），请求级显式值优先。注册表只有一个 provider 时它就是默认目标；有两个时必须显式指定，不猜。
- **api 是实现单元**：`openai-completions`（默认，`/chat/completions`）与 `anthropic-messages`（`/messages`，`x-api-key` + `anthropic-version`）。未知值在调用点报错，不静默回落。
- **模型是数据**：`ModelSpec` 可序列化，`declared=false` 表示该模型不在目录中、能力与价格靠推断。
- **交接丢 thinking**：`parse_*` 只返回正文，`strip_reasoning()` 仍执行一次；切换协议或换模型不需要额外清理。
- **工具循环只支持 `openai-completions`**：其他协议下抛 `ToolsUnsupportedError`，走"端点拒绝 tools"同一条回落路径到单次调用。

## 对 native 对照基线的影响

`services.py` 与 `agents.py` 在 `native_baseline_identity()` 的摘要门禁内，本次改动使摘要变化，按既有规则重登记为 `specialist-review-v4`（`app/model_comparison.py`），并在 review note 中说明：提示模板、`PROMPT_VERSION`、检索、回答契约、校验与工具循环行为未变；默认协议下请求体与响应解析结果与改动前逐字段一致（`tests/test_chat_stream.py` 覆盖）；`agents.py` 只是把回退目标自带的协议透传给 `call_local_llm`。

## 验证

- `uv run --locked ruff check app tests scripts`：通过。
- `uv run --locked mypy`：60 个源文件无问题。
- 离线全量（`tests` + 三个脚本测试，`--disable-socket`）：**1462 passed, 85 skipped, 20 failed**；20 项失败均为本机环境条件，与本改动无关——18 项 JS 测试需要 `node`（当前 PATH 无 node），2 项依赖本机 `output/score85` 冻结语料的本地副本分类结果（干净检出下按跳过处理，已在 `HEAD` 工作树复核）。
- 新增测试：`tests/test_model_ports.py`（75 项）、`tests/test_chat_stream.py` 的协议与流式断言、`tests/test_model_router.py` 的回退协议、`tests/test_tool_loop.py` 的协议拒绝、`tests/test_llm_usage.py` 的显式价格列。
- 未执行：真实模型端点下的 `anthropic-messages` 联调（本机无该协议服务）；M2 框架进程测试（缺 `LAW_REVIEW_FORGE_PYTHON`，按规则记为跳过，不计入通过）。

## 未做 / 仍需注意

- `tool_loop` 仍只构造 OpenAI 负载；要在 Anthropic 协议下也开工具循环，需要按协议分派的 tools 负载构造器，本轮不做。
- 模型目录（`LAW_REVIEW_MODEL_CATALOG_FILE`）目前只影响 provenance 与协议解析，不改变路由决策；多 provider 自动选路不在本轮范围。
- 流式响应下 `usage` 合并对同一字段取后到值；若某协议对同一字段分片上报，需要改为累加——当前两个已知协议都不这么做。
