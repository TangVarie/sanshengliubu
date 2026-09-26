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
| **判定服务** (judge / Jev,HTTP,v0.37.0) | `call_with_retry()` | 1s | 2 | 2s | `pipeline/agents/judge_client.py`;只重试 429 / 502 / 504 / 超时 / 连不上,单次超时 8s |
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
- **辅助层的 token 不进 run 的 input/output 计数**(不碰 `MAX_TOKENS_PER_RUN` 熔断),
  但**成本进总账**:辅助层客户端只把 `cost_usd` 放在返回值里,orchestrator 在调用点经
  `accumulate_auxiliary_cost(..., source=...)` 记进 run 的 `cost_usd`(二审
  `gemini_critic`、结构审 `gemini_structure`、批量采样 `batch_sampling`、判定服务
  `jev_judge`),下一次主链路 stage 完成时随 totals 推到 `pipeline_run.total_cost_usd`。
  (v0.37.0 更正:此前这里和 `accumulate_auxiliary_cost` 的 docstring 都写着「看不到它」,
  和代码对不上。)
- 判定服务的重试**不能**照搬辅助层:`_is_transient` 按异常文本找状态码,而 422 的
  detail 里带 subject_id(含 run 的 UUID,随时可能出现「503」「429」子串)。所以
  `judge_client` 对非瞬时状态**返回**状态码、不抛异常,只有 429 / 502 / 504 / 超时 /
  连不上才以异常形式进 `call_with_retry`

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

v0.37.0 起这条义务由测试兜底(`tests/test_ai_cliches.py`),方向是**提示词 ⊆ Python**:
`vibe_critic.md` 第 0.5 步、`vibe_rewriter.md`「通篇禁止」、`works_builder.md` 范式 A (c)
三份清单里的每一条,都必须能在 `AI_CLICHE_BLACKLIST`、`BANNED_OPENING_PREFIXES` 或
`PROMPT_ONLY_AI_CLICHES`(提示词有、机审有意不按全文查的,写明原因)里找到。反方向
不强求 —— 提示词按设计只放少量带原因的禁令(第 9 节),逼着把机审全量抄进三份提示词
等于把注意力预算花在清单上。

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

## 6. 终审输入为什么要 allowlist (v0.33.2)

`chancellery_final` 跑 kimi-k3 ($3 in / $15 out),是全流水线单价最高的一档,
而它拿到的曾经是**整个 `final_system`**。

到终审这一步,`final_system` 上已经堆了 8 个上游阶段挂的诊断数据。而
`chancellery.md` 全文只引用两样东西:`prompt_matrix`,和 orchestrator 单独
注入的 `narrative_director_summary`。

最刺眼的是 **demo 正文被送了三遍**:

| 位置 | 提示词有引用吗 |
|------|----------------|
| `prompt_matrix[i].demo_output` | ✅ 唯一被引用的那份 |
| `demo_outputs[i].output_content` | ❌ 从未提及 |
| `_red_blue_stats.details[i].refined_demo_output` | ❌ 从未提及 |

12 个格子的实测:瘦身前 104K 字符 → 瘦身后 54K,**砍掉 49%**。

### 这是 v0.30.5 那批 dead drop 的镜像问题

v0.30.5 修的是「数据注入了但提示词不知道去读」(漏信号);这次是「数据注入了
而提示词根本不需要读」(烧钱)。同一个根因:**没人管 `final_system` 上到底
该有什么** —— 每个阶段都往上挂自己的诊断包,没有一个环节负责清理。

### 为什么用 allowlist 而不是 blocklist

blocklist 挡不住"以后又有人往 `final_system` 上挂新字段"这个复发路径,而这
正是问题的来源。代价是将来提示词要用新字段时得记着去
`agents/chancellery.py::_FINAL_REVIEW_KEEP_KEYS` 加一行 —— 所以被丢掉的 key
会打日志(`[chancellery_final] 输入瘦身:丢掉 N 个…`),drift 是可见的。

