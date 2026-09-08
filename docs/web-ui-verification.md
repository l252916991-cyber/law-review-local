# Web UI 可读性与响应式验证

2026-09-08：保留绿色工作台风格，调整正文与辅助信息字号、侧栏对比度、首页文案和工作流布局；900px 以下使用带文字的导航及可横向滚动的案件列表，恢复新建案件入口。目录工具栏可换行，表格在自己的容器内横向滚动。聊天正文使用 15px 字号。

无障碍改动包含键盘焦点提示、跳至主要内容、当前导航 `aria-current`、可聚焦的模型开关，以及减少动态效果偏好。`hidden` 元素继续保持隐藏，移动端显示规则不会覆盖权限控制。

## 验证环境

使用当前工作区代码启动新的 Uvicorn 进程，数据目录为 `/tmp/lexvault-ui-20260908`，仅使用虚构演示数据；Playwright CLI 驱动 Microsoft Edge。

```sh
LAW_REVIEW_DATA_DIR=/tmp/lexvault-ui-20260908 \
LAW_REVIEW_AUTH_MODE=local \
LAW_REVIEW_LLM_URL=http://127.0.0.1:1/v1 \
REDIS_URL=redis://127.0.0.1:1/0 \
uv run --locked uvicorn app.main:app --host 127.0.0.1 --port 8876
node --check app/static/app.js
```

## 已通过的检查

- 320、390、768、1024、1440px 五种宽度，逐一点击案件总览、双层目录、证据关系、AI 阅卷、Agent 实验室、成果导出：共 30 个组合。断言文档和主容器无横向溢出，`aria-current` 与所选页面一致。
- 390px 下实际创建虚构案件，切换回演示案件，并检查页首案件标题更新。
- 目录输入不存在的材料名称，记录数变为 0；清空搜索恢复目录。
- 手机聊天输入框可填写、清空；未发送模型请求。
- 桌面按 Tab 首先聚焦“跳至主要内容”，按 Enter 后焦点进入主内容区。
- 桌面总览、手机总览、手机聊天截图人工检查；最终布局检查启用减少动态效果偏好。
- JavaScript 语法检查、Git 差异空白检查通过；浏览器控制台无错误或警告。

截图存放于本地 `output/playwright/ui-desktop.png`、`ui-mobile.png`、`ui-mobile-assistant.png`，不纳入发布代码。

这些检查覆盖本次前端呈现与操作。未执行完整后端 CI、模型基准、真实案件流程；隔离环境主动禁用模型服务和 Redis，批量导入不可用。
