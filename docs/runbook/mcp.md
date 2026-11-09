# MCP 接入（只读案件工具）

`app/integration/mcp_server.py` 以 stdio + JSON-RPC 2.0 暴露案件只读工具，供支持 MCP 的客户端（Cursor、Claude Desktop 等）调用。它不引入第三方依赖，只做协议适配。

## 1. 启动

```bash
LAW_REVIEW_DATA_DIR=/path/to/data \
LAW_REVIEW_MCP_CASE_ID=1 \
uv run --locked python -m app.integration.mcp_server
```

也可用 `--case-id 1`；`--list-tools` 打印工具描述后退出，便于排查客户端为何看不到工具。

## 2. 客户端配置示例

```json
{
  "mcpServers": {
    "lexvault-case": {
      "command": "uv",
      "args": ["run", "--locked", "--directory", "/path/to/law-review-local", "python", "-m", "app.integration.mcp_server"],
      "env": {
        "LAW_REVIEW_DATA_DIR": "/path/to/data",
        "LAW_REVIEW_MCP_CASE_ID": "1"
      }
    }
  }
}
```

## 3. 暴露的工具

9 个只读工具：`search`、`get_page`、`document_outline`、`documents`、`evidence`、`saved_gap_analysis`、`case_overview`、`verify_statute`、`search_statutes`。

不暴露任何写入、审批、导出、批量导入或模型调用能力。`verify_statute` 只有 `status=ok` 才表示核验成立，`search_statutes` 永远只返回 `candidate`。

## 4. 安全边界

| 项 | 约束 |
| --- | --- |
| 身份 | 仅 `LAW_REVIEW_AUTH_MODE=local`；token 模式下进程直接退出（stdio 没有可传递的客户端身份） |
| 案件范围 | 启动时绑定单个 `case_id`，客户端无法切换 |
| 越权 | 工具层仍按服务端 `case_id` 反查文档归属，跨案 `document_id` 返回错误而非数据 |
| 输出 | 仅 JSON-RPC 写 stdout；诊断信息写 stderr，避免污染协议流 |
| 审计 | 每次调用写 `audit_log`（`Agent只读工具调用`） |

## 5. 已知限制

- 不支持 `resources`、`prompts`、采样等其它 MCP 能力，只实现 `initialize`、`ping`、`tools/list`、`tools/call`。
- 未实现 SSE/HTTP 传输；需要多客户端共享时另建网关。
- 工具调用以当前进程的工作区数据库为准，不支持多租户切换。