改 `chancellery.md` 让终审引用新字段时,**必须同步加进 allowlist**,否则
提示词让它看的东西根本不在输入里。

---

## 7. 批量采样验收:为什么 n=1 验收不成立 (v0.33.3)

`pipeline/batch_sampler.py`,跑在终审之后、质量评分之前(`run()` 第 6.96 步)。

### 缺口

交付物是 system_prompt,它的工作是产出 N≥10 篇。但在此之前,**11 道质量闸看的
都是每个 cell 唯一那篇 `demo_output`** —— 红蓝、画像模拟 ×2、结构审、网感
critic、二审、消费者模拟、终审,全部对着同一个样本。

三个后果:

1. **5 池 + 人设轮换机制从来没被验证过。** 决定"第 7 篇会不会和第 3 篇一个味"
   的就是这套机制,而流水线里唯一的检查是数一数"5 个池的文字在不在 prompt 里"。
   **存在 ≠ 有效。**
2. **n=1 测的是 demo 的质量,不是 prompt 的分布。** 那篇 demo 经过红蓝精修、
   网感重写、结构补漏最多 3 轮打磨,反映的是"这 8 道工序能把一篇修到多好"。
3. **优化方向是反的。** 目标是上限(100 篇里 5 篇爆款 > 100 篇都 85 分),
   但最贵的两个环节(红蓝、网感重写)干的事恰恰是把唯一样本往均值推 ——
   n=1 下无法区分"抹掉了尾部烂篇"和"抹掉了头部爆款"。

### 做法:只变 seed

拿最终 system_prompt 真跑 N 篇(默认 5),**只变 `{{seed}}`**,topic 固定、
persona **留空**。

- 只变 seed 是因为那正是 `works_builder.md` 批量规则承诺的差异化开关
  (「相邻两篇的 seed 值建议间隔 ≥ 20」)。用它自己承诺的口径去测它。
- persona 留空是有意的:手动指定人设等于替 prompt 做了它本该做的事,
  那条轮换规则也就测不出来了。

### 三个指标

| 指标 | 口径 | 为什么 |
|------|------|--------|
| 样本红线通过率 | 追 100% | 和质量评分同一套规则,不同样本空间 |
| 首句去重率 | 取**各 cell 最差值** | 一个格子趋同就是一个格子的批量废了,均值会盖掉它 |
| 两两相似度 | 字符 trigram Jaccard,越低越好 | 首句去重率会被「换词伪装」骗过去 |

第三个指标是必需的:实测「每篇开头都不同但正文只换了几个词」这种样本,
首句去重率 100%,相似度均值 0.45 —— 光看去重率会放过它。

### 观测,不拦

采样结果**不参与出货判决**:不阻塞、不触发重写、不影响 verdict
(`mode: "observe_only"`)。

理由是仓库现在还没有历史分布。没有基线就设阈值等于凭猜调参 —— 那正是这轮
改造要治的病。先攒几条 run,再决定阈值定在哪、要不要升级成闸门。

### SQL

```sql
-- 采样质量随时间的变化(升级成闸门之前先看这个,别急着定阈值)
SELECT
    sl.created_at,
    sl.output_data->>'total_samples'                   AS samples,
    (sl.output_data->>'redline_pass_rate')::float      AS redline_rate,
    (sl.output_data->>'worst_cell_unique_opening_ratio')::float AS worst_uniq,
    (sl.output_data->>'worst_cell_max_similarity')::float       AS worst_sim,
    sl.output_data->'_usage'->>'cost_usd'              AS cost
FROM stage_logs sl
WHERE sl.stage_name = 'batch_sampling'
  AND sl.output_data->>'status' = 'ok'
ORDER BY sl.created_at DESC LIMIT 30;
```

---

## 8. 跨批次多样性:为什么是扩容而不是新加机制 (v0.33.4)

交付的 system_prompt 有 `{{seed}}` 做批内差异化,但**没有任何跨批次机制**。
运营不是跑一次就完,是每天跑、连着跑几十批 —— 跑到第 10 批必然撞车,而且撞了
没人看得出来。

