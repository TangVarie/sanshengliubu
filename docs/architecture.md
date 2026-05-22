# sanshengliubu Architecture Notes

本文档记录 sanshengliubu 仓库的跨模块设计选择 — 那些零散写在 docstring
和注释里、但需要新人在 onboarding 时第一时间看到的决策。

---

## 1. 为什么有三套 LLM 重试 (R-026 设计选择)

sanshengliubu 跑多家 LLM 后端,**目前是三套重试机制并存**,不打算强行统一。
这是 2026-05-22 audit R-026 的明确权衡。

### 对照表

| backend | retry 源 | 基底 | 最大尝试 | max_wait | 在哪里 |
|---------|---------|------|---------|----------|--------|
| **Claude** (Anthropic / Vertex / 中转) | `BaseAgent.run()` | 3s 普通 / 10s for 5xx | `MAX_RETRIES + 1` (默认 4) | `MAX_RETRIES` 控制 | `pipeline/agents/__init__.py:1379` |
| **Gemini** (Vertex Express) | `call_with_retry()` | 2s | 3 | 30s | `pipeline/llm_retry.py` + `pipeline/agents/gemini_client.py` |
| **DeepSeek** | `BaseAgent.run()` (anthropic-compat 路径) | 3s | `MAX_RETRIES + 1` | — | 复用 Claude 路径,见 `_call_claude` |
| **OpenAI / GPT** | `BaseAgent.run()` (gpt branch in `_call_claude`) | 3s | `MAX_RETRIES + 1` | — | 复用 Claude 路径 |

### 为什么不统一

`BaseAgent._call_claude` 已包含 4 项耦合的状态机:
1. **Retry** — 按异常类型分流可重试 / 不可重试,5xx 用更长退避
2. **Per-run budget** — `_check_run_budget` 累计 token / cost,超 `MAX_TOKENS_PER_RUN` 强制终止
3. **Sliding-window rate limiter** — `_SlidingWindowLimiter` 维护 RPM + concurrency 双约束
4. **Cache-fallback** — 中转拒绝 `cache_control` 时自动降级重试

把这套搬进 `llm_retry.py` 是另一个 PR 的工作量 (1-2 天 + e2e 测试),
不在 R-026 (Gemini 跟齐) 的承诺范围。Gemini 调用 **没有** budget / limiter /
cache-fallback (Vertex 走自家配额),所以独立薄重试就够用。

### 设计后果运维需要知道

- "为什么 Claude 重试等了 10s, Gemini 等了 30s" — 不是 bug,是有意:
  Vertex grounding 429 恢复周期 10-20s,Gemini 用 14s 还没到拐点就放弃
  (truth-vault `annotate_essence_pass.py` 用 2+4+8=14s 是按 Anthropic 节奏
  调的,对 Gemini 偏短)
- 调 Claude 路径的退避参数 → 改 `pipeline/config.py::MAX_RETRIES / RETRY_BASE_DELAY_SECONDS`
- 调 Gemini 退避参数 → 改 `pipeline/agents/gemini_client.py` 里 `call_with_retry(...)` 的
  `max_attempts / initial_wait / max_wait`

### 未来统一的触发条件

任一条件成立时,值得起一个 R-026.2 PR 把 BaseAgent 也迁过来:
- 出现第三个非 Anthropic LLM backend (e.g. 直连 Mistral)
- 运维要"全局重试可观测",需要统一日志格式
- `BaseAgent.run()` 自身改动频繁,维护重试逻辑成本超过迁移成本

---

## 2. 飞轮可观测:R-022 audit 信号去哪里查

vibe_loop 每轮会跑 `_audit_rewrite_source_tags` 检查 `rewrite_summary` 字段的
`源:数据库样本 #<id>` / `源:静态兜底 #<编号>` 标记,这是飞轮 ROI 的唯一信号。

### 三个落点

1. **stderr 日志** (`[R-022 audit] ...`) — 实时,适合 dev 调试
2. **`stage_logs` 表** (`stage_name='r022_flywheel_audit'`) — 持久化,
   每个 vibe_loop iteration 一行,**适合 SQL 日报 / TV 自检**
3. **Streamlit UI** (流水线详情页) — 通过 stage_logs 自然渲染出来

### SQL 自检模板

