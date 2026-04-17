# JobFinder — 面试展示指南

## 🎯 项目核心价值

**自动匹配职位的 LLM-as-Judge 系统**：用户上传 CV → 自动搜索、评估、排序职位，展示量化的匹配度。

---

## 📊 演示数据已生成

运行了 `scripts/seed_demo_data.py`，在 `jobfinder_test_cache.db` 中生成了 **17 个真实公司职位**，包含完整评估数据。

### 数据概览
- **17 个职位**：OpenAI、Google、Meta、Anthropic 等顶级公司
- **评分分布**：4-8 分（不同类型的候选人与职位的匹配度）
- **评估维度**：score、strengths、weaknesses、matched_keywords

---

## 🚀 快速启动演示

### 步骤 1：启动 Web UI（测试模式）
```bash
cd D:\Python_Projects\JobFinder
uv run jobfinder serve --mock
```

浏览器自动打开 http://127.0.0.1:8765

### 步骤 2：查看演示数据
- **左侧列表**：17 个职位卡片（按 score 降序）
- **点击卡片**：展示完整的评估详情
  - 🟢 Strengths（你的优势）
  - 🔴 Weaknesses（你的劣势）
  - 📌 Matched Keywords（技能重叠）
  - 📊 Score（0-10 匹配分）

---

## 💡 面试时要强调的核心能力

### 1. **LLM-as-Judge 评估系统** ⭐⭐⭐
**展示点**：
- 不仅抓取职位，还用 LLM 进行**深度评估**（不是简单的关键词匹配）
- 输出**可解释的评分**：为什么这个职位是 8 分而不是 5 分？
  - CV 的哪些技能与 JD 重叠？
  - CV 相对该职位的具体优势？
  - CV 需要学习的领域？

**面试常见问题**：
- "你的匹配算法如何避免假阳性？"
  - 答：多层过滤→年资过滤→LLM 标题评分→完整 JD fetch→LLM 深度评估
  
- "怎么确保评估的质量？"
  - 答：使用 Claude / Gemini 进行结构化输出，输出包含 strengths/weaknesses，可人工验证准确性

---

### 2. **多源抓取 + 缓存策略** ⭐⭐
**展示点**：
- Indeed（JobSpy 库，无需浏览器 → 避免 ToS 问题）
- Adzuna（职位发现 + 标题归一化）
- 缓存层（SQLite，7 天 TTL，支持增量更新）
- 速率限制（2s/role，防限流）

**面试亮点**：
- "为什么用 JobSpy 而不是 Playwright/Selenium？"
  - 答：法律灰色地带（模拟点击）→ 使用官方 API 更安全
  - JobSpy 抽象度高，防被限流

---

### 3. **CV 智能解析** ⭐
**展示点**：
- 支持 .docx / .md 格式 → LLM 结构化提取
- 输出：CVProfile（name, summary, skills, seniority, years_of_experience）
- **年资自动检测**：intern / new_grad / junior / mid / senior / lead
- 缓存（SHA-256 dedup），避免重复解析

**面试价值**：
- 将非结构化简历 → 结构化数据（供搜索/评估使用）
- 年资自动推断 → 支持自动过滤（避免 new_grad 看 Staff 职位）

---

### 4. **公司背景查询（可选）** ⭐
**展示点**：
- 职位高分 (score ≥ 3) 时，自动查询公司信息
- DDG 搜索 + Jina 网页抓取 → LLM 分析
- 输出：CompanyProfile（size, industry, hq_location, overview）
- 30 天缓存（降低 API 成本）

**面试应用**：
- "我不仅告诉你职位好，还告诉你公司怎么样"
- 展示做了额外的上下文增强

---

### 5. **Web UI + SSE 实时流** ⭐⭐
**展示点**：
- FastAPI 后端 + HTML 前端（三栏布局）
- 搜索期间 SSE 实时推送职位卡片
- 配置页：支持多 LLM 提供商（17 个）
- 日志面板：实时查看搜索进度

**面试亮点**：
- "不只是 CLI，还有生产级的 Web UI"
- 支持 --mock 模式（测试数据与正式缓存隔离）

---

### 6. **工程质量** ⭐⭐
**展示点**：
- Pydantic 数据校验（CVProfile、JobResult、JobAssessment、SearchSession）
- SQLite 三张表（职位、会话、失败 URL）+ 迁移脚本
- 日志系统（Rich 终端渲染 + 文件记录）
- 单元测试（pytest）

**面试自信心**：
- "这不是一个脚本，这是一个可维护的系统"

---

## 📈 演示流程（5 分钟）

### 时间线
```
0:00-0:30  产品演示（Web UI）
  → 展示演示数据中的职位列表
  → 点击一个职位（如 OpenAI Senior AI/ML 8 分）
  → 展示评估详情：strengths/weaknesses/matched_keywords

0:30-1:30  技术亮点解说
  → CV 上传 & 解析（如果有真实 CV）
  → 解释 LLM 评分逻辑（为什么是 8 而不是 6？）
  → 展示缓存结构（SQLite 三张表）

1:30-2:30  代码架构走查
  → agent.py 的评估流程
  → scraper_jobspy.py 的多源抓取
  → llm_backend.py 的多 provider 支持

2:30-5:00  Q&A + 讨论
  → 可扩展方向（红旗检测、薪资预测等）
  → 与面试官的交互
```

