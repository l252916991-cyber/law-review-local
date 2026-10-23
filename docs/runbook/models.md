# 模型安装与来源记录

Web 项目通过本地 OpenAI-compatible 服务调用模型，模型运行环境与 `.venv` 分开管理；不把 MLX、Torch、模型权重或 Hugging Face 登录凭证打包进 Web 发布包。

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
