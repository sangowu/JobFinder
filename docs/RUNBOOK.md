# JobRadar 运维手册（Runbook）

面向值班/排障。每条告警一节，锚点与 `monitoring/alerts.yml` 中的 `runbook:` 注解一一对应。

---

## 环境速查

| 组件 | 地址 | 启动方式 |
|---|---|---|
| JobRadar（宿主机进程，非容器） | http://127.0.0.1:8765 | `uv run jobradar serve --no-browser` |
| Prometheus | http://localhost:9090 | `cd monitoring && docker compose up -d` |
| Grafana | http://localhost:3000（`admin` / `admin`，仅本地） | 同上 |

| 资源 | 位置 |
|---|---|
| 指标端点 | `GET /metrics` |
| 日志文件 | `logs/jobradar.log`（`LOG_FILE` 可覆盖，设为空字符串则禁用文件日志） |
| 日志 API | `GET /api/logs?lines=200&level=ERROR` |
| 运行统计 | `GET /api/stats` |
| 缓存数据库 | `data/jobradar_cache.db`（`CACHE_DB_PATH` 可覆盖；`--mock` 走 `data/jobradar_test_cache.db`） |
| 告警规则 | `monitoring/alerts.yml` |

### 常用查询入口

- 抓取目标状态：http://localhost:9090/targets
- 规则加载情况：http://localhost:9090/rules
- 当前告警：http://localhost:9090/alerts

---

## 预期行为：告警延迟

**这些延迟是设计使然，不是卡死。** 实测值：

| 事件 | 延迟 | 构成 |
|---|---|---|
| 服务中断 → 告警 firing | **约 92 秒** | scrape_interval(15s) + `for: 1m` + 组评估周期对齐 |
| 服务恢复 → 告警消退 | **约 75 秒** | 告警只在下一次组评估（间隔 60s）时才清除 |

告警红了一分多钟不消失属正常。想更快就调小 `evaluation_interval` 和各规则的 `for`，代价是误报率上升。

### 已知限制

- **没有独立的健康探针端点**。`/metrics` 同时承担了存活探测职责——它响应即代表进程存活并能处理请求。若将来部署到 ECS/K8s，应单独加 `/health`，避免健康检查与指标抓取耦合。
- **告警没有通知出口**。当前只有 Prometheus 规则，未部署 Alertmanager，告警仅在 Prometheus 页面上变红，不会发邮件/Slack。
- **指标为进程内计数**，JobRadar 重启后 counter 归零。这是 Prometheus counter 的正常语义，`rate()` 能识别 reset 并正确计算；不要直接用 counter 原始值画图。

---

## JobRadarDown

**告警条件** — `up{job="jobradar"} == 0`，持续 1 分钟。

**现象** — Prometheus 连续 1 分钟抓不到 `/metrics`。

**影响** — 服务不可用，或仅指标端点不可达（因为没有独立健康探针，两者无法区分）。所有 Web UI 操作和定时邮件同步都会停止。

**先看哪里**

1. http://localhost:9090/targets → 看 `jobradar` 那行的 `lastError`
2. 进程是否存活：`Get-Process | Where-Object { $_.ProcessName -like "*python*" }`
3. 端口是否在监听：`Test-NetConnection 127.0.0.1 -Port 8765`
4. 宿主机直连是否通：`curl.exe -s http://127.0.0.1:8765/metrics | Select-Object -First 3`

**常见原因与处置**

