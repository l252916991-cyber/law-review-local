# 质量门禁与依赖锁

## 依赖与干净安装

`pyproject.toml` 定义 Python 3.11–3.14 与直接运行/开发依赖，`uv.lock` 锁定跨平台传递依赖。`requirements.txt` 是运行依赖的带 hash 导出，不应手工修改。推荐 `uv sync --locked --group dev`；只有 pip 时使用独立虚拟环境安装运行导出。`requirements-dev.txt` 是包含开发工具的完整带 hash 导出。

```bash
uv lock
uv export --locked --no-dev --no-emit-project --no-header --format requirements-txt --output-file requirements.txt
uv export --locked --all-groups --no-emit-project --no-header --format requirements-txt --output-file requirements-dev.txt
uv sync --locked --group dev
uv pip check
```

更新依赖属于显式维护操作；测试/CI 用 `--locked`，不隐式修改锁文件。[uv 锁定与同步文档](https://docs.astral.sh/uv/concepts/projects/sync/)

## 离线测试

CI Python 3.11/3.14 运行全部 `tests/` 与工程脚本侧测试（发布/模型安装/备份恢复），使用 `pytest-socket` 禁止网络连接、允许仅用于事件循环的 Unix socket。模型、Redis 依赖应通过 mock/fake 隔离；不使用 `continue-on-error` 把模型服务缺失伪装成成功。CI 禁止自动载入未知 pytest 插件并显式启用 pytest-cov/pytest-socket。

M2 实际进程测试另需独立 Python 3.12 框架环境：先用 `uv run --locked python -m scripts.setup_framework --output .venv-framework-local` 创建新环境，设置 `LAW_REVIEW_FORGE_PYTHON="$PWD/.venv-framework-local/bin/python"` 后运行离线套件。安装使用随包的带 hash 依赖导出和 wheel；可加 `--offline` 使用完整缓存。CI 已自动准备该环境；本地未设置变量会明确跳过实际框架进程测试，不能据此声称 M2 集成通过。用例固定关闭真实模型与远程检索服务。

输出真实 `junit.xml`、pytest 日志、覆盖率 XML/JSON/HTML；任何测试失败或分支覆盖率低于 70 都阻止通过，失败报告也上传。未在托管环境实际运行前，不能把本地验证表述为远程 CI 已通过。

## PostgreSQL 与答案质量门禁

CI 的 `postgres` 任务用 PostgreSQL 16 + pgvector 再跑一遍同一套离线测试（每个测试一个新库，测 SQLite 文件本身的用例标 `sqlite_only`），并在 PostgreSQL 上运行答案质量门禁；`test` 任务在 SQLite 上运行门禁。门禁口径见[答案质量门禁](eval-gate.md)，后端差异见 [PostgreSQL 后端](postgresql.md)。

## 渐进质量范围

- Ruff：全部维护的 `app/`、`tests/`、`scripts/` 执行 `E9`（语法）、`F`（Pyflakes：未使用/未定义名、重复定义、无用赋值）、`I`（导入排序）、`UP`（按 Python 3.11 升级语法）与 `B`（bugbear）；测试目录对 `F811` 例外，因为从兄弟模块导入的 pytest fixture 会以同名参数注入。`B905`（zip 未指定 strict）与 `B023`（循环内闭包）暂不启用：前者需要逐处评估截断语义，后者经 2026-11-01 逐处核对均在同一轮迭代内调用。FastAPI 的 `File()`/`Body()` 等默认参数登记为不可变调用。
- mypy：`scripts/` 要求完整函数类型；`app/` 自 2026-11-01 纳入检查，先用宽松配置（检查已标注的代码，未标注函数体暂不检查）。当日仍有类型错误的 22 个模块逐个列在 `pyproject.toml` 的 `ignore_errors` 名单中，名单只减不增；其余业务模块的新错误会直接让门禁失败。不用全仓 `ignore_errors` 掩盖问题。
- `.pre-commit-config.yaml` 使用同一个锁定环境的本地 hooks；`uv run pre-commit install` 是开发者可选操作。
- 正式前端执行 `pnpm --dir frontend contract:check`、`typecheck`、`lint`、`test`、`build:static` 和受影响的 `test:e2e`。旧版 `app/static/` 前端已于 2026-11-01 删除，不再有对应门禁。详见[前端运行手册](frontend.md)。

## 曾经的偶发失败（已修复）

`tests/test_eval_transport_faults.py::test_all_four_real_faults_fail_closed_on_both_client_paths` 曾偶发失败，空闲时单独跑也约三次失败一次。根因：演练用 0.35 秒的评测任务截止模拟超时，但计时从发出开始信号算起，而子进程在调用模型之前还要检索、拼提示词，机器稍慢时截止先到，模型调用还没开始，`injection_reached_generation` 为假。

2026-11-02 改为在测试端点收到生成请求、模型调用确实卡住之后才开始计时，截止时长和断言都没有放宽。修复后单独连跑 15 次、三路并行加 CPU 压力再跑 12 次，全部通过。演练协议里的 `task_deadline_clock` 字段记录了这一口径。

## 真实页面流程验证

2026-10-25 起，`scripts/page_flow_check.py` 是当前 Playwright 验收的兼容入口：启动隔离 FastAPI、合成数据与模型测试替身，加载当前静态构建。保留 flow 名称，计数与历史 CDP 步骤数不可互换；`--data-dir` 不再复用数据。先安装锁定前端依赖并 `pnpm --dir frontend build:static`，浏览器通过 `--browser`、`LEXVAULT_BROWSER_PATH` 或 Playwright Chromium 指定。历史 CDP 驱动保留为工具源码，正式 UI 不使用旧 DOM 流程。

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run --locked python scripts/page_flow_check.py \
  --flow metrics --output-dir output/browser-check          # 用量与费用面板：正常/空案件/部分缺失/失败/快速切案
UV_CACHE_DIR=/tmp/uv-cache uv run --locked python scripts/page_flow_check.py \
  --flow chat --output-dir output/browser-check-chat       # 流式问答：引用回原文、撤回、同键回放、消息落库
UV_CACHE_DIR=/tmp/uv-cache uv run --locked python scripts/page_flow_check.py \
  --flow workbench --output-dir output/browser-check-wb    # Agent 工作台：工具轨迹、回退徽标、切案清理
UV_CACHE_DIR=/tmp/uv-cache uv run --locked python scripts/page_flow_check.py \
  --flow agent --output-dir output/browser-check-agent     # API 级：工具循环、运行轨迹、用量记账
```

- 输出：`browser-check.json`（步骤、失败项、浏览器路径、源码 HEAD）、各步骤整页与元素截图、`server.log`。
- `--flow chat` / `workbench` / `agent` 会启动 `scripts/stub_model.py`：一个 OpenAI 兼容的**模型测试替身**，答案是固定字符串、embedding 是哈希向量、rerank 按位置给分，并可按问题标记返回一次不合格答案以触发回退。它只用于验证协议、引用契约与页面行为；报告里 `model` 字段标为 `stub`，不能当作真实模型效果。
- 报告步骤名与任务包对应：`wb-01-run`/`wb-02-trace`/`wb-03-fallback`/`wb-04-switch` 对应 A06.1 的 Agent 轨迹、回退与切案清理验收；`chat-*` 对应 A01.1；`0x-*` 对应 A02.1/A02.2。
- 未安装受支持浏览器时脚本退出码 2 并输出 `browser unavailable`；这属于未验证，不是通过。
- 该验证不联网、不写真实案件数据；模型调用另由真实模型验收单列，不用页面替身代替。
- 浏览器在 CI 镜像中默认不存在，因此 CI 只保证编译与逻辑层测试；页面流程证据需在具备浏览器的宿主上生成并记录版本。

## GitHub Actions 版本来源

截至 2026-09-05，使用官方已发布 tag 对应的完整 commit SHA，而非可变主版本标签：

- [checkout v7.0.1](https://github.com/actions/checkout/releases/tag/v7.0.1)
- [setup-python v7.0.0](https://github.com/actions/setup-python/releases/tag/v7.0.0)
- [setup-node v6.1.0](https://github.com/actions/setup-node/releases/tag/v6.1.0)
- [upload-artifact v7.0.1](https://github.com/actions/upload-artifact/releases/tag/v7.0.1)

工作流使用 GitHub 托管 Ubuntu 24.04 runner；自托管/企业环境应先核对各 action 的 runner/runtime 兼容要求。