### 修法:给现有的「开头切入池」扩容并编号,而不是新加一套机制

5 池里的 `opening_angle` 原来只要求"至少 5 个"。5 种粒度的组合跑 5-10 批就用光,
拆到 15 种(`C01`-`C15`)能撑 20-30 批。这是**纯粒度问题**,不需要新模块。

不新加机制是有原因的:`works_builder.md` 的字符预算刚在 v0.33.1 收紧过
(13 个必嵌模块 ≈2400 字 / 上限 3000),硬塞一套 500 字的新机制会把刚修好的
预算再撑爆 —— 那就是一边修一边破。扩容只多约 100 字符。

`opening_angle` 是五个池里**唯一**要求满 15 条的,其余四池仍是"至少 5 个":
它们管批次内差异化,只有开头角度还要管跨批次。

### 编号形式是为了让回避能落地

运营粘贴「上批已用:C03 C07 C11」比粘贴一堆角度描述可操作得多。两条条件规则
内置在交付的 prompt 里,用户不填就正常跑:

- `used_angles` —— 跨批次回避
- `account_profile` —— 多账号从根部分岔(避免"5 个账号发出来像一个人写的")

### 校验:存在 ≠ 有效

`_validate_prompt_cell` 数 system_prompt 里的 `C01`-`C15` 编号,少于 10 条报
**软告警**(不触发重试,遵循本函数其余检查的尺度)。

只数编号不查内容:C01-C15 这种编号在正常中文里几乎不会误命中(实测
`维生素C` / `C 罩杯` / `C16` / `C99` 全不命中),是个高精度信号。

### 诚实边界

这是**补偿不是根治**,`pages/4_output_center.py` 会把这句话如实写给运营看。

大模型是无状态的 —— 它不知道昨天用同一条 prompt 生成过什么。真正的跨批次去重
需要工作台做三件事:维护已生产资产库、每次生成自动注入避重池、账号间自动分配
差异化档案。这三件事没做之前,跑到第 20-30 批仍然会开始撞车,届时该推的是
工作台改造而不是继续改 prompt。

---

## 9. 机审闸 prose_gate:三档分级为什么是必需的 (v0.33.5)

`pipeline/prose_gate.py` —— 所有「机械可判」的文字指纹检测收归一处。

### 定位

去 AI 味的规则此前以禁令清单形态散在礼部产出和工部装配的 prompt 里。代价:
信号稀释(模型注意力从"写"转到"查")、防御性写作(一边写一边躲清单,稿子会紧)、
让 LLM 查禁词是用判断力干正则的活(烧 token 且带方差)。

**机械可判的下沉到 prose_gate,需要判断的留给 vibe_critic,生成 prompt 里只放
正向目标 + 少量带原因的禁令。**

判断标准:想往任何生成 prompt 里加一条新禁令时,先问能不能被正则抓 —— 能,
就放 prose_gate,prompt 省下这一条的注意力预算。

### 三档分级(改动时最容易搞错的地方)

| 档 | 判定 | 后果 |
|----|------|------|
| HARD | 命中即 fail | 走定点改写 → 下一轮免费复扫 |
| SOFT | 命中记标 | 不 fail,喂给 critic 当参考 |
| BATCH | 跨 cell 分布 | 单篇不判,只看占比 |

**第三档不是可选项。** 小红书有大量自己的方言 ——「家人们」「谁懂」「闭眼入」
「救了我的」。`foundation.md` 明确把这些列为**平台暗号词**、是真实感信号:

> 平台暗号词:「绝绝子」「真的会谢」「无语子」「家人们」「谢邀」「评评理」

这些词**单篇出现是人味,十篇都有才是流水线**。放进单篇硬禁 = 机审闸和
foundation.md 打架,而 foundation.md 是对的。所以它们只能进 BATCH 档,
按跨 cell 占比判(插入词 ≥60%、修辞模板 ≥40%)。

