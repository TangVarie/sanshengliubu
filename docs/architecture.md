# sanshengliubu Architecture Notes

本文档记录 sanshengliubu 仓库的跨模块设计选择 — 那些零散写在 docstring
和注释里、但需要新人在 onboarding 时第一时间看到的决策。

---

## 1. 为什么有三套 LLM 重试 (R-026 设计选择)

sanshengliubu 跑多家 LLM 后端,**目前是两套重试机制并存**,不打算强行统一。
这是 2026-05-22 audit R-026 的明确权衡(v0.32.0 换厂后更新)。

### 对照表

| backend | retry 源 | 基底 | 最大尝试 | max_wait | 在哪里 |
|---------|---------|------|---------|----------|--------|
| **Kimi** (Moonshot anthropic-compat) | `BaseAgent.run()` | 3s 普通 / 10s for 5xx | `MAX_RETRIES + 1` (默认 4) | `MAX_RETRIES` 控制 | `pipeline/agents/__init__.py::_call_model` |
| **DeepSeek** (官方 anthropic-compat) | `BaseAgent.run()` | 3s | `MAX_RETRIES + 1` | — | 同上,共用一条路径 |
| **辅助层** (Kimi / DeepSeek,二审·结构审·Vision) | `call_with_retry()` | 2s | 3 | 30s | `pipeline/llm_retry.py` + `pipeline/agents/kimi_client.py` |
| **SocialDataX** (MCP,非 LLM) | 自带 transient 重试 | 1s / 2s | 3 | — | `pipeline/agents/socialdatax_client.py` |
| **Claude / GPT**(历史路径,默认不再使用) | `BaseAgent.run()` | 3s | `MAX_RETRIES + 1` | — | 路由仍在,靠 `model_overrides` 才会走到 |

主链路的三家(Kimi / DeepSeek / 历史 Claude)都说 Anthropic Messages 协议,
所以共用同一条 `_call_model`;GPT 走 `_call_openai_chat` 分支,返回同样的
7-tuple,上层不分叉。

### 为什么不统一

`BaseAgent._call_model` 已包含 4 项耦合的状态机:
1. **Retry** — 按异常类型分流可重试 / 不可重试,5xx 用更长退避
2. **Per-run budget** — `_check_run_budget` 累计 token / cost,超 `MAX_TOKENS_PER_RUN` 强制终止
3. **Sliding-window rate limiter** — `_SlidingWindowLimiter` 维护 RPM + concurrency 双约束
4. **Cache-fallback** — 上游拒绝 `cache_control` 时自动降级重试

把这套搬进 `llm_retry.py` 是另一个 PR 的工作量 (1-2 天 + e2e 测试)。

更重要的是**辅助层本来就不该要这四项**:它是 advisory,二审挂了就当没二审。
让它去占主链路的 run 预算、甚至触发 `RunBudgetExceededError` 把整条 run 判死,
是明确的错误行为。所以"两套并存"不只是技术债,有一半是有意的隔离。

### 设计后果运维需要知道

- "为什么主链路重试等了 10s,辅助层等了 30s" — 不是 bug,是有意:主链路的
  退避按 Anthropic 协议族的节奏调(3s/6s/12s),辅助层的 30s 上限更宽,
  因为它慢一点没关系,失败才有关系
- 调主链路退避参数 → 改 `pipeline/config.py::MAX_RETRIES / RETRY_BASE_DELAY_SECONDS`
- 调辅助层退避参数 → 改 `pipeline/agents/kimi_client.py` 里 `call_with_retry(...)` 的
  `max_attempts / initial_wait / max_wait`
- **辅助层的 token 不进 run 总账** —— 和 v0.31 的 Gemini 层一样。成本通过
  返回值的 `cost_usd` 单独上报,`pipeline_run.total_cost_usd` 里看不到它

### 跨厂家 fallback(v0.32.0 新增)

`MODEL_FALLBACK_CHAIN = [kimi-k2.6, kimi-k3, deepseek-v4-flash]`。上游返回
"无可用渠道"时按链降级。和 v0.31 的区别是这条链**跨厂家**:Moonshot 整体故障
时仍能靠 DeepSeek 把流水线以降级质量跑完。

