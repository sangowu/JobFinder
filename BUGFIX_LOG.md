# Bug Fix Log

每条记录格式：
- **错误**：现象或报错信息
- **原因**：根本原因
- **解决方案**：具体修改
- **结果**：修复后验证情况

---

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

<!-- 新 bug 请在此行上方添加，格式同上 -->
