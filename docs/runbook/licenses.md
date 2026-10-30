# 组件许可清单

生成于 2026-09-07（Asia/Shanghai），对应 `uv.lock` 当时解析的运行时依赖与本目录 vendored 前端库。用途：交付前核对分发许可。注意两点边界：

- 表中的"许可声明"来自组件自带的包元数据，是发布方的自我声明，不构成法律核验；正式交付前应由负责人抽查关键组件的 LICENSE 原文。
- Python 表来自 `requirements.txt`（含哈希锁定版本）；升级依赖或准备新版本发布时，应重新生成并核对本表。

## 2026-10-25 前端重写补录

以下版本/声明来自本机锁定安装包的 package.json，与 pnpm-lock.yaml 对照；只覆盖列出的直接依赖，不等同于全部传递依赖 SBOM。上面的日期与下方 Python 表保留历史口径，不能当作当前全量许可审查完成。

| 组件 | 固定版本 | 包元数据许可声明 |
| --- | --- | --- |
| Next.js | 16.4.3 | MIT |
| React / React DOM | 19.3.1 | MIT |
| TypeScript | 5.9.3 | Apache-2.0 |
| Playwright Test | 1.64.1 | Apache-2.0 |
| ESLint | 9.39.5 | MIT |
| Prettier | 3.9.10 | MIT |

录屏使用 Playwright 管理的 FFmpeg 1013，本机缓存附 COPYING.LGPLv2.1；二进制不进入源码发布包。新前端通过 Next 构建生成，不再从正式路由加载下列历史 vendored 库；归档文件的声明继续保留。

项目目前没有项目级 LICENSE。维护者的权利确认、项目许可证选择、完整依赖/数据再分发核对仍是对外发布前置条件；本轮没有自行赋予开源许可或公开发布。

## 仅作设计参照、未引入代码的第三方

以下项目只影响本仓的实现思路，没有代码、二进制或依赖被复制或打包，因此不进入上面的依赖表；记录在此是为了说清设计来源，避免日后误以为可以按 MIT 条款主张分发。

| 项目 | 许可 | 本仓如何使用 |
|---|---|---|
| [pi-mono](https://github.com/badlogic/pi-mono)（`pi-ai` 包） | MIT | 仅参照其文档描述的 provider / wire-api 二分、`ModelSpec` 可序列化字段、跨 provider 交接丢弃 thinking 块三条约定；`app/model_ports.py` 为独立实现，未复制源码 |

## Vendored 前端库

| 文件 | 组件 | 许可声明 |
|---|---|---|
| `app/static/vendor/vis-network.min.js` | vis-network（visjs 社区版） | 文件头声明 MIT 类许可，含 Almende B.V. 与 visjs contributors 版权声明；交付前应核对仓库 LICENSE 原文 |
| `app/static/vendor/echarts.min.js` | Apache ECharts | Apache License 2.0（文件头含 ASF 许可声明） |

## Python 运行时依赖（uv.lock 锁定版本）

| 组件 | 版本 | 许可声明 |
|---|---|---|
| aiosqlite | 0.22.1 | 未声明 |
| annotated-doc | 0.0.5 | MIT |
| annotated-types | 0.8.0 | MIT |
| anyio | 4.15.0 | MIT |
| arq | 0.28.0 | MIT |
| async-timeout | ? | 本机未安装（元数据缺失） |
| certifi | 2026.7.22 | MPL-2.0 |
| cffi | 2.1.1 | MIT-0 |
| charset-normalizer | 3.5.1 | MIT |
| click | 8.5.0 | BSD-3-Clause |
| cn2an | 0.5.24 | MIT License |
| cryptography | 50.0.1 | Apache-2.0 OR BSD-3-Clause |
| distro | 1.9.0 | Apache License, Version 2.0 |
| fastapi | 0.141.1 | MIT |
| h11 | 0.16.0 | MIT |
| hiredis | 3.4.1 | MIT |
| httpcore | 1.0.9 | BSD-3-Clause |
| httpcore2 | 2.12.0 | BSD-3-Clause |
| httpx | 0.28.1 | BSD-3-Clause |
| httpx2 | 2.12.0 | BSD-3-Clause |
| httpx2-jsfetch | ? | 本机未安装（元数据缺失） |
| idna | 3.19 | BSD-3-Clause |
| jieba | 0.42.1 | MIT |
| jsonpatch | 1.33 | Modified BSD License |
| jsonpointer | 3.1.1 | Modified BSD License |
| langchain-core | 1.6.1 | MIT |
| langchain-protocol | 0.0.19 | MIT |
| langgraph | 1.2.11 | MIT |
| langgraph-checkpoint | 4.2.0 | MIT |
| langgraph-checkpoint-sqlite | 3.1.1 | MIT |
| langgraph-prebuilt | 1.1.0 | MIT |
| langgraph-sdk | 0.4.4 | MIT |
| langsmith | 0.12.1 | MIT |
| lxml | 6.1.3 | BSD-3-Clause |
| orjson | 3.12.0 | MPL-2.0 AND (Apache-2.0 OR MIT) |
| ormsgpack | 1.12.2 | Apache-2.0 OR MIT |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause |
| proces | 0.1.7 | MIT License |
| pycparser | 3.0 | BSD-3-Clause |
| pydantic | 2.13.5 | MIT |
| pydantic_core | 2.46.5 | MIT |
| PyJWT | 2.13.0 | MIT |
| python-docx | 1.2.0 | MIT |
| python-multipart | 0.0.32 | Apache-2.0 |
| PyYAML | 6.0.3 | MIT |
| redis | 5.3.1 | MIT |
| requests | 2.34.2 | Apache-2.0 |
| requests-toolbelt | 1.0.0 | Apache 2.0 |
| sniffio | 1.3.1 | MIT OR Apache-2.0 |
| sqlite-vec | 0.1.9 | MIT License, Apache License, Version 2.0 |
| starlette | 1.6.0 | BSD-3-Clause |
| tenacity | 9.1.4 | Apache 2.0 |
| truststore | 0.10.4 | MIT |
| typing_extensions | 4.16.0 | PSF-2.0 |
| typing-inspection | 0.4.4 | MIT |
| urllib3 | 2.7.0 | MIT |
| uuid_utils | 0.17.0 | BSD-3-Clause |
| uvicorn | 0.52.4 | BSD-3-Clause |
| websockets | 16.1.1 | BSD-3-Clause |
| xxhash | 4.0.1 | BSD-2-Clause |
| zstandard | 0.25.0 | BSD-3-Clause |
