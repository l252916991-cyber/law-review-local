# A11 前端重写实施记录

日期：2026-10-25。开始基线：`5dc803c`，分支 `codex/ai-app-upgrade`。依据[续行计划](ai-application-upgrade-continuation-plan-20261024.md)；功能范围见[迁移清单](frontend-migration-inventory-20261025.md)。

## A11.1 工程与 API 契约

- 建立 Next.js 16.4.3 / React 19.3.1 / TypeScript 5.9.3 工程，固定 Node 24.22.1、pnpm 11.25.0 和依赖锁文件。ESLint 使用与 Next 插件 peer 范围兼容的 9.39.5；该版本包提示已停止支持，后续需待插件支持 ESLint 10 时整体升级。依赖审计当前未报告已知漏洞。
- FastAPI OpenAPI 导出在隔离临时数据目录执行；生成 HTTP 契约与聊天请求类型，单独实现 SSE 分片、UTF-8、CRLF、事件格式和终态验证。
- Next 仅转发固定后端 `/api/*`，保留 Host/Origin、会话 cookie、取消和流式响应；采用 Node HTTP transport，因为 Node fetch 会替换 Host，真实登录测试曾因此触发 FastAPI CSRF 拒绝。新增代理测试使用真实本地 HTTP 服务验证。
- 代理必须配合 FastAPI token 模式，保留代理标记使 local 模式拒绝代理请求；未改默认 FastAPI 启动入口，未打开远端访问。页面目前只有工程落地页，业务页面等待 A11.2/A11.3。
- 增加前端 CI：锁定安装、契约、类型、lint、协议/代理测试、生产构建、依赖审计。前端源码按明确目录及精确配置文件进入发布允许清单，依赖、构建产物、凭据和页面截图排除。
- 发布复验发现原允许清单遗漏 `benchmarks/answer_eval/v1/demo-case.json`，使发布包不能复现现有答案集测试；补入该合成 fixture 的精确路径并添加断言。

## 验证证据

| 检查 | 结果与边界 |
| --- | --- |
| 本机工具 | Node v24.22.1、pnpm 11.25.0 的版本命令与依赖安装实跑成功；历史包管理器阻塞解除 |
| 冻结依赖 | `pnpm install --offline --frozen-lockfile` 通过（先联网填充依赖缓存）；无 peer 依赖冲突 |
| 前端 | 契约检查、类型检查、lint、生产构建通过；8 项协议/代理测试通过；`pnpm audit` 未报告已知漏洞 |
| 实际 HTTP 联调 | 新进程 Next 生产构建 + FastAPI，隔离合成库：匿名 401、HttpOnly 登录、案件创建/读取、跨站 403、页面 HTML 均通过；未使用模型 |
| 原工作区完整离线复跑 | 1254 passed / 2 failed / 1 skipped，覆盖率 85.74%；失败仍为既有两项外部语料断言，见 `output/a11-baseline/` |
| 干净发布包完整离线复跑 | 1248 passed / 10 skipped，覆盖率 85.73%；无失败。Python 3.12.13 + 独立框架解释器。未带外部冻结语料/受许可限制的 LawBench，原用例自然跳过；另有 mlx_lm 缺失跳过，未删除或修改断言 |
| 工程检查 | Ruff、Mypy、工程脚本 9 项测试与发布允许清单检查通过；未替代 Python 3.11/3.14 矩阵或真实模型验收 |

干净发布包测试证明该包的离线工程检查可复现，不能据此宣称原工作区的两项语料失败已修复。测试替身/合成数据与实际模型、真实业务效果继续分列。A11.2–A11.4 尚未完成，未切换正式入口、推送或发布。
