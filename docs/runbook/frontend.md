# 前端构建、验证与回滚

正式 UI 使用 Next.js / React / TypeScript，构建为静态 HTML、CSS 和 JavaScript，由 FastAPI 在同一来源提供页面与 API。`/` 与 `/mobile` 共享响应式界面，旧 `/assets/*` 入口已退役。运行时不需要 Node 服务；原有 local/token/OIDC、案件范围、SSE 与模型出口治理继续由 FastAPI 执行。

## 构建和启动

使用 `frontend/.node-version` 的 Node（24.22.1）和 `frontend/package.json` 的 pnpm（11.25.0）。首次安装需能访问包仓库，缓存齐全后可用 `pnpm install --offline --frozen-lockfile`。

```sh
uv sync --locked --group dev
pnpm --dir frontend install --frozen-lockfile
pnpm --dir frontend contract:check
pnpm --dir frontend typecheck
pnpm --dir frontend lint
pnpm --dir frontend test
pnpm --dir frontend build:static
./run.sh
```

打开 `http://127.0.0.1:8765`。应用不隐式读取 `.env`；环境变量仍需显式注入。local 模式只接受原有允许的本机访问；token/OIDC 的配置和权限策略保持适用。

`build:static` 在临时目录编译，排除开发 API 代理，成功后替换 `frontend/out/`，保留上一份成功构建为 `frontend/out.previous/`。失败时保持现有构建。构建记录 `build-info.json` 包含输入文件摘要、源码版本/脏状态及公开资源摘要；构建目录和临时产物不入 Git。不要在构建期间修改前端源文件；验证和发布前应重新构建最终候选。

FastAPI 对 HTML 禁用缓存，使用精确的内联脚本哈希配置 CSP；对 Next 内容哈希资源允许长期缓存。未构建时页面返回 503 并提示构建命令，不静默启用旧 UI；API 仍可单独用于诊断。

## 开发入口

需要热更新时，先以 token 模式启动 FastAPI，再运行：

```sh
LEXVAULT_API_ORIGIN=http://127.0.0.1:8765 pnpm --dir frontend dev
```

Next 开发服务只监听回环地址，`/api/*` 代理保留公开 Host、会话与代理标记。**代理不适用免登录 local 模式**，该模式仍拒绝转发访问。正式静态部署浏览器直接请求 FastAPI，不经过这层代理。不得通过删除代理标记或增加全网可信地址绕过限制。

## 浏览器验证

```sh
pnpm --dir frontend exec playwright install chromium
pnpm --dir frontend test:e2e
```

本机已有 Chromium 系浏览器时可显式设置 `LEXVAULT_BROWSER_PATH` 为其可执行路径。CI 使用 Playwright Chromium。测试启动独立 FastAPI、随机临时令牌、临时合成数据目录和模型测试替身，默认加载当前 `frontend/out/`。`LEXVAULT_E2E_MODE=next` 可验证生产 Next 开发代理构建（先 `pnpm --dir frontend build`）。两种结果须分别标注；均不证明真实模型效果。

`frontend/test-results/` 保存本机截图及失败 trace，不入库。测试后修改前端代码需重建并重跑受影响流程，不能用旧构建的页面结果放行。

## 发布包与 Docker

纯源码包默认不包含构建产物。需要可直接运行的 UI 时先构建，再显式打包：

```sh
uv run --locked python scripts/package_release.py --with-frontend --output dist/lexvault-ui.zip
```

发布脚本只加入 `build-info.json` 绑定且哈希匹配的公开资源，拒绝篡改、越界与符号链接；不会把任意 `out/` 文件、源码缓存、凭据或案件材料打包。对最终 ZIP 解包后使用隔离数据目录验证启动、资源、登录、API 与 SSE，另行记录源码和发布清单摘要。

Docker 使用 Node 构建阶段，最终镜像仍为非 root Python 单进程，模型服务不在镜像内。构建参数 `NODE_IMAGE`、`PYTHON_IMAGE` 可指定经过核对的兼容基础镜像或镜像仓库；验证报告必须记录实际参数，不能把替代镜像构建当成默认仓库连通性通过。

## 回滚

停止当前服务并保留运行数据；只回滚 UI 时可切换上次成功构建：

```sh
mkdir -p output/frontend-rollback
rollback_bundle=$(mktemp -d output/frontend-rollback/failed.XXXXXX)
mv frontend/out "$rollback_bundle/ui"
mv frontend/out.previous frontend/out
./run.sh
```

先确认 `out.previous/` 存在且与当前后端 API 契约兼容。完整版本回滚使用上次已验证发布包和原启动配置，不改写 Git 历史、不恢复真实数据库到测试快照。旧版 `app/static/` 前端已于 2026-11-01 从仓库删除，需要核对时查看 `v0.4.0` 之前的提交；恢复旧 UI 需使用对应旧版发布包，正式入口不并行提供旧桌面/移动前端。
