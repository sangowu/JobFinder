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

<!-- 新 bug 请在此行上方添加，格式同上 -->
