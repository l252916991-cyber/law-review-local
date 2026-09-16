# LexVault v0.3.0 接收与验收

本包交付的是**可构建、可离线演示的源码与运维工具**。模型权重、卷宗、凭证、法条语料和历史评测数据不随包交付；真实模型和法律业务效果须在接收方授权的环境另行验收。

## 接收条件与完整性

- Docker 路径：Docker Engine/Compose v2，能够下载 `python:3.12-slim`、`redis:7-alpine` 和固定的 uv 镜像；首次构建须能访问 Python 包索引。至少预留 4 GiB 磁盘给镜像、数据和日志。模型服务另行部署，容量取决于其权重，不计入此要求。
- 源码路径：Python 3.11–3.14、`uv 0.12.0`；PDF/OCR 需 Poppler、Tesseract 与简体中文语言包。未提供这些系统工具时仍可用 TXT 合成材料验证 API。
- 接收者先核对发布页所列的整个 zip SHA-256，再打开 `RELEASE_MANIFEST.json` 核对每个成员的 SHA-256。`sha256sum lexvault-source.zip`（Linux）或 `shasum -a 256 lexvault-source.zip`（macOS）。哈希能检测意外损坏，不能代替可信发布渠道或签名。

解压后目录应直接包含 `Dockerfile`、`docker-compose.yml`、`.dockerignore`、`pyproject.toml` 和 `uv.lock`。默认源码包不包含 LawBench；`benchmarks/fewshot/2-2_sft_pool.jsonl` 为本项目维护工具使用的合成样例。发布命令及数据保护规则见[发布手册](release.md)。

## Docker 离线工程演示

1. 在解压目录复制 `.env.example` 为 `.env` 并完成配置，然后在可信终端执行 `set -a; . ./.env; set +a`。Compose 会自行读取 `.env`，但后续 `curl` 和验收脚本中的 `${WEB_PORT:-8000}` 只有在当前 shell 也加载该文件后才会使用同一端口；应用本身不会自动加载 `.env`。不得执行来源不可信的 `.env`，也不得上传或提交该文件。
2. 为 Docker 演示设置 `LAW_REVIEW_AUTH_MODE=token`，生成至少 32 字符的随机 Bearer 令牌，并在 `LAW_REVIEW_API_TOKENS_JSON` 中配置一个有 `view/edit/export` 权限的管理员；不要使用样例短令牌。Web 默认只发布在宿主机 `127.0.0.1:${WEB_PORT:-8000}`。若宿主模型也占用 8000，设置 `WEB_PORT=8765`。
3. 在解压目录运行 `docker compose up -d --build`，查看 `docker compose ps`；验证 `curl -fsS http://127.0.0.1:${WEB_PORT:-8000}/api/health`。Compose 启动单 Web、单批量任务 worker 和 Redis，应用数据保存在 Docker 命名卷；不得再启动第二个 Web 指向同一数据卷。
4. 仅在**一次性数据卷**上执行 `LAW_REVIEW_SMOKE_TOKEN=<本次令牌> python scripts/delivery_smoke.py --base-url http://127.0.0.1:${WEB_PORT:-8000} --mode offline`。脚本实际创建两页合成 TXT、关闭 LLM 和远程 embedding 问答、核对第 2 页引用及导出包/清单哈希，然后将自己创建的案件移入回收站。API 有审计保留和 30 天回收站，不提供立即彻底删除；验收结束用 `docker compose down -v` **仅销毁本次一次性卷**，不得对已有业务卷执行此命令。

离线演示证明的是发布包、镜像、权限访问、解析、检索、引用定位和导出链路能在隔离环境工作；不证明生成模型质量、法条现行有效性或真实律师审阅收益。

## 接入本地模型

应用使用 OpenAI-compatible 的本地接口，模型服务须明确提供：对话 `/v1/chat/completions`、embedding `/v1/embeddings`、重排 `/v1/rerank`（或分别配置三台服务）。设置 `LAW_REVIEW_LLM_URL`、`LAW_REVIEW_EMBEDDING_URL`、`LAW_REVIEW_RERANK_URL` 和对应 `*_MODEL` 为服务**实际列出的 ID**；不要默认下载或换用另一个模型。嵌入权重变化时修改 `LAW_REVIEW_EMBEDDING_REVISION`，避免混用旧向量空间。先用服务自身的 `/v1/models` 与健康检查核对型号、URL、网络可达性，再启动模型模式的合成验收：

```bash
LAW_REVIEW_SMOKE_TOKEN=<本次令牌> python scripts/delivery_smoke.py \
  --base-url http://127.0.0.1:${WEB_PORT:-8000} --mode model
```

模型模式要求回答经引用校验且 `llm_used=true`。Docker 内访问宿主模型可使用 `host.docker.internal`，Compose 在 Linux 映射 `host-gateway`；宿主模型必须允许来自容器网桥的连接。若服务只监听宿主 `127.0.0.1`，先在受控网络调整监听或代理设置，**不要**为了连通而开放公网或关闭认证。模型 URL 安全规则、诊断和来源记录见[模型手册](models.md)与[运维手册](operations.md)。

## 交付候选验收记录

2026-09-16 从默认源码 zip 解压后实际构建镜像，并在一次性 token 认证容器和合成两页 TXT 上完成离线与模型两种 HTTP 验收：两者均定位到第 2 页并通过导出包、原件和清单哈希核对；模型模式返回 `llm_used=true` 且包含合成金额。该次模型组合为 `Qwythos-9B-v2-8bit-mlx`、`Qwen3-Embedding-4B-4bit-DWQ` 和 `bge-reranker-v2-m3-mlx`。同日以 Compose 启动 Web、worker、Redis 三个服务，通过队列导入一份合成两页 TXT，任务结果为 1/1 成功、0 失败。以上是单个合成样例的交付链路验收，不是模型质量或性能基准。

## 运行、备份与升级

- 源码非容器路径：`uv sync --locked --no-dev`，显式加载私有配置后执行 `bash run.sh`；zip 解压工具可能不保留可执行位，因此接收验收不依赖 `./run.sh`。批量导入需要 Redis 和 `bash run_worker.sh`，普通合成问答不需要队列服务。
- 查看故障：`docker compose ps`、`docker compose logs --tail 100 web worker`；停止服务用 `docker compose down`（**不加** `-v`，保留数据卷）。应用 `/api/health` 只表明进程可应答；`/api/ready` 另外显示受控法条语料是否完整，缺语料时可为 degraded。
- 正式升级前停 Web 和 worker、排空任务，并按[发布手册](release.md)用 `scripts/data_snapshot.py` 停写备份及 verify；Docker 命名卷数据应先复制到受控私有目录再按工具要求处理。恢复演练必须使用新数据目录，不能对正在写入的 SQLite 文件直接复制，不能复用较新 schema 数据库运行旧应用。
- 法条语料、模型权重、TLS 代理、组织授权、备份加密及保留期限均由部署方配置。多用户或 LAN 服务须 token/OIDC 身份、实际 Host 白名单及 TLS 代理；回环 `local` 模式只用于单机。

当前交付边界为单实例本地/受控内网辅助阅卷。CI 的合成 HTTP 验收和离线回归不能代替目标硬件的模型、OCR、代理权限、备份恢复或律师业务验收；验收结论应分别记录。

本项目当前未提供项目级开源许可证；公开可见不等于授予复制、修改、分发或商用许可。第三方依赖、模型与数据仍分别遵循其来源许可证和使用条件，详见[许可清单](licenses.md)。
