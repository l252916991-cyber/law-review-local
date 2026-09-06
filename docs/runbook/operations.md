# 本机运维手册

`uv sync --locked --group dev` 创建项目独立 `.venv`，不改变正在运行的全局 oMLX/MLX 环境。应用依赖与开发依赖以 `pyproject.toml` + `uv.lock` 为准；`requirements.txt` 是带 hash 的运行依赖导出。

执行 `uv run python scripts/doctor.py --require-ocr`，检查 Python 3.11–3.14、SQLite FTS5、Python 包、Poppler 三个命令及 Tesseract `chi_sim`/`eng`。此命令不探测模型、不读取案件。缺少 OCR 时仍可使用纯文本/DOCX。

复制 `.env.example` 为 `.env` 后人工核对；在每个服务终端显式 `set -a; . ./.env; set +a`。不要在共享终端打印 token 或带密码的 Redis URL。服务应从项目根目录启动；相对数据目录以进程工作目录为准。

```bash
uv run uvicorn app.main:app --host 127.0.0.1 --port 8765
uv run arq app.tasks.WorkerSettings
```

只在需要批量导入时启动 Redis/worker。Docker Redis 绑定 `127.0.0.1:6379:6379`；Web/worker 的队列名、Redis DB 和数据路径须一致。不要在已运行长测试时重启本地模型或并行发起模型基准。

| 症状 | 首先检查 |
|---|---|
| 批量导入 503 | Redis 连通性、worker 是否启动、Web 启动日志 |
| 任务持续 pending | Web/worker `ARQ_QUEUE_NAME` 和 `REDIS_URL` 是否相同 |
| PDF/OCR 失败 | `doctor.py --require-ocr`、语言包、具体文件错误 |
| 检索降级 | 模型 ID、embedding 后端/维度、索引状态和日志 |
| LLM 回退 | 配置模型是否可用、超时/输出契约/截断原因 |
| LangGraph 409 | 是否失败 LangGraph 运行、checkpoint 是否仍在；已完成不可恢复 |
| SQLite locked | 活跃写任务、长事务、索引重建频率；先测量，不直接共享跨线程连接 |

模型服务器只提供其实际安装的模型，应用不会偷偷选择另一模型替代。部署为多用户服务前必须启用身份/案件授权和 TLS，并完成权限测试。

## 身份与案件权限

`LAW_REVIEW_AUTH_MODE=local` 为默认，仅允许回环连接，适合单用户本机。任何反向代理/LAN/公网暴露必须使用 `token`，同时设置 `LAW_REVIEW_ALLOWED_HOSTS` 为实际服务域名，TLS 由受控反向代理提供。不能把代理转发后的回环来源当作用户身份。

token 模式的 `LAW_REVIEW_API_TOKENS_JSON` 是“随机 token → principal 配置”的 JSON 对象。每个 token 至少 32 字符；每个 principal 具有唯一 `name`、布尔 `admin` 与正整数数组 `case_ids`。默认空对象在 token 模式下拒绝服务，不能直接沿用模板。管理员可创建案件，普通 principal 仅访问分配的案件及其派生资源；撤销/轮换凭证由维护者更新配置管理。

浏览器登录把 token 交换为 HttpOnly、SameSite=Strict cookie，浏览器保存期限为 8 小时，HTTPS 时设置 Secure。API 客户端可使用 `Authorization: Bearer ...`。前端显示 `/api/auth/me` 返回的身份，退出调用 `DELETE /api/auth/session`。不要将 token 放进 URL、截图、公开脚本、localStorage 或日志。跨站 API 请求（包括 GET 导出）与不允许的 Host 会被拒绝。cookie 使用配置凭证本身，浏览器期限不是服务端凭证失效时间；服务端撤销需轮换配置中的 token。

这不是独立用户注册、SSO、数据库加密或企业级凭证管理系统。拥有同一凭证者拥有相同案件权限；不能把该最小实现宣传为完整生产安全方案。

## 就绪探测与导出安全

`GET /api/system/health` 在启动完成且业务库可只读查询时返回 200；启动未完成、停机中或业务库不可读时返回 503。token 模式仍要求管理员凭证。探测不会创建缺失数据库；数据库锁等待上限为 1 秒。Redis 每次重新探测，连接和 ping 合计最多 2 秒，连接释放另限 2 秒；Redis 不可用只禁用批量导入，不使核心服务返回 503。启动取消也会关闭阅卷执行器并清除就绪状态。

这不是模型、OCR、worker 心跳、磁盘可写性或完整业务 SLA 检查；`/api/health` 保持原有兼容契约。健康端点不得作为多实例任务所有权或自动接管依据。

结案包中的两份 CSV 对公式触发符（`= + - @`，含前导空白/BOM）及前导制表符、回车、换行添加单引号，防止卷宗文本在电子表格中直接作为公式执行。原始数据库值不变；CSV 引号转义仍由标准库完成。导出不等于人工确认，不应删除防护前缀后再打开不可信文件。

## 后台阅卷

单次任务使用 `POST /api/cases/{id}/agent-jobs`，返回 `202`、`job_id` 和 `poll_url`；轮询结果包含状态、已完成步骤、run_id、最终结果及恢复资格。前端刷新后可继续追踪任务。当前单进程最多 2 个执行线程、4 个已接收任务；队列满返回 429，不是分布式 worker 平台。

服务重启将未完成后台任务标记 interrupted；有有效 LangGraph checkpoint 时可通过原 run 恢复，原生运行不具备 checkpoint 续跑。同步 `agent-chat` 仍保留向后兼容，双版本 `agent-compare` 与 `resume` 仍为同步响应，尚未提供 SSE token 流。