代价是候选可能压根没配 key,所以 `_model_fallback_candidates` 会先用
`backend_configured()` 过滤 —— 否则 "DEEPSEEK_API_KEY 没配" 这种配置错误会被
fallback 循环当成一次普通失败,最后抛出的错跟真实原因对不上。

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

## 5. 质量评分:双层评分体系去哪里查

`pipeline/quality_metrics.py` 在出货前(`orchestrator.run()` 第 6.97 步,
`save_output` 之前)给最终 `prompt_matrix` 打一次分,落 `stage_logs`
(`stage_name='quality_score'`)。

### 为什么加这一层

在此之前本仓库**只有输入侧遥测**。R-022 飞轮 audit 追的是"数据库样本有没有
被用上",没有任何东西回答得了"这一版产出比上一版好吗"。后果是改提示词全凭
手感:改完 `works_builder.md` 跑一条 run,觉得 demo 看着顺眼就当改对了 ——
样本量 1,还是被红蓝 + 网感重写打磨过 3 轮的那 1 篇。

### 零 LLM 成本

- **红线层** — 纯 Python 确定性判定,不调模型
- **高分层** — 从 `vibe_critic` 已产出的 `cell_reviews` 里提取
  (`multiplier_gate` 四项 + `template_test`),不重复判

`vibe_loop` 跨轮次把 `cell_reviews` 按 cell_id 累积到
`final_system._vibe_cell_reviews`。这一步是必需的:round 2+ 只复检被改写过的
cell,单看最后一轮的 `critic_result` 会漏掉 round 1 就通过的那些格子。

### 两个数字的读法不同(最容易被改错的地方)

| 层 | 指标 | 目标 | 思维 |
|----|------|------|------|
| 红线层 | 通过率 % | **100%** | hill-climbing,零容忍尾部失败 |
| 高分层 | 高分篇**绝对数** | 翻倍(如 4/12 → 8/12) | 上限思维,保方差 |

高分层**有意不提供 `high_score_rate` 字段**。把它当 pass rate 优化会触发:
mutation 倾向"消除尾部低分篇" → 分布向均值收窄 → 把 60 分拉到 75 分的同时
把 95 分压到 85 分 → 方差消失 → 爆款消失。而小红书内容价值分布极度长尾,
100 篇里 5 篇爆款的价值远大于 100 篇都 85 分。

另外三条设计约束,改动时不要破坏:

1. **红线是准入,高分是排名。** 有红线违规的 cell 一律不进高分篇计数,哪怕
   高分层拿满 6 项。这类格子单独标 `high_score_blocked_by_redline` ——
   它们是修复性价比最高的。
2. **红线层只收"命中即死"型判定(黑名单)。** "应存在"型检查(必须有 X)一律
   放高分层。理由见 v0.32.3 / v0.32.4 两次事故:那两次都是"应存在"型检查
   误判导致批次重试 + 单 cell 重试的三轮空烧。黑名单的假阳性率天然低,
   "应存在"的假阴性率天然高。
3. **工艺四要素按 paradigm 分流。** 「具体性四要素」是 `works_builder.md`
   写在**范式 A 专用**段下的;范式 B(元评论应答体)靠术语锚和结构建立信任。
   拿 A 的尺子量 B 会系统性低估所有 B 格子 —— 实测 `foundation.md` 里那条
   被当范例的真实洗洁精爆款,按 A 判会缺 3 项。

### 词表同步义务

`AI_CLICHE_BLACKLIST` / `BANNED_OPENING_PREFIXES` 是提示词里几处禁用清单的
shadow copy,和第 4 节 `_SECRET_PATTERNS` 与 truth-vault 的对齐是同一个模式。
同源出处:

- `vibe_critic.md` 第 0.5 步「AI 空话硬否决」黑名单
- `works_builder.md` 范式 A (a)(c) + 范式 B「反 AI 腔禁用清单」
- `foundation.md`「反面教材——这些都是伪网感」

