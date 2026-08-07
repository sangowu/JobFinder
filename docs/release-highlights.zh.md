# 版本亮点

> **中文** · [English](release-highlights.md) · [Español](release-highlights.es.md)

面向使用者的版本优化摘要。这里的每个数字都必须能在 [CHANGELOG.md](../CHANGELOG.md)
的 `### Validation` 中找到出处；本文档不产生任何自己的数字。

从 0.5.0 起，每个版本对比上一个发布版本。

## 0.4.0

基线：性能数字来自 `bench/serial-baseline`，成本数字来自
`bench/pre-merged-eval` → `bench/merged-eval`。本版本跨越十个 PR，
因此不与 `v0.3.0` 对比。

**搜索快 2.6 倍**
118.5 秒 → 45.6 秒（-61.5%）

**首条结果快 3.7 倍**
47.5 秒 → 12.8 秒（-73.1%）

**吞吐提升 145%**
每分钟 14.7 → 36.0 个职位

**LLM 调用减半**
每轮 41 → 20 次（-51.2%）

**每职位输入 token 减少 47.5%**
5,251 → 2,759
