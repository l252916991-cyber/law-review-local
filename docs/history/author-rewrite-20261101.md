# 2026-11-01 提交作者改写与哈希对照

- 日期：2026-11-01（Asia/Shanghai）。
- 范围：本地 Git 基线（2026-10-23）之后、合并进 `main` 的全部 176 个提交。
- 改动：作者与提交者 `Codex <codex@local.invalid>` 统一改为 `刘勇 <311464897+l252916991-cyber@users.noreply.github.com>`；文件内容、提交时间与提交说明不变。
- 工具：`git filter-repo --mailmap`，映射只有一行，不触及其他身份。
- 同日在 Claude 协助下完成的整理提交（合并、lint、拆分、文档等）作者同样记为维护者，每条提交说明末尾保留 `Co-Authored-By: Claude` 署名。

## 为什么改

这些提交由维护者主导、在 AI 编码助手（Codex）辅助下完成。原仓库级作者是初始化时的占位身份，不关联任何 GitHub 账户。改写后作者与维护者一致；开发方式在 [README](../../README.md) 中如实说明，不因作者字段变化而改变。

## 哈希变化的处理

改写会改变全部提交哈希。完整的新旧对照见 [author-rewrite-commit-map-20261101.tsv](author-rewrite-commit-map-20261101.tsv)（`old_commit` → `new_commit`）。

- 文档、`.gitleaksignore`、`app/evaluation/model_comparison.py` 的基线来源提交、`scripts/migration_demo.py` 的历史基线和对应测试中的旧哈希，已按对照表替换为新哈希（截取原长度）。
- **封存数据不改**：`benchmarks/answer_eval/holdout-v2-protocol.json` 与 `holdout-v2-sealed-manifest.json` 的 `candidate_commit` 仍为旧值 `ff2856afb9b9decc07d6de7642db2f73d95deb41`，否则会破坏封存摘要。按对照表，它对应新提交 `b70d7835f03a601be45c2a64277a362d133e2a8a`。
- 改写前的完整历史（含全部旧分支）以 Git bundle 形式私下保存，不随源码发布。
