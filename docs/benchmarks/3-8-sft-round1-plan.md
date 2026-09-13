# 3-8 LoRA 首轮（round-1）：训练前冻结清单

日期：2026-09-13。状态：**冻结**。本轮只检验一个假设——「3K 条真实咨询 SFT 能否带来 3-8 配对收益且不伤其他任务」；
数据量、提示、训练参数三者只动数据量，其余不变；首轮通过前不调参、不换提示、不扩 8K。
数据审计见 `3-8-sft-data-audit.md`；2-2 技术教训见记忆与 `output/lora-2-2-v1/`。

## 审计边界的落实（对上轮审计的三处收窄）

1. **语义分组去重（替代纯文本去重）**：清洗池全部 63,738 条 input 经 `Qwen3-Embedding-4B-4bit-DWQ`
   嵌入，余弦 ≥0.85 并查集聚簇；**簇整体分配**到同一 split，改写版本不得跨训练/验证/留出集。
   阈值与簇分布写入 manifest（`output/sft-38-v1/manifest.json`）。已知局限：0.85 为首个冻结值，
   未经人工校准曲线；report 如实记录簇分布与跨库同案改写例（51278/51631 类）。
2. **留出集定位**：从 Pair-QA 池留出的 200 条为**同源留出测试集**——度量同分布泛化；不排除基座
   预训练见过该公开数据，也不替代独立来源的外部测试集。上述两种限定写入验收报告模板。
3. **引用质量**：`has_citation` 仅作为池内分层记录字段（manifest 记录各 split 引用率），
   **不作为样本筛选标准**；「有法条引用」不等于「引用正确」。人工核查维度 = 引用适用性、
   版本正确性、与结论的支持关系（见验收规则）。

## 冻结的数据清单

| 项 | 值 |
|---|---|
| 源数据 | `output/sft-audit-38/DISC-Law-SFT-Pair-QA-released.jsonl`（sha256 入 manifest） |
| 清洗规则 | 考试题式正则剔除、答案截断剔除（不以句末标点结尾）、泄漏边缘 5 条剔除、input<10 字剔除 |
| 清洗池 | 63,738 条（剔除：考试式 5,175 / 截断 9,717 / 泄漏 5 / 过短 1,057） |
| 划分 | train 3,000 ／ valid 200（训练侧验证，选 checkpoint 唯一依据）／ holdout 200（同源留出测试） |
| 分组 | 余弦 0.85 并查集簇，seed=`38-sft-v1`，簇整体分配 |
| 渲染 | text 格式预渲染：生产提示 A（`TASK_GUIDANCE["3-8"]` 字面冻结，sha256 入 manifest）+ 评测同款闭合 think；completion = 答案 + `<|im_end|>`；评测答案与参考答案均不进入训练 |
| 产物 | `output/sft-38-v1/dataset/{train,valid}.jsonl`、`output/sft-38-v1/holdout.jsonl`、`manifest.json`（含各 split sha256） |

## 冻结的训练配置（一个配置，不调参）

| 项 | 值 |
|---|---|
| 基座 | `models/Qwythos-9B-v2-4bit-mlx`（训练、服务、评测同基座；与 2-2 线一致） |
| 方法 | `mlx_lm.lora`（默认层、~0.12% 可训练参数），full-finetune=false |
| 超参 | lr 1e-4、batch 4、iters 750（≈1 epoch）、grad-checkpoint、save-every 50 |
| 序列 | max-seq 1536（3-8 prompt+答案实测长度；2-2 的 320 截断不可沿用） |
| 验证 | `--data` 目录含 valid.jsonl，steps-per-eval 50，**仅以 valid loss 选 checkpoint** |
| 服务 | `output/lora-2-2-v1/serve_adapter.py`（mlx_lm server 的 adapter-path bug workaround），port 8100 |
| 内存纪律 | 训练期间 oMLX unload 全部已载模型（保留进程）；训练 ~9.5–11GB wired，完成后再恢复服务 |

## 冻结的对照臂与验收规则

**对照臂 A**：同一 4bit 基座、无 adapter、同题同参数（temperature=0、max_tokens=1600、timeout=240）。
唯一变量 = adapter 挂载。所有 3-8 分数统一做 normalize 后重算（口径与 neworg-v1 一致）。

| # | 验收项 | 通过标准（首轮） |
|---|---|---|
| 1 | 背诵测试 | 训练集抽 5 题，adapter on 能复述训练答案要点、off 不能——证明 adapter 真实生效 |
| 2 | 3-8 配对收益 | dev20 两臂配对差值 >0（如实报告 95%CI；显著性留给 8K 轮） |
| 3 | 其他任务退化 | 2-2、2-7、1-2 各 10 题配对，任一任务降幅 >2pp 即失败（2-2 线先验坍塌教训） |
| 4 | 人工法律质量 | trainval+holdout 抽 10 题：引用适用性、版本、与结论支持关系；无「结论被引用反驳」级硬伤 |
| 5 | 路由隔离 | 生产形态 = 双端口（3-8 专用端口挂 adapter、其余端口裸基座）；实测两端口对同一非 3-8 请求行为与各自基座一致 |

首轮通过的定义：1–5 全部满足 → 允许 8K 扩轮（仍只动数据量）。任一失败 → 归档负结果与归因，不调参续跑。