**改一边要同步另一边**,否则评分标准和提示词要求会悄悄分叉:分数还在涨,
产出已经不按新规矩走了。

### SQL 自检模板

```sql
-- 最近 20 条 run 的分数曲线(这是判断"改动有没有用"的主视图)
SELECT
    pr.project_id,
    sl.created_at,
    sl.output_data->>'total_cells'               AS cells,
    sl.output_data->>'redline_pass_cells'        AS redline_ok,
    (sl.output_data->>'redline_pass_rate')::float AS redline_rate,
    sl.output_data->>'high_score_cells'          AS high_score,
    sl.output_data->>'high_score_coverage_cells' AS covered,
    sl.output_data->'redline_violation_tally'    AS violations
FROM stage_logs sl
JOIN pipeline_runs pr ON sl.run_id = pr.id
WHERE sl.stage_name = 'quality_score'
ORDER BY sl.created_at DESC
LIMIT 20;

-- 红线违规按类型排行(过去 30 天)——告诉你该先修哪条规则
SELECT
    v.key   AS rule,
    SUM(v.value::int) AS hits,
    COUNT(DISTINCT sl.run_id) AS runs_affected
FROM stage_logs sl,
     LATERAL jsonb_each(sl.output_data->'redline_violation_tally') v
WHERE sl.stage_name = 'quality_score'
  AND sl.created_at > NOW() - INTERVAL '30 days'
GROUP BY v.key
ORDER BY hits DESC;

-- 高分篇数的周趋势。⚠️ 只在 covered = cells 的 run 之间比较 ——
-- 覆盖不满说明 critic 没测全,那条 run 的高分篇数是被低估的,混进来会污染曲线。
SELECT
    DATE_TRUNC('week', sl.created_at)::date AS week,
    COUNT(*)                                 AS runs,
    SUM((sl.output_data->>'high_score_cells')::int) AS high_score_cells,
    SUM((sl.output_data->>'total_cells')::int)      AS total_cells
FROM stage_logs sl
WHERE sl.stage_name = 'quality_score'
  AND (sl.output_data->>'high_score_coverage_cells')::int
      = (sl.output_data->>'total_cells')::int
GROUP BY week
ORDER BY week DESC;
```

### 字段 schema (落库 output_data)

```jsonc
{
  "total_cells": 12,
  "redline_pass_cells": 10,
  "redline_pass_rate": 0.8333,
  "redline_violation_tally": {"ai_cliche": 3, "duplicate_opening": 2},
  "high_score_cells": 5,              // 绝对数,不提供 rate(见上)
  "high_score_threshold": "5/6",
  "high_score_coverage_cells": 12,    // 高分层测全的格子数
  "per_cell": [
    {
      "cell_id": "D1_xiaohongshu",
      "platform": "小红书",
      "direction_id": "D1",
      "redline_violations": [{"rule": "ai_cliche", "hit": "性价比高", "detail": "..."}],
      "redline_pass": false,
      "high_score_items": {            // true / false / null(=没测)
        "reward_signal": true, "interest_align": true,
        "gap_tension": true, "identity_consistency": true,
        "template_test": true, "craft": false
      },
      "high_score_earned": 5,
      "high_score_scored": 6,
      "is_high_score": false,
      "high_score_blocked_by_redline": true,   // 味道对但踩了硬伤 = 优先修
      "craft_missing": ["具体情绪反应"],
      "paradigm": "A_emotional_hook"
    }
  ]
}
```

---

## 后续 sprint backlog (audit 2026-05-22 未实施项)

| ID | 主题 | 工时 | 触发条件 |
|----|------|------|---------|
| R-018 | daemon thread → 持久 job worker (jobs 表 + 独立 worker 进程) | 1-2 周 | 浏览器关闭丢任务变成日常痛点 |
| R-028 | cell-level resume (stage_logs 加 cell_status JSONB) | 1-2 天 + schema | matrix > 30 cells 时 resume 成本显著 |
| R-026.2 | 把 BaseAgent.run() 重试迁到 llm_retry.py | 1-2 天 | 第三个非 Anthropic backend / 全局重试可观测要求 |
