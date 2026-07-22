# Bug Fix Log

每条记录格式：
- **错误**：现象或报错信息
- **原因**：根本原因
- **解决方案**：具体修改
- **结果**：修复后验证情况

---
## 2026-07-22

### FEATURE · 模糊邮件 LLM 裁决与本地评估

邮件同步改为“本地规则优先、模糊样本选择性调用 LLM”。订阅和明确无关邮件不调用模型；状态不明确、关键信息缺失或规则信号冲突时使用默认 Provider/Model 做结构化裁决，失败自动回退规则结果。SQLite 仅保存正文 Hash、规则/LLM/最终结构化结果、模型、延迟、token 和人工反馈，不保存邮件正文。投递追踪页展示调用、失败、待确认、分歧、延迟、token 及人工准确率。


### CHANGE · 评分结果排除搬迁与签证分析

匹配提示词和结果后处理不再把搬迁、签证、雇主担保或工作许可作为风险、弱项、评分结论或总解释，也不因此增加风险扣分。地点匹配分仍保留，但只表达地点是否匹配，不推断候选人需要搬迁。


### BUG-004 · Gemini 拒绝 CV 解析工具 Schema

**错误**
提交 CV 后提示 `additionalProperties is not supported in the Gemini API`。

**原因**
岗位相关年限最初使用 `dict[str, float]` 表示，Pydantic 因此在工具 JSON Schema 中生成了 Gemini 不支持的 `additionalProperties`。

**解决方案与结果**
将岗位相关年限改为 `{role, years}` 对象数组，并新增 Schema 兼容性回归测试，确保 CV 工具 Schema 不包含 `additionalProperties`。Ruff 与完整 pytest（141 tests）均通过。


### FEATURE · 搜索任务暂停、继续与终止控制

搜索进度区新增互斥显示的暂停/继续按钮和终止按钮。后台搜索采用协作式检查点，在抓取批次、过滤和写入边界安全暂停或终止；已产生的职位结果继续保留。

### BUG-003 · 不相关行业年限被用于目标职位评估

**错误**
CV 的所有行业总工作年限可能被当作目标职位的相关经验，导致职位预过滤、评分和总结产生错误结论。

**原因**
`CVProfile` 只有全局 `years_of_experience` 字段，所有针对具体职位的年限比较都直接复用了该值，无法区分相关与不相关经历。

**解决方案与结果**
新增按目标职位记录的 `role_experience_years`，并让预过滤、批量评估和深度匹配仅使用与当前职位匹配的相关年限。缺少可靠相关年限时，不再执行年限硬拒绝或生成确定的年限差距结论。新增跨行业经历和旧资料缺失字段的回归测试；Ruff、编译检查与完整 pytest（135 tests）均通过。


## 2026-07-17

### BUG-002 · 投递追踪按钮错误打开 API 配置页

**错误**
点击顶部“投递追踪”后，API 配置页覆盖显示。

**原因**
投递追踪按钮被插入到 API 配置按钮的 SVG 内，非法按钮嵌套导致点击事件同时触发父级配置按钮。 同时，投递追踪 JavaScript 被追加在 `</html>` 之后，导致修正按钮后点击仍无响应。

**解决方案与结果**
将两个导航按钮改为同级 DOM 节点，并增加静态结构回归测试。Ruff 与完整 pytest 均通过。


## 2026-04-17

### BUG-001 · cache.py SyntaxError：非法全角逗号字符

**错误**
```
File "jobfinder/cache.py", line 98
    "ALTER TABLE job_cache ADD COLUMN company_profile TEXT",  -- 已弃用，保留迁移兼容旧库
                                                                    ^
SyntaxError: invalid character '，' (U1+FF0C)
```

**原因**
迁移 SQL 字符串末尾误用了 SQL 注释语法 `--`，且注释中包含全角逗号 `，`（U+FF0C）。Python 字符串内不能出现 SQL 注释，解析器将 `--` 之后的内容作为 Python 代码处理，遇到全角字符报 SyntaxError。

**解决方案**
将行尾 SQL 注释改为 Python 行尾注释：
```python
# Before
"ALTER TABLE job_cache ADD COLUMN company_profile TEXT",  -- 已弃用，保留迁移兼容旧库
# After
"ALTER TABLE job_cache ADD COLUMN company_profile TEXT",  # deprecated, kept for old DB compat
```

**结果**
`uv run jobfinder serve --mock` 正常启动，SyntaxError 消失。

---

### BUG-002 · cache.py `_row_to_job` Pydantic ValidationError：sources 列类型混乱

**错误**
```
pydantic_core.ValidationError: 2 validation errors for JobResult
sources.2
  Input should be a valid string [type=string_type, input_value={'source': 'indeed.ie', ...}]
sources.3
  Input should be a valid string [type=string_type, input_value={'source': 'linkedin.com', ...}]
```

