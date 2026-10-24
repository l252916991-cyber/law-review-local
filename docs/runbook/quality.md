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

## 渐进质量范围

- Ruff：全部维护的 `app/`、`tests/`、`scripts/` 执行关键语法/未定义名检查；不宣称全套风格规则已达标。
- mypy：先纳管 `scripts/`，要求函数类型；历史业务模块的类型迁移逐步扩大，不用全仓 `ignore_errors` 掩盖问题。
- `.pre-commit-config.yaml` 使用同一个锁定环境的本地 hooks；`uv run pre-commit install` 是开发者可选操作。
- 前端至少执行 `node --check app/static/app.js`；交互、恢复与窄屏需独立浏览器验收。

## 真实页面流程验证

`scripts/page_flow_check.py` 用一个已安装的 Chromium 内核浏览器（Edge/Chrome/Chromium，或 `LAW_REVIEW_BROWSER` 指定路径）在隔离数据目录里启动真实 FastAPI 进程、播种合成演示案件、驱动页面并断言渲染结果；`scripts/browser_driver.py` 是仅依赖标准库的 CDP + WebSocket 客户端，不引入 npm 依赖。

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run --locked python scripts/page_flow_check.py \
  --flow metrics --output-dir output/browser-check
```

- 输出：`browser-check.json`（步骤、失败项、浏览器路径、源码 HEAD）、各步骤整页与元素截图、`server.log`。
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
