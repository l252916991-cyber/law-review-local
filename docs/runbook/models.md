# 模型安装与来源记录

Web 项目通过本地 OpenAI-compatible 服务调用模型，模型运行环境与 `.venv` 分开管理；不把 MLX、Torch、模型权重或 Hugging Face 登录凭证打包进 Web 发布包。

## 模型层：provider、wire api 与模型声明

`app/model_ports.py` 把"谁提供模型"和"用什么协议说话"拆成两层，参照 pi-ai 的 provider / API 划分（见 `docs/runbook/licenses.md`）：

- **provider** 是运行时单元：名称、端点、凭证来源（环境变量或 `<LAW_REVIEW_DATA_DIR>/model_credentials.json`）。请求级显式值优先于存量凭证。
- **wire api** 是实现单元：`openai-completions`（默认，`/chat/completions`）与 `anthropic-messages`（`/messages`，`x-api-key` + `anthropic-version`）。新增协议只需注册一组 `request_path` / `build_chat_request` / `parse_completion` / `parse_stream_frame`，传输、超时、记账、凭据都不复制。
- **`ModelSpec` 是纯数据**：`id / api / provider / context_window / input_modalities / reasoning / price_column`。它随每次调用写进 `llm_provenance()`，因此"这次是哪个模型、哪套协议、是否在目录里声明过"能进审计，而不是只存在对象里。
- 模型目录可从 `LAW_REVIEW_MODEL_CATALOG_FILE`（JSON）或 `LAW_REVIEW_MODEL_CATALOG` 读取；两者都没有时退回到 `config.LLMConfig` 的单模型默认值。未声明的模型 ID 仍可调用，但 provenance 中 `declared=false`，价格列只能按名称猜。
- 注册表只有一个 provider 时，它就是默认目标；有两个及以上时，调用方必须显式指定 provider，否则报错而不是猜。

**跨协议交接时丢弃 thinking 块**：旧模型产出的推理内容对另一种协议没有意义，转发给新模型会被当作正文。因此 `parse_completion` / `parse_stream_frame` 只返回正文，`strip_reasoning()` 在解析之后仍执行一次；切换 `LAW_REVIEW_LLM_API` 或换模型都不需要额外清理。

**工具循环只支持 `openai-completions`**：`tool_loop` 构造 OpenAI tools 负载并从中读 `tool_calls`，在其他协议下抛 `ToolsUnsupportedError`，由 `chat()` 回落到单次调用——与"端点拒绝 tools"共用同一条回落路径，不另起一套降级逻辑。

**新增协议的检查清单**：注册 API 常量与四个协议函数 → 在 `tests/test_model_ports.py` 加协议级断言 → 在 `tests/test_chat_stream.py` 用真实 `call_local_llm` 验证端点路径、鉴权头、负载形状与 provenance → 更新本节和 `.env.example`。

## 已有模型

```bash
uv run python scripts/setup_models.py inspect models/Qwythos-9B-v2-4bit-mlx
```

此操作只读取配置、权重文件数量/大小及链接情况，不发送请求、不重算全部权重或移动文件。现有本机转换的 Qwythos 权重没有完整上游 commit/转换工具来源时，不能声称从任意新下载可逐字节复现。

## 新安装的统一入口

先核对模型仓库许可证、实际存在的 immutable commit、所需格式和磁盘空间，在独立模型环境安装 Hugging Face `hf` CLI，再调用：

```bash
uv run python scripts/setup_models.py download \
  --repo OWNER/MODEL --revision VERIFIED_40_CHARACTER_COMMIT \
  --target /absolute/private-models/model-name
```

默认只显示将执行的命令；显式增加 `--execute` 才下载。脚本拒绝 `main/latest`、源码目录内目标，以及没有匹配来源记录的非空目录；同一来源中断后可以重试。它不自动安装包、不猜测备用模型、不执行量化转换、不删除原始权重、不自动切换正在运行的服务器。

`hf download --revision` 来自 [Hugging Face CLI 官方文档](https://huggingface.co/docs/huggingface_hub/guides/cli)。需要 MLX 转换时，应在独立环境固定 MLX/转换器版本和量化参数，并保留输入 commit 与输出 hash；当前 Web 工程没有足够证据为既有本地转换产物补造历史。

服务加载成功后，将其准确模型 ID 写入 `.env` 的 `LAW_REVIEW_LLM_MODEL`/`LAW_REVIEW_AGENT_CRITIC_MODEL`，embedding 模型采用实际配置。改为远端服务会改变数据边界，须单独授权。旧根目录下载脚本保留为历史材料，不再推荐运行。
