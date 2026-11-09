# 隔离 MCP 验收

先执行不调用模型的协议探针：

```sh
uv run --locked python scripts/validate_application_acceptance.py --output output/mcp-acceptance.json
```

它验证真实 Python stdio 子进程，但客户端是标准库协议探针，不能写成 Cursor/Claude Desktop 已通过。服务绑定启动案件，默认离线检索，token 模式没有 stdin 身份而拒绝启动；保持该边界。

人工桌面客户端验收时，新建临时目录并初始化内置合成案件（勿使用真实案件目录）：

```sh
demo_dir=$(mktemp -d)
LAW_REVIEW_DATA_DIR="$demo_dir" uv run --locked python -c 'from app.storage.db import init_db; init_db(seed=True)'
```

在客户端提供的 MCP 配置界面填入以下模板，将路径替换为实际的绝对路径；本指南不修改全局客户端配置：

```json
{
  "mcpServers": {
    "lexvault-synthetic": {
      "command": "/absolute/path/to/project/.venv/bin/python",
      "args": ["-m", "app.integration.mcp_server", "--case-id", "1"],
      "env": {
        "PYTHONPATH": "/absolute/path/to/project",
        "LAW_REVIEW_DATA_DIR": "/absolute/path/to/new-temporary-directory",
        "LAW_REVIEW_AUTH_MODE": "local",
        "LAW_REVIEW_ALLOW_REMOTE_MODELS": "off"
      }
    }
  }
}
```

记录客户端名称/版本、协商协议、工具目录、documents 正常调用、未知工具与跨案文档拒绝。客户端不支持服务声明的 2024-11-05 版本时记为不兼容，不修改记录伪装协商成功。完成后移除该临时配置并关闭对应进程，再按目录归属清理合成数据。

真实模型验收需确认端点、模型 ID 和资源；只用合成材料，显式增加 `--live --model <实际模型> --fallback-model <实际回退模型>`。脚本拒绝外部及带凭据 URL；退出非零代表门槛未满足，完整详情在输出报告。资源准入失败不能作为“不支持 tools”的证据，也不能通过放宽内存保护获取虚假的通过。