同一个词在不同层里是不同性质的东西 —— 这一点在移植任何外部规则表时最容易错。

### 为什么自建规则表而不是移植现成脚本

外部中文 AI 腔检测脚本的校准场景通常是知乎长文 / 公众号,和小红书种草差异很大。
直接移植至少三处误伤:

1. **冒号全禁** —— 小红书正文「成分:」「价格:」是常规写法,标题「XX:一定要看」
   更是平台原生句式。需要重写规则而不是加豁免开关
2. **家人们 / 姐妹们** —— 和 foundation.md 直接冲突(见上)
3. **谁懂 / 闭眼入 / 救了我的** —— 平台方言,单篇禁掉等于禁掉平台语言

所以只借**规则家族的思路**(翻案句 / 商业黑话 / 伪精确 / 跨篇指纹),
具体词表按小红书重新校准。

另有两处口径必须放宽:
- **伪精确**单位表只收时间和次数,不收克/毫升/% —— 那些是产品规格,属事实层
- **蓝词**走白名单豁免。蓝词必须原样重复(轮换同义词直接损伤搜索权重),
  它们出现在每一篇里是**正确行为**,不能算批量指纹

### 跨篇指纹是新能力,不是重复建设

已有的跨 cell 查重(`_find_cross_cell_duplicates` / batch_sampler 的 trigram
Jaccard)抓的是**词面重合** —— 换汤不换药。

prose_gate 的跨篇层抓的是**结构重合** —— 每篇开头都时空锚定、每篇都插一句
"说真的"、每篇结尾落在同一句式。词面重合度可以很低,读者一眼流水线。
这是**换药不换汤**。

两层并行,各管一半。

### 一处成本认知的更正

有一种说法是「机审前置能让 vibe_critic 和二审的调用量下降」。**这是错的** ——
两者都是批处理(整批 cell 一次调用),过滤掉几个 cell 只降输入 token,
不降调用次数。而且拦下的 cell 走 rewriter + 复扫,**可能反而增加轮次**。

所以 prose_gate 放在 critic **之后**做合并,而不是之前做过滤:借 critic 已经
产出的 failed 列表顺路把机械命中的 cell 送进 rewriter,不额外发起任何调用。
真正的省在下一轮 —— round 2+ 复扫是免费的,且能抓到 rewriter「把 A 指纹改成
B 指纹」这种机扫过了、读者没过的劣化。

### 单一来源

`quality_metrics.check_redlines` 是 `prose_gate.scan_text` 的薄封装。
两边各带一份黑名单必然漂移,而漂移的表现最难查:分数还在涨,闸门已经按新规矩
走了。与其靠人记着同步,不如让它只有一份。

v0.37.0 把最后一份私有副本也收了:`orchestrator._validate_prompt_cell` 的构建期
校验此前自带 11 条 `ai_cliches`,和这里的 15 条互有缺漏,现在直接读
`prose_gate.AI_CLICHE_BLACKLIST`(两份的并集,16 条)。

### SOFT 档去哪了(v0.37.0)

网感循环里对 critic 放过的 cell 跑的那次 `scan_text`,SOFT 结果以前写在矩阵 cell
的 `_prose_soft_flags` 上 —— 全仓没人读,却跟着 cell 进了 rewriter 输入、终审输入
和交付的矩阵,改写之后还是旧 demo 的标记。现在记在当轮 `critic_result["_prose_soft_flags"]`
({cell_id: [...]},和 `_gemini_arbitration` 同一处);出货那一版的 SOFT 另有
stage_log `prose_gate` 的 `per_cell.soft_flags`。**没有**按「喂给 critic 当参考」
的原意注入下一轮 critic 输入:那会改 severity,不是记录。

---

## 10. 判定服务(judge / Jev):影子阶段 (v0.37.0)

