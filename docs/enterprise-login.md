# 企业登录

登录界面沿用原生 HTML/CSS/JavaScript，未认证和身份检查失败时不显示工作台；按公开配置显示组织 SSO 或令牌入口。支持令牌显示切换、提交防重、失败提示、网络重试。业务请求（包括导出）收到 401 后重载到登录页，清除当前工作区内存；仅保存案件 ID 和导航页名用于重认证后恢复，并依据重新获取的授权案件列表验证。退出只注销本系统当前会话，不代表身份平台全局退出。未配置 token 模式的本机部署保持免登录。

本地验证：完整离线测试、静态检查、依赖导出和发布清单检查通过；隔离浏览器验证了令牌错误/成功、加载态、退出和 390px 窄屏布局。浏览器自动化普通点击通道超时，按钮处理使用页面原生 click 事件验证，尚不替代人工键盘/读屏验收或 WCAG 认证。真实身份平台、生产代理与 MFA 策略仍需部署验收。


`GET /api/auth/me` 向未登录客户端公开 `organization_name`、`support_contact`、`oidc_enabled`。前两项分别来自 `LAW_REVIEW_ORGANIZATION_NAME` 和 `LAW_REVIEW_SUPPORT_CONTACT`，默认空字符串、去掉首尾空白，只是 JSON 文本。前端必须使用 `textContent`，不得作为 HTML、链接地址或脚本解析。不要在这些公开配置中填写秘密。

组织登录沿用 `LAW_REVIEW_AUTH_MODE=token`，仍需有效的 `LAW_REVIEW_API_TOKENS_JSON`。OIDC 配置为 `LAW_REVIEW_OIDC_ISSUER`、`LAW_REVIEW_OIDC_CLIENT_ID`、`LAW_REVIEW_OIDC_CLIENT_SECRET` 和 `LAW_REVIEW_OIDC_PRINCIPALS_JSON`；映射键是经签名验证的 `sub`，不会回退用 email 授权。默认 scope 为 `openid profile`。部分配置会关闭登录并返回固定错误；identity 对配置错误保持 503。

OIDC 使用授权码 + PKCE S256。`GET /api/auth/oidc/login` 生成独立随机 state、nonce、浏览器凭据和 verifier。`lexvault_oidc_flow` cookie 为 HttpOnly、SameSite=Lax、600 秒，Path 为 `/api/auth/oidc/callback`；HTTPS 或非本地主机带 Secure。数据库仅保存 state/browser 凭据摘要，保存 nonce/verifier、发起时 redirect URI 和配置摘要，10 分钟过期。回调以一条 `DELETE ... RETURNING` 原子领取并删除匹配浏览器的 flow，回放、无 cookie、错误 cookie、过期或部署配置变更均失败。同一浏览器重新开始登录会覆盖 cookie，之前的窗口需重新发起。

只有 token 模式下精确路径 `/api/auth/oidc/callback` 的 GET 请求绕过跨站来源检查，其余 API、其他方法、尾随斜线、Host 检查和权限检查保持原规则。回调验证签名、issuer、audience、expiry、sub 和 nonce；只接受明确列出的 RSA/ECDSA 签名算法。provider 的错误正文、error_description、异常文本不会进入用户响应。

浏览器登录失败返回 302 到 `/?auth_error=固定码`，成功返回 `/`；均清除 flow cookie。当前码为 `state`（无效、过期或重放 flow）、`unavailable`（未配置、配置错误、启动服务错误或发起限流）、`denied`（身份未获授权）、`failed`（provider 拒绝或验证失败）。前端可额外保留 `expired` 映射。会话撤销/创建存储错误必须真实返回 HTTP 503；OIDC 的此类 503 也清除 flow cookie，不能返回登录成功或退出成功。

令牌登录通过数据库事务按 ASGI 来源地址累计失败次数：15 分钟窗口最多 10 次失败，之后包括正确凭据在内均返回 429，窗口到期恢复。成功不重置失败计数，避免有效账号掩盖攻击。OIDC 使用独立的发起保护：每来源地址在 15 分钟窗口最多预留 100 次尚未成功完成的请求，并保留全局 1024 个有效 flow 容量上限。每个 flow 保存所属计数桶和窗口到期时间；成功创建会话时，在同一事务中只释放本 flow 的一个计数，不重置其他失败/未完成请求，也不会扣减新窗口计数。因此同一律所 NAT 的连续成功登录不累计封锁；失败、放弃和 discovery 错误仍占用额度直到窗口到期。回调只有已领取的有效 flow 才能换取 token，失败回调不可重复使用 flow。不信任客户端提交的 `X-Forwarded-For`；反向代理需要安全设置 ASGI client 地址，否则代理后用户共享一个桶。此处限流不覆盖直接 Bearer API 调用。

会话保留 8 小时绝对期限，新增固定 30 分钟闲置期限；有效 cookie 请求原子刷新活动时间但不延长绝对期限。后台 identity 轮询也算活动。登出数据库删除失败返回 503，保留 cookie 让客户端重试。令牌登录轮换旧会话亦遵循此规则。浏览器会话 cookie 保持 HttpOnly / SameSite=Strict。同站发起 OIDC 时把当前 session 的哈希绑定到数据库 flow，不保存原 cookie 或令牌。跨站 callback 即使不携带 Strict session cookie，也会在成功验证后用绑定哈希撤销旧会话，并在同一事务中创建新会话、释放 flow 计数；任何一步失败全部回滚，返回 503，旧会话仍可重试注销。

数据库版本 11 沿用 `init_db` 的事务迁移，增加 `oidc_flows`、`auth_login_limits` 和 `auth_sessions.last_seen_at`。版本 12 增加 flow 的旧会话哈希和计数窗口绑定，清除无法安全轮换旧会话的 v11 在途 flow，升级时这些用户需要重新发起登录。旧会话从升级时开始闲置计时，但原绝对到期时间不变。认证 flow、限流和 session 均不依赖进程内状态，可在共享同一 SQLite 文件的 worker 间使用；这不代表应用已经支持多 Web worker：现有 `process_ownership()` 仍限制单 Web owner，本次未改变其恢复/队列约束。数据库文件须放在支持 SQLite 锁语义的本地文件系统。

自动测试使用隔离临时数据库、真实签名的模拟 IdP、禁用外部 socket，覆盖浏览器绑定、PKCE、原子消费、有效与无效 claims、权限、精确跨站例外、闲置和绝对期限、持久限流、迁移以及撤销失败。真实 IdP 连通性、代理配置、浏览器端到端 SSO 和生产多 worker 部署仍需部署验收。