| 原因 | 判断依据 | 处置 |
|---|---|---|
| 进程未启动 / 已崩溃 | `lastError` 含 `connection refused`，端口无监听 | 重启：`uv run jobradar serve --no-browser`；崩溃原因查 `logs/jobradar.log` 末尾 |
| 端口被占用 | 启动时报 `address already in use` | 换端口 `--port`，或结束占用进程（`netstat -ano \| findstr 8765` 拿 PID） |
| 容器无法访问宿主机 | 宿主机 `curl` 通，但 target 仍 down | 容器内验证：`docker exec -it jobradar-prometheus wget -qO- http://host.docker.internal:8765/metrics`；检查 `docker-compose.yml` 的 `extra_hosts: host-gateway` |
| Prometheus 容器自身挂了 | 9090 页面打不开 | `docker compose ps`，`docker compose up -d` |
| 启动时 import 失败 | 日志有 traceback，端口从未监听 | 按 traceback 修；先跑 `uv run ruff check .` 和 `uv run python -m pytest tests/ -q` |

**验证恢复** — target 变 UP 后，等约 75 秒告警自动消退。若 target 已 UP 但告警持续 firing 超过 3 分钟，检查 Prometheus 是否真在评估规则（`/rules` 页面的 `lastEvaluation` 时间戳是否在推进）。

**何时升级** — 重启后 5 分钟内仍 firing，或反复崩溃（15 分钟内 ≥3 次）。

---

## HighErrorRate

**告警条件** — 5xx 请求占比 > 5%，持续 5 分钟。

**现象** — 用户操作报错。可能只影响个别接口。

**影响** — 取决于哪个接口在报错。搜索类接口失败影响核心功能；配置类接口失败只影响设置页面。

**先看哪里**

1. **定位是哪个接口**（这一步最关键）——在 Prometheus 执行：

   ```promql
   sum by (handler) (rate(http_requests_total{status=~"5.."}[5m]))
   ```

2. 看错误日志：`GET /api/logs?lines=200&level=ERROR`，或直接 `logs/jobradar.log`
3. 该接口是否依赖外部服务（LLM provider / Adzuna / Gmail / JobSpy）

**常见原因与处置**

| 原因 | 判断依据 | 处置 |
|---|---|---|
| LLM provider 密钥失效或额度耗尽 | 日志有 401/403/429，`handler` 指向搜索或评估相关接口 | 配置页测试连通性；换 provider 或补额度 |
| 外部抓取源限流 | 日志有 JobSpy WARNING，Indeed 返回 0 条 | Indeed 限流很常见，等几小时重试（见「已知外部依赖问题」） |
| Adzuna 429 | 日志有 429，title 发现失败 | 调大 `title_discovery.py` 的 `_MIN_INTERVAL`（当前 1.2s） |
| 数据库文件损坏 / 被占用 | 日志有 `sqlite3.OperationalError` | 确认无第二个进程在写同一个 db；必要时 `uv run jobradar cache clear` |
| 代码缺陷 | 日志有明确 traceback，且可稳定复现 | 按 traceback 修；补回归测试 |

**何时升级** — 错误率 > 20%，或错误集中在核心搜索链路且 30 分钟内无法定位。

---

## HighLatencyP95

**告警条件** — 全局 p95 延迟 > 2 秒，持续 10 分钟。

```promql
histogram_quantile(0.95, sum by (le) (rate(http_request_duration_highr_seconds_bucket[5m]))) > 2
```

**现象** — 页面操作明显变慢。

**影响** — 可用但体验差。若同时伴随 `HighErrorRate`，优先按后者处理。

**先看哪里**

1. **哪个接口慢**——按 handler 拆分（注意：这个直方图桶较粗，见下方说明）：

   ```promql
   histogram_quantile(0.95, sum by (le, handler) (rate(http_request_duration_seconds_bucket[5m])))
   ```

2. 是否有搜索任务在跑：`GET /api/search/status`
3. LLM 调用耗时：`GET /api/stats` 看 token 消耗与耗时趋势

> **两个延迟直方图，别用错**
>
> | 指标 | handler 标签 | 桶 | 用途 |
> |---|---|---|---|
> | `http_request_duration_highr_seconds` | 无 | 21 个，`0.01` → `60.0` | **全局 p95 告警**，分辨率足够 |
> | `http_request_duration_seconds` | 有 | 仅 `0.1 / 0.5 / 1.0 / +Inf` | 按路由粗略排查，**1 秒以上无分辨率** |
>
> 带 handler 的那个最大有限桶只有 1 秒，超过 1 秒 `histogram_quantile` 会返回 `+Inf`。所以告警阈值必须走 highr 那个。这是 instrumentator 的刻意设计——细桶 × 多路由会导致指标基数爆炸。