---

## 🎬 演示脚本示例

### 开场（30 秒）
> "这是一个 CLI + Web 工具，根据用户 CV **自动搜索并评估职位**。核心创新是用 LLM 作为评判官，不仅检查关键词匹配，还输出**可解释的评分**：为什么这个职位适合你，你的优势在哪，需要学习什么。"

### 演示 UI（1 分钟）
> "我这里已经预加载了 17 个顶级公司的职位。你可以看到左边的列表按匹配分排序。如果点击这个 OpenAI 的职位（8 分），右边会显示完整的评估——它告诉你为什么匹配分这么高：PyTorch、Transformers 这些技能你有，但 RLHF 对齐这块是你的短板。"

### 技术核心（2 分钟）
> "后端用了几个关键的设计：
> 1. **多层过滤**：先过年资（不浪费 API token），再用 LLM 批量评标题，最后才 fetch 完整 JD
> 2. **缓存策略**：相同的职位 7 天内直接复用，相同 CV 永久缓存（加快迭代）
> 3. **多 provider 支持**：Claude、Gemini、Qwen、本地 Ollama，用户可选
> 4. **Web UI**：FastAPI + SSE，搜索时实时推送职位卡片，可以看到进度"

---

## 📦 关键文件导览

| 文件 | 用途 | 展示点 |
|------|------|--------|
| `agent.py` | 搜索主流程 | 多层过滤的算法逻辑 |
| `scraper_jobspy.py` | 职位抓取 + LLM 评分 | `_filter_cards_by_llm` 批量评分 |
| `llm_backend.py` | 多 provider LLM 调用 | 17 个 provider 的支持 |
| `server.py` | FastAPI 后端 + SSE | Web UI 的数据接口 |
| `templates/index.html` | 前端 UI | 三栏布局、卡片交互 |
| `cache.py` | SQLite 缓存层 | 数据持久化与增量更新 |
| `schemas.py` | Pydantic 数据模型 | CVProfile、JobAssessment、CompanyProfile |

---

## ❓ 常见面试问题 & 回答

### Q: "为什么需要 LLM 评估？简单的关键词匹配不行吗？"
**A**: 关键词匹配只能告诉你"这个职位提到了 Python"，但 LLM 可以理解**上下文**：
- "这是一个数据分析师的 Python"还是"深度学习工程师的 Python"？
- CV 中的 3 年 Python 经验 vs. 职位要求的 5 年，是否足够？
- 职位在 San Francisco 但要求会中文，这个约束你满足吗？

关键词匹配的假阳性太高；LLM 可以做更细粒度的理解。

---

### Q: "这个系统的瓶颈在哪里？"
**A**: 主要有几个：
1. **抓取速度**：Indeed 的速率限制（2s/role），Adzuna 的 API 限流（1.2s/req）
   - 可优化：并发 + 更聪明的重试机制
2. **LLM 成本**：每个职位都要调用 LLM 评估
   - 可优化：先用快速模型粗过滤，高分职位才用贵的模型精评
3. **缓存命中率**：职位去重依赖 company + title 的规范化
   - 可优化：加入发布日期、URL 指纹作为额外维度

---

### Q: "怎么确保评估的准确性？"
**A**: 
1. 结构化输出（Pydantic）保证格式一致
2. Prompt 工程：在 `_assess_jd` 中给 LLM 明确的指示（考虑哪些维度）
3. 多模型对比：可以用不同的 LLM（Claude vs Gemini）对同一职位评估，比较差异
4. 用户反馈环（Web UI 可扩展投票功能）：用户标记"这个推荐对我有用吗"，迭代 prompt

---

### Q: "下一步的扩展方向是什么？"
**A**: 
- 🚩 **红旗检测**：职位描述中是否有超长工作时间、高离职率信号
- 💰 **薪资预测**：基于职位、公司、位置，预测薪资范围
- 🎓 **成长评分**：这个职位有多少学习空间？能否突破我的天花板？
- 🌐 **国际化**：支持更多国家的职位源（目前主要是英美）
- 📊 **用户反馈闭环**：收集用户对推荐的反馈，微调 LLM prompt
- 🤝 **面试准备**：基于职位描述生成面试准备指南

---

## 🎓 技术栈总结

**后端**：Python、FastAPI、SQLite、Pydantic  
**抓取**：JobSpy（Indeed）、Adzuna API  
**LLM**：Claude / Gemini / Qwen / 本地 Ollama（17 个 provider）  
**前端**：HTML5、Vanilla JS、Tailwind CSS  
**部署**：可直接 `uv run` 启动，支持容器化

---

## 🎯 最后的话

**这个项目展示的不仅是"我能做搜索爬虫"，而是：**
- ✅ 系统设计能力（多层过滤、缓存策略）
- ✅ AI 集成能力（多 provider、structured output、prompt engineering）
- ✅ 全栈开发能力（后端 API + 前端 UI + 数据库）
- ✅ 工程质量（数据校验、日志、测试框架）
- ✅ 问题解决能力（找准用户痛点、创意解决方案）

**关键是讲清楚：为什么选择这样的架构，每个选择背后的权衡。**

---

演示数据已就位，祝面试顺利！🚀