**原因**
早期版本的 `_insert_job` / `_merge_job` 在写入 `sources` 列时，将完整的 `raw_sources` dict 对象混入了原本应为字符串列表的 `sources` 字段。导致 SQLite 中 `sources` 列存储了形如 `["indeed.ie", {"source": "linkedin.com", ...}]` 的混合 JSON，反序列化时 Pydantic 校验失败。

**解决方案**
在 `_row_to_job` 读取时加兼容清洗，将 dict 条目提取出 `source` 字段：
```python
# Before
sources=json.loads(row["sources"] or "[]"),
# After
sources=[s if isinstance(s, str) else s.get("source", "") for s in json.loads(row["sources"] or "[]")],
```

**结果**
`scripts/verify_dedup.py` 正常读取全部 42 条缓存记录，ValidationError 消失。

---

## 2026-04-27

### BUG-003 · dynamic title seniority gate 触发后整轮搜索被中断

**错误**
```
2026-04-27 00:28:24 [WARNING] jobradar.agent: Scrape error, skipping: 'skip_seniority'
2026-04-27 00:28:24 [INFO] jobradar.agent: Search complete, 0 jobs collected
```

**原因**
`agent.py` 新增 title seniority gate 后，在预过滤阶段会对每个来源的 `source_stats` 执行 `ss["skip_seniority"] += 1`。
但 `_SS_KEYS` 里没有初始化 `skip_seniority` 这个键，第一条被该 gate 拦截的职位就会触发 `KeyError`，外层异常处理把整轮搜索当成 scrape error 跳过。

**解决方案**
将 `skip_seniority` 补进 `_SS_KEYS`，保证每个来源的漏斗统计字典从一开始就包含这个计数字段。

```python
# Before
_SS_KEYS = ("in", "dup", "cache_hit", "no_desc", "closed", "llm_rejected", "saved")

# After
_SS_KEYS = ("in", "dup", "skip_seniority", "cache_hit", "no_desc", "closed", "llm_rejected", "saved")
```

**结果**
title seniority gate 现在会正常计数，不再把整轮搜索中断为 `0 jobs collected`。

---

## 2026-05-05

### BUG-004 · matching 风险辅助函数漏导入 `re`，整轮搜索被误记为 scrape error

**错误**
```text
2026-05-04 20:42:33 [WARNING] jobradar.agent: Scrape error, skipping: name 're' is not defined
2026-05-04 20:42:33 [INFO] jobradar.agent: Search complete, 0 jobs collected
```

**原因**
`matching.py` 新增了用于识别跨城市搬迁和办公室到岗要求的正则辅助函数，但文件顶部漏写了 `import re`。
运行到 matching 阶段时抛出 `NameError`，外层统一异常处理把它记录成了 `Scrape error, skipping`，因此日志表面看像抓取失败，实际是匹配阶段的 Python 异常。

**解决方案**
在 `jobradar/matching.py` 顶部补上：

```python
import re
```

**结果**
`python -m compileall jobradar/matching.py` 通过，matching 风险检查不再因为缺少 `re` 导致整轮搜索返回 `0 jobs collected`。

---

## 2026-07-18

### BUG-005 · 投递追踪行点击没有打开对应 Gmail 邮件

**原因**

列表行的点击处理只调用了页面内的时间线展开函数。虽然事件记录保存了 Gmail message ID，但前后端都没有实现 Gmail 跳转链接。

**解决方案**

新增应用邮件重定向端点：使用已保存的 message ID 通过 Gmail API 查询真实 thread ID 和当前授权邮箱，再跳转到对应 Gmail 会话。列表行点击改为在新标签页打开该端点，确认和舍弃按钮继续阻止行点击事件。

**结果**

现有及后续同步的投递记录均可直接打开对应 Gmail 邮件，无需重新同步。

---
## 2026-07-21

### BUG-006 · Gmail 逆序返回导致旧投递状态覆盖最新状态

**原因**

Gmail 列表通常按新到旧返回，原同步流程按返回顺序直接落库；同一投递的旧邮件会在最新邮件之后执行，并无条件覆盖 `current_status` 和 `last_event_at`。

**解决方案**

同步阶段先解析邮件时间并按升序处理，持久化阶段再比较 `event_at` 与 `last_event_at`，只有不早于当前事件的邮件才能更新当前状态。旧邮件仍进入时间线，但不能让状态倒退。

**结果**

新增回归测试覆盖“最新拒绝先返回、旧投递后返回”的 Gmail 顺序，最终状态稳定保持最新事件。

---
<!-- 新 bug 请在此行上方添加，格式同上 -->