**常见原因与处置**

| 原因 | 判断依据 | 处置 |
|---|---|---|
| 搜索任务占用（正常） | `/api/search/status` 显示进行中，慢的是搜索相关接口 | 属预期，搜索含多次 LLM 调用与外部抓取；如频繁触发可调大 `for` 或阈值 |
| LLM provider 响应慢 | 慢的接口都涉及 LLM，`/api/stats` 显示单次调用耗时上升 | 换更快的模型（配置页），或换 provider |
| 缓存未命中导致重复 LLM 评估 | 日志显示大量 JD 评估，`cv_hash` 变化 | 正常冷启动行为；确认 CV 未被反复重新解析 |
| 数据库慢查询 | 慢的是 `/api/jobs` 等纯读接口 | 检查 `job_cache` 表体积；`uv run jobradar cache clear` 清过期数据 |

**何时升级** — p95 > 10 秒，或伴随请求量骤降（说明请求在堆积）。

---

## EmailSyncStale

**告警条件**

```promql
jobradar_email_sync_last_success_timestamp_seconds > 0
  and
time() - jobradar_email_sync_last_success_timestamp_seconds > 3600
```

持续 5 分钟。

> **`> 0` 守卫的作用**：该指标是 Gauge，初始值为 0。没有这个前置条件时，`time() - 0` ≈ 17 亿秒，服务一启动就会触发并一直响到第一次同步成功——把「从未发生过」误判成「很久没发生」。改这条规则时不要删掉它。

**现象** — Gmail 同步超过 1 小时没有成功过。定时任务默认间隔 15 分钟（`EMAIL_SYNC_INTERVAL_SECONDS`，最小 60s），所以连续失败约 4 次才会触发。

**影响** — 投递状态追踪停止更新（submitted / interview / offer / rejected 不再自动推进）。不影响搜索和评分。

**先看哪里**

1. 按 trigger/status 拆分同步结果：

   ```promql
   sum by (trigger, status, reason) (rate(jobradar_email_sync_runs_total[30m]))
   ```

   `status` 有三个值：`success` / `failed` / `skipped`。**先分清是「失败」还是「被跳过」**——两者处置完全不同。

   `reason` 仅在 `failed` 时有意义，四个取值：`auth`（授权失效，不会自愈）/ `rate_limit`（限流或网络抖动，通常自愈）/ `other`（其余，看日志）/ `none`（成功或跳过）。

2. 日志：`GET /api/logs?lines=100&level=WARNING`，找 `Scheduled email sync failed`
3. 同步耗时是否异常：`jobradar_email_sync_duration_seconds` 的 `_count` 与 `_sum`

**常见原因与处置**