```sql
-- 过去 24 小时所有 audit 行(包括 warn)
SELECT
    sl.run_id,
    pr.project_id,
    sl.created_at,
    sl.status,
    sl.output_data->>'iteration' AS iter,
    sl.output_data->>'total_vibe_cells' AS vibe_cells,
    sl.output_data->>'db_sourced' AS from_db,
    sl.output_data->>'static_sourced' AS from_static,
    sl.output_data->'missing_tag_cell_ids' AS missing,
    sl.output_data->'excess_static_by_platform' AS excess
FROM stage_logs sl
JOIN pipeline_runs pr ON sl.run_id = pr.id
WHERE sl.stage_name = 'r022_flywheel_audit'
  AND sl.created_at > NOW() - INTERVAL '24 hours'
ORDER BY sl.created_at DESC;

-- 只看有 warning 的 (missing_tag 或 excess_static_use 非空)
SELECT * FROM stage_logs
WHERE stage_name = 'r022_flywheel_audit'
  AND status = 'completed_warn'
  AND created_at > NOW() - INTERVAL '24 hours';

-- 飞轮命中率:DB 来源 cell / 总 vibe cell (过去 7 天)
SELECT
    DATE(sl.created_at) AS day,
    SUM((sl.output_data->>'db_sourced')::int) AS db_anchored,
    SUM((sl.output_data->>'static_sourced')::int) AS static_anchored,
    SUM((sl.output_data->>'total_vibe_cells')::int) AS total,
    ROUND(
      SUM((sl.output_data->>'db_sourced')::int)::numeric
      / NULLIF(SUM((sl.output_data->>'total_vibe_cells')::int), 0),
      3
    ) AS db_hit_rate
FROM stage_logs sl
WHERE sl.stage_name = 'r022_flywheel_audit'
  AND sl.created_at > NOW() - INTERVAL '7 days'
GROUP BY DATE(sl.created_at)
ORDER BY day DESC;
```

### 日报 / 告警 hook 怎么接

最简单:cron 每小时跑一次"过去 1 小时 `completed_warn` 行数 > 0 就发告警"。
sanshengliubu 自己没有 cron 基础设施时,**让 truth-vault 仓的日报 SQL 多读一张
sanshengliubu 表** 即可 — TV 的飞轮自检本来就跨仓查 stage_logs。

中等成本:在 sanshengliubu 加 `scripts/check_audit_warnings.py`,GitHub Actions
cron schedule 跑,grep 最近 24 小时 stage_logs 的 audit 行,把汇总发 Slack。
工时约 2 小时,优先级 P3。

### 字段 schema (落库 output_data)

```jsonc
{
  "iteration": 1,                          // 第几轮 vibe_loop (1-indexed)
  "total_vibe_cells": 8,                   // 本轮 vibe_rewriter 跑了几个 cell
  "db_sourced": 5,                         // 锚点写了"源:数据库样本"的 cell 数
  "static_sourced": 3,                     // 锚点写了"源:静态兜底"的 cell 数
  "missing_tag_cell_ids": ["D2_xhs"],      // 没写"源:*" 标记的 cell_id
  "excess_static_by_platform": {           // 哪些平台的静态使用超出配额
    "小红书": {
      "vibe_cells_in_batch": 4,
      "available_packs": 3,
      "allowed_static_at_most": 1,
      "actual_static": 2,
      "excess": 1
    }
  },
  "per_platform_total": {"小红书": 4, "抖音": 4},
  "reference_packs_summary": {             // 来自 retrieve_samples.summarize_packs_by_platform
    "total_packs": 10,
    "platforms_hit": {"小红书": 6, "抖音": 4},
    "platforms_missed": [],
    "tv_synced_total": 7,
    "manual_total": 3
  },
  "has_warnings": true                     // shortcut: missing OR excess 非空
}
```

---

## 3. 单租户假设 (R-019)

详见 `README.md` 顶部警告条 + `db/schema.sql` 注释块。简言之:

- 5 张主表显式 `DISABLE ROW LEVEL SECURITY`,是单租户 MVP 的有意选择
- 多租户改造方案见姐妹仓 truth-vault 的
  `sanshengliubu-patches/005_multi_tenant_workspaces.sql`
- `app.py` sidebar caption 每次提醒运营当前模式

---

## 4. Secret masking 与 truth-vault 对齐 (R-023)

`pipeline/logger_utils.py::_SECRET_PATTERNS` 是 truth-vault 仓
`scripts/_common.py::_SECRET_PATTERNS` 的 shadow copy,7 个模式覆盖
Anthropic / Supabase service / Supabase PAT / JWT / Google / OpenAI scoped /
generic sk-* token。

TV 新增 vendor 时**记得同步过来**,否则该 vendor 的 token 在 ssll 日志里
不会被 mask。

`app.py` 启动时调 `install_secret_masking_on_root_logger()`,后续任何
`logger.exception` 写 stderr 之前都会过 mask。

---

## 后续 sprint backlog (audit 2026-05-22 未实施项)

| ID | 主题 | 工时 | 触发条件 |
|----|------|------|---------|
| R-018 | daemon thread → 持久 job worker (jobs 表 + 独立 worker 进程) | 1-2 周 | 浏览器关闭丢任务变成日常痛点 |
| R-028 | cell-level resume (stage_logs 加 cell_status JSONB) | 1-2 天 + schema | matrix > 30 cells 时 resume 成本显著 |
| R-026.2 | 把 BaseAgent.run() 重试迁到 llm_retry.py | 1-2 天 | 第三个非 Anthropic backend / 全局重试可观测要求 |