judge 是 BYWOOD 三仓共用的判定服务(独立仓库,Railway 单独一个服务):同一个 Jev
模型、同一套题库纪律、同一张账本(truth_vault.note_feature_answers)。本仓只放薄客户端
`pipeline/agents/judge_client.py`,影子阶段的纯函数在 `pipeline/jev_shadow.py`。
judge 仓 docs/00 #5 拍板:**二审先影子跑(记不分流),采样正文落库后再判;persona 第三路后做。**

### 开关与默认值(`pipeline/config.py`「判定服务 judge」一节)

| 阶段 | 开关(默认) | 题库 | 写账本 | 结果去哪 | 改不改产出 |
|------|-------------|------|--------|----------|-----------|
| 网感二审影子 | `ENABLE_JEV_CRITIC_SHADOW`(开) | `ssll_critic_v0.1` | 否 | `critic_result["_jev_arbitration"]` + stage_log `jev_critic_shadow` | 否 |

> v0.37.1:v4-flash 二审本身每轮另写一条 stage_log `vibe_arbitration`(原始结果、额外判 fail 的 cell、机审命中、Jev 一致率摘要),详情页 Tab 6 按时间顺序把它和 `jev_critic_shadow` 配回所属那一轮。`critic_result` 上挂的 `_gemini_arbitration` / `_jev_arbitration` / `_prose_soft_flags` 只在内存里和 `final_system.vibe_critic_result` 里,到不了那一轮 vibe_critic 的 stage_log(那条在 critic 返回前就写完了)。
| 采样正文 | `ENABLE_JEV_SAMPLE_SHADOW`(开) | `feature_questions_v0_1` + 按段 `human_feel_para_v0.1` | fq 是(`ssll_sample`),段级否 | stage_log `jev_sample_shadow` | 否 |
| 预埋评论 | `ENABLE_JEV_COMMENT_SHADOW`(开) | `comment_reader_v0.4` / `comment_thread_v0.3` | 否 | stage_log `jev_comment_shadow` | 否 |
| 预埋评论重生成 | `ENABLE_JEV_COMMENT_REGEN`(**关**) | 同上 + Moonshot 生成端 | 否 | 写回 `cell.comment_seeds`,stage_log `jev_comment_regen`(兼续跑标记) | **是** |
| 画像第三路 | `ENABLE_JEV_PERSONA_ROUTE`(**关**) | `ssll_critic_v0.1` 的 `persona_*` 三题 | 否 | 并进 `_persona_reactions`(`_source="jev"`),stage_log `jev_persona_route` | **是**:弱 cell 判据改成「至少两路否决」 |

**没配 `JUDGE_URL` = 全部零副作用**:不发请求、不写 stage_log、不往任何结果上挂字段、
连取消检查那次 DB 查询都不多发。两个会改产出的开关关着时,对应代码整段不执行,
画像模拟与 v0.36.1 逐字节一致。

### 二审影子为什么「只记不分流」

Jev 按 `multiplier_gate` 四项 + `template_test.still_holds` 出概率,state 装齐主 critic
判决时对照的锚点(`stop_trigger` / `reward_type` / `gap_direction` 来自 `_direction_index`,
`product_role` 来自 cell_plan,`advertising_stance` 来自 brief)—— Jev 偏字面,锚点不装
进去判的就不是同一件事。「任一 fail 即 fail、任一 weak 最多 borderline」这条规则
(`vibe_critic.md` 210-213)在影子里只用来算一个对照数(`gate_severity`),不回写任何
critic 结果;更不映射 strategic —— 那会触发策略升级回中书省重跑整轮,一次几分钱的
调用能触发全流水线最贵的重入。现行 v4-flash 二审的 payload 同样缺这些锚点,但**没有**
顺手补:它的结论直接进 failed,补锚点会改分流。

### 数据出境与账本

