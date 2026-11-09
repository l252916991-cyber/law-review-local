# 本地三分钟合成演示

使用 A11 静态界面、真实隔离 FastAPI 和模型固定答案测试替身。视频无配音，持续显示“合成数据 / 模型测试替身”；不能作为真实模型效果或业务验收。脚本不修改真实案件、客户端配置、模型服务或内存保护。

```sh
pnpm --dir frontend install --frozen-lockfile
pnpm --dir frontend build:static
pnpm --dir frontend exec playwright install ffmpeg
LEXVAULT_BROWSER_PATH=/absolute/path/to/chromium-family-browser \
  node frontend/scripts/record-demo.mjs --output output/new-demo
```

输出目录必须新建。脚本拒绝复用占用的 13000 端口，生成随机临时令牌，调用隔离服务脚本启动临时合成数据库；退出时停止自身服务和浏览器。不要把脚本改指向运行中的真实服务。仅输出合成截图、WebM、环境/来源摘要和服务日志，默认不入 Git。

| 秒数 | 场景 | 证据边界 |
| --- | --- | --- |
| 0–12 | 登录与范围说明 | 合成库、随机令牌 |
| 12–42 | 建案、导入虚构文本 | 实际 UI/API 文件导入 |
| 42–72 | SSE 提问及确认引用 | 模型响应为测试替身 |
| 72–92 | 引用回到本次导入原文 | 实际来源定位 |
| 92–120 | 法条核验节点缺少语料 | 关闭模型，节点明确“无法核验”；不冒充普通聊天统一强制拒答 |
| 120–145 | Agent 工具、校验与回退轨迹 | 测试替身主/回退模型，不是真实双模型验收 |
| 145–170 | 用量与未知成本 | 不配置价格，不宣称免费或已知账单 |
| 170–180 | 限制与待验事项 | 真实模型、人工一致性、项目许可证待完成 |

`demo-report.json` 包含实际场景时间、源码版本/脏状态、相关运行文件与前端构建摘要、完成状态。`ok=false` 的失败录制不能作为交付通过。服务必须完整结束后再核对 WebM 时长、解码和关键帧。

本轮最终录制在 `output/a131/demo-04/`。早期失败录制与日志保留为负结果：向普通聊天询问未核验法条时，固定答案替身仍可能返回格式通过的卷宗事实；最终视频改为展示已验证的法条专用节点边界，未修改产品引用契约或把普通聊天描述成法律有效性判断。

原始 WebM 容器实测 189.84 秒（录制/编码时钟与场景时钟有偏差）；交付版 `lexvault-demo-180s.webm` 用 FFmpeg `-itsscale 0.9481668773704172`（180/189.84）及 `-c copy` 等比归一为 **180.00 秒**，不重编码、不删除场景，原录制保留。`presentation-manifest.json` 记录完整命令及原始/交付 SHA-256，`presentation-validation.log` 记录解码检查。

英文说明见仓库 `README.en.md`。项目无项目级 LICENSE；第三方组件许可元数据只作清单，对外发布前仍须维护者确认权利并选择许可证。视频、源码和在线体验均未公开发布。

## 恢复校验片段（关闭模型，可离线复现）

上面的 180 秒成片使用模型固定答案替身，不能作为“真实模型响应”的演示。恢复/拒用片段与此无关，可以关闭模型单独复现，并应在新版演示中单独截取：

```sh
D=output/portfolio-demo/anomaly-YYYYMMDD
uv run --locked python scripts/ops/demo_recovery_check.py --phase interrupt \
  --data-dir "$D/case-reuse" --output "$D/01-interrupt.json"
uv run --locked python scripts/ops/demo_recovery_check.py --phase resume \
  --data-dir "$D/case-reuse" --output "$D/02-resume.json" --state "$D/01-interrupt.json"
uv run --locked python scripts/ops/demo_recovery_check.py --phase tamper \
  --data-dir "$D/case-tamper" --output "$D/03-tamper.json"
```

三条命令各自独立进程：`interrupt` 在审校完成后合成中断，`resume` 把保存的审校结果原样复用且不重复专家节点，`tamper` 改动一页来源后拒绝恢复且不消耗重试次数。证据 JSON 含运行标识、步骤数、`resume_count` 与审校摘要；画面必须同时录到 `model_disabled=true`，不得把该片段描述成模型效果。

真实问答、专家交接、候选分歧与导出的主演示仍须在登记模型端点可用后重录（见 [P3 记录](../history/migration/portfolio-p3-demo-status-20261027.md)），不得沿用固定答案替身的成片充当真实模型响应。
