# E33 deployment acceptance matrix

状态：工程文件与验收步骤已提供；Docker/TLS/目标硬件的真实验收必须由部署负责人执行并填入证据。

## 记录模板

- 日期/时区：
- 执行人：
- 主机 OS / Docker 版本：
- Git commit：
- 镜像 digest：
- Compose 配置 SHA-256：
- 数据目录/备份工件（不得包含真实材料于代码库）：
- 结果：PASS / FAIL / BLOCKED

## 1. 干净机器安装

```sh
uv run --locked python scripts/doctor.py
docker version
docker build --pull --tag lexvault-local:<git-sha> .
docker compose config --quiet
docker compose up -d
docker compose ps
docker compose exec web id -u
curl --fail http://127.0.0.1:${WEB_PORT:-8000}/api/health
curl --fail http://127.0.0.1:${WEB_PORT:-8000}/api/system/health
sha256sum docker-compose.yml
```

`docker compose exec web id -u` 必须为 `10001`。日志抽查不得包含 API token、OIDC secret、Redis 密码、原始卷宗文本或模型 prompt。

## 2. 数据与会话持久化

1. 通过代理登录一个测试主体，保存 cookie；访问 `/api/auth/me` 和授权案件。
2. 只重启 Web：`docker compose restart web`；复用 cookie，确认会话仍有效。确认 `auth_sessions` 的哈希行未保存原始 cookie。
3. `docker compose down` 后再 `up -d`，确认命名 `web-data` 卷仍保留案件、原件哈希、模板和会话。
4. 按 `docs/runbook/release.md` 执行停写快照；恢复到新目录，执行 SQLite integrity/FK 检查与附件哈希检查，再确认案件、证据、审批和模板可读。
5. 清空 Redis 后重启 worker，确认批量导入 outbox 自动重派；记录实际恢复时间。

## 3. TLS 代理边界

使用固定版本的 Caddy/Nginx 与一次性本地 CA/测试域名，禁止把测试证书或密钥提交到仓库。

- HTTP 自动跳转 HTTPS。
- HTTPS 返回明确 HSTS（例如 `max-age=31536000; includeSubDomains`，由代理提供）。
- cookie 包含 `Secure; HttpOnly; SameSite=Strict`。
- 配置域名在 `LAW_REVIEW_ALLOWED_HOSTS` 内可用，任意 Host 返回 400。
- Web 后端仅 loopback 可达，外部只能通过代理访问。
- 注入 `Forwarded`、`X-Forwarded-For`、`X-Forwarded-Host`、`X-Forwarded-Proto`，确认代理替换/剥离客户端伪造值；本机模式误经代理应被拒绝。
- 若启用 OIDC，callback redirect URI 必须是 HTTPS 域名。

## 4. 双用户隔离

配置两个合成主体并同时登录：

- 各自只能读取被授权案件及其文档、证据、会话、任务、导出。
- 跨案件读/写/导出均拒绝，拒绝事件带正确 actor 和 request_id。
- cookie 与 Bearer 两种认证路径均验证。
- 重启 Web 后两主体的会话仍可用；注销、过期、凭证轮换立即失效。

## 5. 升级与回滚

1. 准备受支持的 v6 数据库副本并停写。
2. 备份后启动 v7：确认 `PRAGMA user_version=7`、业务数据/审批/模板/会话保留。
3. 注入迁移失败，确认 DDL 与版本号一起回滚。
4. 应用回滚使用匹配备份恢复，禁止手工降低 `user_version`。
5. 记录命令、时间、备份路径、恢复耗时和差异清单。

## 6. 容量冒烟（不等同 SLA）

在目标硬件和目标模型服务执行：最多 50 个并发读、最多 5 个模型请求；报告 p50/p95、错误率、队列长度、CPU/内存/磁盘/WAL 峰值和恢复时间。结果只定义当前可支持范围，不构成 SLA 或高可用承诺。

当前代码验收边界：Dockerfile/Compose 与 loopback 绑定已静态检查；本机 Docker daemon 不可用时，不得把静态检查写成镜像构建通过。TLS、干净机器、真实 Redis/模型和目标容量必须有实际部署证据。