- 每个请求都带 `project = "ssll:<projects.id>"`、`category` 和 `published: false`。
  `category` 由 brief 的 `product_category` 翻成 TV 统一词表:先看
  `JUDGE_PROJECT_CATEGORY_OVERRIDES`(按项目手工指定),再按 `JUDGE_CATEGORY_RULES`
  (医药类;分不清处方 / 非处方的药品从严按「处方药」发)、`JUDGE_CATEGORY_CLOSED_MARKERS`
  (沾医疗但认不出的:医用 / 特医 / 医美…)、`JUDGE_CATEGORY_GENERAL_RULES`(非医药类)。
  **认不出就不发**(v0.37.2):`_jev_judge` 直接记 `category_unrecognized` 跳过 ——
  三省六部的项目在服务端没登记,处方药只能靠 `category` 拦,认不出的品类(「司美格鲁肽」
  「降糖针」)不能当安全的放出去。处方药项目的未发布稿服务端 403,本仓记
  `policy_blocked` 跳过,不重试。要放行某个项目,在 judge 仓 `config/data_policy.yaml`
  里写 `ssll:<uuid>`。
- 终审之后的辅助开销(批量采样、判定服务影子)只进进程内的 run 总账,收尾写 run 状态时
  一并写 `pipeline_runs.total_tokens / total_cost_usd`(v0.37.2;之前后面没有 BaseAgent
  阶段替它们落库)。
- 只有采样稿的 fq 判定写账本:`subject_type = ssll_sample`,
  `subject_id = <run_id>:<cell_id>:<代际>:<seed>`。**代际**(一次采样一个 UTC 时间戳)
  是必需的:修订重跑会删掉 `batch_sampling` 日志、按同一组 seed 重采,不带代际新旧两批
  正文就对应同一个账本主键,后写覆盖先写。`batch_sampling` 的 `per_sample[i].body`
  现在存正文全文,`subject_id` 也一起落在那条 stage_log 里,能回查到「那一篇」。
- demo / 画像 / 预埋评论的 id 不是 TV 的笔记或评论 id,一律 `write=false`。预埋评论用
  `subject_type = comment`,靠 `published: false` 让出境策略按未发布稿处理。

### 续跑与失效

新 stage_log 名都登记在 `REFINEMENT_MARKER_ANCHORS`(`orchestrator.JEV_STAGE_LOG_NAMES`,
测试钉着):二审影子、评论重生成锚 `vibe_critic`,画像第三路锚 `persona_simulator`,
采样 / 评论影子锚 `chancellery_final`。续跑时已完成的采样 / 评论影子直接跳过;评论
重生成的标记会把上次写回的评论重新贴回(从快照恢复的矩阵里是重生成之前的评论)。

### SQL

```sql
-- 二审影子:每轮 Jev 与主 critic 在门槛层判决上的一致率
SELECT sl.created_at,
       sl.output_data->>'round' AS round,
       sl.output_data->'gate_severity_agreement_with_main' AS gate_agree,
       sl.output_data->'agreement_with_main' AS per_item
FROM stage_logs sl
WHERE sl.stage_name = 'jev_critic_shadow' AND sl.status = 'completed'
ORDER BY sl.created_at DESC LIMIT 30;

-- 采样影子:每个 cell 的 fq 答案分布(5 篇 × 20 题)
SELECT sl.run_id, c->>'cell_id' AS cell, c->'fq_distribution' AS dist
FROM stage_logs sl, LATERAL jsonb_array_elements(sl.output_data->'cells') c
WHERE sl.stage_name = 'jev_sample_shadow'
ORDER BY sl.created_at DESC LIMIT 50;
```

---

## 后续 sprint backlog (audit 2026-05-22 未实施项)

| ID | 主题 | 工时 | 触发条件 |
|----|------|------|---------|
| R-018 | daemon thread → 持久 job worker (jobs 表 + 独立 worker 进程) | 1-2 周 | 浏览器关闭丢任务变成日常痛点 |
| R-028 | cell-level resume (stage_logs 加 cell_status JSONB) | 1-2 天 + schema | matrix > 30 cells 时 resume 成本显著 |
| R-026.2 | 把 BaseAgent.run() 重试迁到 llm_retry.py | 1-2 天 | 第三个非 Anthropic backend / 全局重试可观测要求 |
