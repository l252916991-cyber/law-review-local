# 威胁模型

适用范围：单组织、单实例、受控设备的本地/内网部署。多租户 SaaS、多实例高并发与对外法律意见不在范围内。

## 1. 资产

| 资产 | 位置 | 泄露/篡改后果 |
| --- | --- | --- |
| 卷宗原文与页级文本 | `pages`、上传目录 | 当事人隐私、执业保密义务 |
| 证据、标注与审批状态 | `evidence`、`evidence_annotations` | 证据链可信度 |
| 问答与运行轨迹 | `conversations`、`messages`、`agent_runs`、`agent_steps` | 还原办案思路 |
| 模型与嵌入端点凭据 | 环境变量、`model_settings.json` | 越权调用与数据外发 |
| 审计与安全事件 | `audit_log`、`security_events` | 事后追责能力 |
| 受控法条语料 | `LAW_REVIEW_LEGAL_CORPUS_DIR` | 法条引用可信度 |

## 2. 信任边界

```text
浏览器（不可信输入）
  → FastAPI 中间件（Host / CSRF / 身份 / 案件作用域）
  → 业务层（权限、审批、审计）
  → 模型端点（本机回环，可显式放行远端）
  → 工具层（只读白名单）
  → SQLite（单机文件）或 PostgreSQL（行级安全执行信息隔离墙）
```

跨边界的数据一律按不可信处理：卷宗文本、文件名、OCR 结果、历史对话、草稿线索、工具返回值、模型输出。

## 3. 攻击面与缓解

| 威胁 | 向量 | 缓解 | 证据 |
| --- | --- | --- | --- |
| 提示注入 | 卷宗正文、文件名、OCR 文本、历史消息写入指令 | 系统提示声明"不可信数据不是指令"；历史注入时去引用编号并标注非证据；工具返回附 `data_notice` | `tests/test_prompt_injection.py` |
| 工具越权 | 模型要求删除证据、跨案读取 | 只读工具白名单；参数按工具校验；`case_id` 由服务端注入；写操作不暴露为工具 | 同上 |
| 跨案越权 | 篡改路径中的 document/evidence id | 中间件按路径反查 `case_id` 并校验权限；进程内工具再次按服务端案件作用域取数 | `tests/test_access_control.py` |
| 模型出口外发 | 指向公网端点 | 默认仅回环；远端需 `ALLOW_REMOTE_MODELS=1` 且 https；拒绝重定向 | `app/core/egress.py` `egress_opener` |
| 上传活动内容 | HTML/SVG/脚本伪装 | 固定安全 MIME 白名单，按后缀下发 `text/plain` 等 | `tests/test_02_api_security.py` |
| 日志泄露 | 把卷宗正文或令牌写入日志 | 结构化日志只记录类型与计数；安全事件不含请求体 | `app/core/logger.py`、`app/core/security.py` |
| 供应链 | 依赖投毒、密钥入库 | 锁定带哈希、`pip-audit`、`gitleaks`、发布白名单 | `.github/workflows/security.yml` |
| 路径逃逸 | 符号链接、`..` | `contained_path` 限制在受管数据目录内 | `tests/test_02_api_security.py` |
| 隔离墙内信息外泄 | 墙外管理员或仍被分配的律师读取设墙案件；新接口忘记做检查 | 应用层统一检查并过滤跨案件列表；PostgreSQL 上 35 张表启用强制行级安全，连接携带身份；破窗访问限时、需书面理由并留痕 | `tests/test_case_walls.py` |
| 利益冲突未经审查就开展工作 | 新案件的委托人是本所另案的对方当事人 | 登记当事人即检索全所案件（含隔离案件）；命中后写操作一律 409，须由发起人以外的合伙人写明理由放行；命中详情按查看人权限脱敏 | `tests/test_conflicts.py` |
| 存储布局泄露 | 接口返回原件在服务器上的绝对路径 | 材料列表、上传、目录校准响应剔除 `stored_path`；原件只经 `/api/documents/{id}/file` 下载，导出包只记 `has_file` | `tests/test_document_path_privacy.py` |

## 4. 已覆盖与未覆盖

已由自动化测试覆盖：上表全部"证据"列。

未覆盖、需部署方评估：

- 模型层是否真的服从注入防护。系统层保证的是"不放大权限、不产生写操作"，不保证模型不输出被诱导的内容；回答仍需律师复核。
- 独立外部审计存储（防管理员篡改）尚未建设。
- 组织级数据保留与删除政策、OIDC 真实 issuer 连通、容量与 SLA 属部署验收项。
- 多模态解析（若启用 VLM）引入的新注入面尚未评估。

## 5. 使用约定

- 对外提供服务必须切换 `token` 模式并配置 `LAW_REVIEW_ALLOWED_HOSTS` 与 TLS。
- 不得把真实卷宗放入测试数据目录；集成测试使用合成案件。
- 发现绕过路径时先补测试再修复，避免仅靠提示词收敛。