| status | 原因 | 判断依据 | 处置 |
|---|---|---|---|
| `skipped` | 自动同步被手动暂停 | 日志 `Automatic email sync is paused` | UI 里恢复自动同步；若为有意暂停，可临时静默该告警 |
| `skipped` | 上一次同步还在跑 / 另一进程持有租约 | 日志 `already running` 或 `running in another process` | 确认无重复实例；长期卡住则重启服务释放租约 |
| `failed` | Google 账号未连接 | 日志 `Google email is not connected` | UI 重新走 OAuth 授权 |
| `failed` | OAuth token 失效 | `reason="auth"` 的样本在增长；日志有 `Google token refresh failed` 或 `Gmail API rejected the request: HTTP 401/403` | 重新授权，见 [EmailSyncAuthFailure](#emailsyncauthfailure) |
| `failed` | Gmail API 限流或网络问题 | `reason="rate_limit"`；日志有 429 / 超时 | 等待重试；持续则调大同步间隔 |

**验证恢复** — 手动触发一次同步，成功后 Gauge 更新，告警在下一个评估周期消退。

**何时升级** — 重新授权后仍连续失败，或超过 6 小时未成功。

---

## EmailSyncAuthFailure

**告警条件**

```promql
increase(jobradar_email_sync_runs_total{status="failed",reason="auth"}[1h]) > 0
```

持续 5 分钟。

**与 EmailSyncStale 的分工** — 这条抓**根因**（授权坏了），5 分钟就响；那条抓**后果**（1 小时没成功），不区分原因。授权失效不会自愈，必须人工介入，所以不等一小时。两条同时红是正常的，按本节处置即可。

**现象** — Gmail 同步因授权问题失败。

**影响** — 同 EmailSyncStale：投递状态追踪停止更新，搜索和评分不受影响。

**先看哪里**

1. 确认分类：

   ```promql
   increase(jobradar_email_sync_runs_total{status="failed",reason="auth"}[1h])
   ```

2. 定位具体是哪一类。异常路径记在**日志的 WARNING 级**（`GET /api/logs?lines=100&level=WARNING`），由 `server.py` 的 `_email_sync_loop` 以 `Scheduled email sync failed: <异常消息>` 打出：

| 消息 | 含义 |
|---|---|
| `Google token refresh failed: ...` | refresh token 被 Google 拒绝（多为用户在账号设置里撤销了授权） |
| `Google authorization is invalid; reconnect the account` | token 过期且没有 refresh token |
| `Gmail API rejected the request: HTTP 401 Unauthorized` | token 失效 |
| `Gmail API rejected the request: HTTP 403 Forbidden` | 权限不足，或 Gmail API 配额 / 项目配置问题 |

> **「账号未连接」不会出现在日志里。** 这条路径是 `return`，不是 `raise`（`email_sync.py` 的 `if not email_sync_configured()`），所以 `_email_sync_loop` 拿到的是正常返回值，只会打一行 INFO，看不出异常。它同样计入 `reason="auth"`，但只能从 `GET /api/email/status` 的 `connected: false` 和 `latest_sync.error_message` 里确认。**指标红了但日志干净时，先查这一条。**

**处置**

1. 打开 Web UI，断开并重新连接 Google 账号，走完 OAuth 授权
2. 手动触发一次同步
3. 确认 `jobradar_email_sync_runs_total{status="success"}` 有新增

403 且重新授权无效时，去 Google Cloud Console 检查 OAuth 客户端是否仍启用、Gmail API 是否开启、scope 是否包含 `gmail.readonly`。

**验证恢复** — 同步成功后，`reason="auth"` 的 counter 停止增长，告警在 1 小时窗口滑过后消退。**告警不会立即消失是正常的**——`increase(...[1h])` 需要等失败样本滑出窗口。

**配置耦合** — 本规则的 1 小时窗口假设同步间隔远小于 1 小时（默认 900s）。若把 `EMAIL_SYNC_INTERVAL_SECONDS` 调到 1 小时以上，窗口内可能一次失败都统计不到，规则会失效，需同步调大窗口。

### 已知盲区：计数器停止增长时告警不会响

这条规则用的是 `increase()`，它测量的是**增量**，不是绝对值。计数器停在某个非零值不动时，`increase()` 返回 0，告警保持 inactive——哪怕授权确实是坏的。

有两种情况会踩到：

| 情况 | 为什么 |
|---|---|
| 授权已损坏的状态下重启 JobRadar，且此后不再产生新的失败（例如自动同步被暂停） | 计数从 0 重新开始，停在某个值不动 |
| 所有失败都发生在 Prometheus 首次抓到该序列之前 | 序列"一出生"就是那个值，之前没有样本可比，首次出现不计作增长 |

第二种在实测中真实遇到过：连续触发 3 次失败后计数为 3，但 `increase(...[1h])` 一直是 0，直到第 4 次失败让它涨到 4 才进入 pending。

**这不是缺陷，是取舍。** 换成绝对值判断（`... > 0`）会有更糟的副作用：一旦历史上发生过任何一次认证失败，告警就永远响着，直到进程重启才清零——完全不可用。

**兜底手段是 `EmailSyncStale`**：它看的是"最后一次成功距今多久"，与失败是否还在累积无关。所以这两条规则不是重复，是互补：

- 本规则 = 快速定位根因，代价是可能漏报
- `EmailSyncStale` = 兜底，慢但漏不掉

**排查时的实际影响**：怀疑授权有问题但本告警没响，不要因此排除授权原因。直接查绝对值：

```promql
jobradar_email_sync_runs_total{status="failed",reason="auth"}
```

有值就说明这个进程生命周期内发生过认证失败，再对照 `GET /api/email/status` 的 `latest_sync` 判断当前是否仍在失败。

**何时升级** — 重新授权后仍出现 `reason="auth"` 失败，说明不是 token 问题，查 Google Cloud 项目配置。

---

## 附录

### 已知外部依赖问题

| 现象 | 原因 | 处置 |
|---|---|---|
| Indeed 抓取结果为 0 | JobSpy 被 Indeed 限流（常见，非故障） | 等几小时重试；查日志中 JobSpy WARNING |
| Adzuna 返回 429 | 速率限制 | 调大 `_MIN_INTERVAL`（当前 1.2s）；核对 `.env` 中 `ADZUNA_APP_ID` / `ADZUNA_APP_KEY` |
| LLM 评估全部拒绝 | `cv_summary` / `cv_skills` 提取失败 | 检查 CV 解析结果；`uv run jobradar assess` 补跑评估 |
| 职位无模型评分 | 当前 `cv_hash` 下尚无 `job_matches` 记录（换 CV 后常见）；默认 Web 列表不会展示这类历史卡片 | 仅在需要主动补算历史 JD 时运行 `uv run jobradar assess` |

### 安全注意

- **`/metrics` 不得暴露到公网。** 它泄露内部路由结构、流量规律和错误分布，是攻击者的侦察素材。本地开发无妨；若部署到云上，必须置于内部监听器之后或用安全组限制来源。
- **指标 label 不得包含敏感数据**（邮箱地址、API key、CV 内容、公司名）。指标会被 Grafana 及任何有读权限者看到，且带保留期。现有 label 仅为 `trigger` / `status` / `reason` / `method` / `handler`，均为低基数枚举值——新增指标时保持这一原则。`reason` 是个正面例子：它只有 4 个固定取值（`auth` / `rate_limit` / `other` / `none`），而不是把 `str(exc)` 直接塞进去——后者会让每条不同的错误消息都变成一个新时间序列。
- **label 基数**：`handler` 用的是路由模板（`/api/jobs`）而非真实路径。新增 label 前先问「这个值最多有多少种取值」，高基数 label（ID、URL、hash）会让 Prometheus 内存爆掉。

### 修改告警规则后的验证流程

```bash
# 1. 语法校验
docker run --rm -v "${PWD}/monitoring:/etc/prometheus" --entrypoint promtool \
  prom/prometheus:latest check rules /etc/prometheus/alerts.yml

# 2. 完整配置校验（含 rule_files 路径解析）
docker run --rm -v "${PWD}/monitoring:/etc/prometheus" --entrypoint promtool \
  prom/prometheus:latest check config /etc/prometheus/prometheus.yml

# 3. 重载
cd monitoring && docker compose up -d --force-recreate prometheus

# 4. 确认加载：/rules 页面应显示全部规则且 health=ok
```

**改完必须实测触发一次。** 没验证过会触发的告警等于没有告警——停掉服务等 90 秒，确认 `JobRadarDown` 走完 Inactive → Pending → Firing，再恢复服务确认它会消退。
