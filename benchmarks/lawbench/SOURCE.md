# LawBench 测试题库来源

- 上游项目：https://github.com/open-compass/LawBench
- 固定版本：`e30981bb3ff54c41571f222e0b23e92d27375388`
- 本地内容：`data/zero_shot`，20 类任务，每类 500 题，共 10,000 题
- 论文：Fei et al., *LawBench: Benchmarking Legal Knowledge of Large Language Models*（EMNLP 2024）

仓库代码采用 Apache-2.0。LawBench 是由多个公开法律数据集创建或转换而成的混合数据集，使用和再分发时还应遵循每项任务原始数据源的许可。项目内保留了上游 `LICENSE`，题目未作修改。

JEC-QA 官方说明共有 26,365 道中国国家司法考试选择题，但截至 2026-09-02 官方数据下载链接不可用，因此没有使用来源不明的镜像。本题库中的任务 `1-2` 和 `3-6` 是 LawBench 官方整理的 JEC-QA 子集，共 1,000 题，可直接进行确定性自动评分。
