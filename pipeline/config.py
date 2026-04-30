"""Pipeline configuration — model assignments, retry strategy, constants."""

# ── Version ────────────────────────────────────────────────────────────────
# Bump on every meaningful release. Format: vMAJOR.MINOR.PATCH (date) — feature
VERSION = "v0.30.1"
VERSION_DATE = "2026-04-19"
VERSION_NOTES = (
    "v0.30.1 输出中心简化 + 修订按钮智能分流提示: 输出中心顶部新增"
    "『成品提示词清单』主区,直接列 N 个不重复 prompt 的完整内容 + "
    "示例文稿(代替原来要翻平台 tab + 多层 expander 的繁琐结构)。"
    "应用修订按钮文字按实际行为校正:扫 mandatory_revisions 文本里的 "
    "D\\d+ 和全局关键词,提前告诉用户『只会重建 D5』vs『会重跑整个工部』。"
    "v0.30.0 历史: 多 vendor 路由 + 高质量模型预设(premium_multi_vendor): "
    "(1) DeepSeek 走官方 anthropic-compat 端点(api.deepseek.com/anthropic),"
    "新增 DEEPSEEK_API_KEY secret + per-model 路由器 _get_client_for_model;"
    "(2) GPT 走 tdyun 中转的 anthropic-compat 路径(model='gpt-5.5'),"
    "thinking 参数对 GPT 强制屏蔽(避免 OpenAI 后端 400);"
    "(3) 各 stage 模型映射写入 PREMIUM_MULTI_VENDOR_MAP(代码层),"
    "user 在 secrets.toml 删 model_overrides 即可启用;"
    "(4) 中书省 ↔ 门下省 故意异厂家(Claude vs GPT)避免辩论同色彩。"
    "v0.29.12 历史: (1) STAGE_MAX_TOKENS["
    "ministry_personnel] 20K→32K,多画像 × authenticity_card 字段长容易"
    "撞上限导致响应截断;(2) _try_repair_truncated_json 的 "
    "cut_points[-300:] 硬限放开——13K+ 响应最后 300 个 cut point 常常"
    "都卡在深层嵌套里,每个 candidate 都不合法,扫全部才能找到有效"
    "cut;(3) JSON 提取失败错误消息改成 first 200 + last 200 双端"
    "预览,方便判断是整段不是 JSON 还是只是尾部截断。"
    "功能同 v0.29.11(画像模拟接入反馈链): 之前 persona_simulator 只写 "
    "_persona_reactions 给 UI 显示,不参与任何决策,跑了等于白烧 token。"
    "现在每条 cell 扫 3 个画像的 action,全 skip 的 cell 追加进 "
    "strategic_warnings,和 consumer_simulation 走同一条告警通道 —— "
    "UI 红色警告 + 如果 ENABLE_STRATEGIC_ESCALATION 开启会触发 "
    "secretariat 修订 direction 的 stop_trigger/reward_type。"
    "不依赖 summary.weak_cells(模型有时给 direction_id 而非 cell_id),"
    "直接从 personas[*].reactions 逐 cell 统计更稳。"
    "功能同 v0.29.9(流水线详情页可观测性大升级): (1) 新增『中间精炼』tab 展示 "
    "叙事导演 / 红蓝精炼 / 画像模拟 三个阶段,之前 UI 没位置、"
    "图标染色点进去看不到内容; (2) 网感 tab 补上 叙事结构重写 "
    "(structural_rewriter) 的 per-cell 摘要; (3) 太子 tab 顶部新增 "
    "『📎 接收到的参考文件』清单,按 txt/md/pdf/docx/图片 统一识别 "
    "[参考文件: name] 包装 + 从 body 推断 kind/status,每个文件"
    "一行状态图标 + 字数 + 预览,不再需要翻几百行 base64 确认"
    "收到没;(4) free_text 显示一律折叠 BASE64_IMAGE 块和裸 "
    "base64 串,用『📎 已折叠 · N 字符』代替,实际喂 agent 的是"
    "完整原文不受影响。"
)

# ── Model assignments per stage ────────────────────────────────────────────
# All stages use the same Claude model family. Whether thinking is enabled
# is controlled per-call via the standard Anthropic API
# `thinking={"type":"enabled","budget_tokens":N}` parameter — see
# THINKING_STAGES below + agents/__init__.py::_call_claude.
#
# Why one model name in most presets: Anthropic native, modern relay
# proxies, and Vertex all accept the standard JSON `thinking` parameter.
# The old convention of using a `-thinking` suffix in the model name was
# a relay-specific routing hack. Keeping a single model name also makes
# prompt caching cache across thinking and non-thinking stages (same
# system prompt + same model = same cache key).
#
# ── MODEL_PRESET options ─────────────────────────────────────────────
# Change this string to switch strategy/content model split without
# editing MODELS directly:
#
#   "all_opus"      (default, current behavior) — every stage on Opus.
#                   Deepest reasoning; most expensive; Opus has a slightly
#                   more "精致/端正" voice that some reviewers call AI-toned.
#
#   "content_sonnet" — Strategy + review stages stay on Opus; content-
#                   producing stages (works_builder, vibe_critic,
#                   vibe_rewriter) switch to Sonnet 4.6. Rationale:
#                   Sonnet's demo output tends to read more "松弛/人味",
#                   and critic-style tasks benefit from a lighter tone.
#                   Cheaper + faster on the content-heavy stages.
#                   RECOMMENDED for experimentation if output still feels
#                   AI-toned after vibe rewriter.
#
#   "all_sonnet"   — Everything on Sonnet. Cheapest, fastest, but
#                   reasoning-heavy stages (secretariat, chancellery,
#                   chancellery_final) may produce lower-quality plans.
#                   Mostly useful for dev loops / cost-tight pilots.

OPUS_MODEL = "claude-opus-4-7"
# 用于 all_sonnet preset 以及 planning / 结构化任务(planning 阶段需要
# Sonnet 4-6 的稳定 JSON 输出能力)。
SONNET_MODEL = "claude-sonnet-4-6"
# v0.28.0: 内容写作专用模型。老版本 Sonnet(3.7)在纯写作任务上对齐
# 痕迹更少,常被反馈"网感更强 / 更像真人",所以把 content 角色单独
# 绑到 3.7 而不是跟 planning 共用 Sonnet 4.6。
# 注意:实际模型名可能因 relay / Vertex 不同而需要调整——比如
# Anthropic 官方是 "claude-3-7-sonnet-20250219"。如果你的接入点不
# 认识 "claude-sonnet-3-7",可在 secrets.toml 的
# [claude_relay_presets.X.model_overrides] 里覆盖:
#   ministry_works_builder = "claude-3-7-sonnet-latest"
#   vibe_rewriter = "claude-3-7-sonnet-latest"
#   ...
SONNET_CONTENT_MODEL = "claude-sonnet-3-7"

# v0.30.0: 在代码层面直接锁定每个 stage 用哪个模型(高质量配置),用户
# 不必再在 secrets.toml 里维护 model_overrides。可选 preset:
#
#   premium_multi_vendor(v0.30.0 默认,推荐) — 不限成本求最优组合,
#     - 推理 / 长上下文 / 中文锐度都拉满
#     - 中书省 vs 门下省 故意用不同厂家(Claude vs GPT)避免辩论同色彩
#     - 内容生成保留 Sonnet 3.7(短中文网感实战最强)
#     - 兵部 / chancellery 用 GPT 提供异色彩对抗
#     - 需要 secrets.toml 同时配 Claude 中转 + (可选)DEEPSEEK_API_KEY
#
#   content_sonnet(v0.29.x 历史默认) — 保留兼容
#   all_opus / all_sonnet — 单厂家全跑,降级方案
MODEL_PRESET = "premium_multi_vendor"

# v0.30.0: 各阶段精确模型映射 — 写在代码里防止 secrets.toml 误覆盖。
# 用户实际中转里的模型 ID,要和 tdyun-style anthropic-compat relay 对齐。
# 注:thinking 模式由模型名后缀 `-thinking` 控制(tdyun 约定),不是
# 通过 thinking JSON 参数传(老路径)。
PREMIUM_MULTI_VENDOR_MAP: dict[str, str] = {
    # ── 策略 / 推理核心 — Opus 4.7 thinking ──
    "crown_prince": "claude-opus-4-7-thinking",          # 长上下文 + 文件留存
    "secretariat": "claude-opus-4-7-thinking",            # 中书省发言,锐利 insight
    "chancellery_final": "claude-opus-4-7-thinking",      # 终审 holistic 判断
    "ministry_works": "claude-opus-4-7-thinking",         # 工部架构,搭整脊柱
    "narrative_director": "claude-opus-4-7-thinking",     # 跨 cell 一致性诊断
    "vibe_critic": "claude-opus-4-7-thinking",            # judge 必须锐
    "structural_rewriter": "claude-opus-4-7-thinking",    # 身份/缺口手术,要推理
    # ── 异厂家辩论(Claude vs GPT)— 中书省 ↔ 门下省 ──
    "chancellery": "gpt-5.5",                             # 门下省,异色彩 critic
    "ministry_war": "gpt-5.5",                            # 兵部,刁钻竞争设计
    # ── 结构化派发 / 五部 — Opus 4.6 稳态 ──
    "dispatcher": "claude-opus-4-6",
    "ministry_personnel": "claude-opus-4-7",              # 画像创作偏 Opus 4.7
    "ministry_revenue": "claude-opus-4-6",
    "ministry_rites": "claude-opus-4-6",
    "ministry_justice": "claude-opus-4-6-thinking",       # 合规要严谨
    "ministry_works_cell_planner": "claude-opus-4-6",
    # ── 内容生成 — Sonnet 3.7(短中文网感实战最佳) ──
    "ministry_works_builder": "claude-3-7-sonnet-20250219-thinking",
    "vibe_rewriter": "claude-3-7-sonnet-20250219-thinking",
    "red_blue_refiner": "claude-3-7-sonnet-20250219-thinking",
    "persona_simulator": "claude-3-7-sonnet-20250219-thinking",
}

_STAGE_ROLES = {
    # Strategy / review: needs reasoning depth
    "crown_prince": "strategy",
    "secretariat": "strategy",
    "chancellery": "strategy",
    "chancellery_final": "strategy",
    "ministry_works": "strategy",
    # Structured planning: Opus preferred for stability
    "dispatcher": "planning",
    "ministry_personnel": "planning",
    "ministry_revenue": "planning",
    "ministry_rites": "planning",
    "ministry_war": "planning",
    "ministry_justice": "planning",
    "ministry_works_cell_planner": "planning",
    # Cross-cell coherence: needs reasoning (sees whole matrix)
    "narrative_director": "strategy",
    # Content generation + taste judgment: voice quality matters
    "ministry_works_builder": "content",
    "red_blue_refiner": "content",     # needs natural language feel
    "persona_simulator": "content",    # simulates real humans
    "vibe_critic": "content",
    "vibe_rewriter": "content",
    # v0.29.0: 叙事结构重写者 — 和 vibe_rewriter 同角色(内容写作),
    # 走 content 池(Sonnet 3.7 网感)。
    "structural_rewriter": "content",
}


def _resolve_models(preset: str) -> dict[str, str]:
    """Assemble the MODELS dict from role tags + preset. Returning a dict
    keeps consumers (logging, cost accounting, settings UI) unchanged.

    角色 → 模型映射(按 preset):

    content_sonnet(默认):
      - content 角色(builder / vibe_critic / vibe_rewriter / red_blue /
        persona_simulator) → SONNET_CONTENT_MODEL(默认 Sonnet 3.7,写作
        人味最重)
      - 其他所有角色(strategy + planning + cross-cell coherence) →
        OPUS_MODEL(默认 Opus 4.7,深推理)

    all_sonnet:
      - content 角色 → SONNET_CONTENT_MODEL(Sonnet 3.7)
      - 其他角色 → SONNET_MODEL(Sonnet 4.6,稳定 JSON 输出)

    all_opus:
      - 全部 → OPUS_MODEL

    注:planning 角色(尚书省 / 六部 / 格子规划)在 content_sonnet 下用 Opus
    (不是 Sonnet),因为 "Structured planning: Opus preferred for stability"
    ——结构化派发需要稳定的指令理解,降到 Sonnet 会偶尔漏字段。如果要
    planning 走 Sonnet 省钱,改用 all_sonnet preset。
    """
    if preset == "all_sonnet":
        # 全 Sonnet 模式:content 用 3.7 保网感,其他用 4.6 保结构
        return {
            k: (SONNET_CONTENT_MODEL if role == "content" else SONNET_MODEL)
            for k, role in _STAGE_ROLES.items()
        }
    if preset == "content_sonnet":
        return {
            k: (SONNET_CONTENT_MODEL if role == "content" else OPUS_MODEL)
            for k, role in _STAGE_ROLES.items()
        }
    if preset == "premium_multi_vendor":
        # v0.30.0:每个 stage 都从 PREMIUM_MULTI_VENDOR_MAP 直接拿模型名;
        # 没在 map 里的 stage(罕见,通常是新增的 stage 还没补)fallback 到
        # OPUS_MODEL,既保证能跑也提示要补。
        return {
            k: PREMIUM_MULTI_VENDOR_MAP.get(k, OPUS_MODEL)
            for k in _STAGE_ROLES
        }
    # Default / fallback: all_opus
    return {k: OPUS_MODEL for k in _STAGE_ROLES}


MODELS: dict[str, str] = _resolve_models(MODEL_PRESET)

# ── Retry & timeout ────────────────────────────────────────────────────────

MAX_RETRIES = 2
# Exponential backoff base. Delay for attempt N is
# RETRY_BASE_DELAY_SECONDS * 2**N (so 3s after attempt 0, 6s after attempt
# 1, 12s after attempt 2, ...). With MAX_RETRIES=2 this matches the old
# linear 3,6 sequence; the formula is kept exponential so bumping
# MAX_RETRIES doesn't silently change the retry curve.
RETRY_BASE_DELAY_SECONDS = 3

# ── Clarification ─────────────────────────────────────────────────────────
# How long the pipeline waits for the user to answer a clarification
# request before giving up. 1 hour is generous for humans to come back
# from another tab / lunch, short enough that a truly-abandoned run gets
# cleaned up eventually.
CLARIFICATION_TIMEOUT_SECONDS = 3600
# Poll interval while waiting for user response.
CLARIFICATION_POLL_SECONDS = 5
# Max clarification rounds per agent — if the model keeps asking, force
# continue with whatever partial output it gave instead of looping forever.
MAX_CLARIFICATION_PER_AGENT = 2

# ── Platform demo length bounds ──────────────────────────────────────────
# (min_chars, max_chars) for demo_output validation per platform. Keys are
# matched via substring+case-insensitive against the cell's platform field,
# so both Chinese and romanized variants resolve to the same range. Values
# are approximate — the validator allows 1.5× the max as a hard ceiling.
PLATFORM_DEMO_LENGTH_RANGES: dict[str, tuple[int, int]] = {
    "小红书": (200, 1000),
    "xiaohongshu": (200, 1000),
    "抖音": (50, 500),
    "douyin": (50, 500),
    "b站": (150, 600),
    "bilibili": (150, 600),
    "知乎": (300, 2000),
    "zhihu": (300, 2000),
    "微博": (20, 200),
    "weibo": (20, 200),
}
# Fallback when the platform isn't recognized.
PLATFORM_DEMO_LENGTH_DEFAULT: tuple[int, int] = (50, 2000)

# ── Chancellery review ─────────────────────────────────────────────────────

MAX_CHANCELLERY_REJECTIONS = 2  # plan_review: force pass on round 3 (legacy, used by non-debate path)

# ── Strategy Debate ──────────────────────────────────────────────────────
# Max turns in the secretariat ↔ chancellery multi-turn debate.
# Secretariat speaks on even turns, chancellery on odd. So MAX_DEBATE_TURNS=8
# means 4 exchanges (each agent speaks 4 times). Chancellery can approve
# at any odd turn to end early. Last chancellery turn is force-approve.
MAX_DEBATE_TURNS = 8

# final_review (工部产出的 prompt_matrix) 的轮次上限。第一次跑流水线 = round 1；
# 用户每点一次「应用修订意见并重跑」round +1。超过 MAX_FINAL_REJECTIONS 后强制
# 放行，并在 suggestions 里打风险注。防止终审无限驳回工部造成死循环。
MAX_FINAL_REJECTIONS = 3

# ── Token limits ───────────────────────────────────────────────────────────
# max_tokens must accommodate (thinking_budget + actual_output) for thinking stages.

MAX_TOKENS_DEFAULT = 16000
MAX_TOKENS_STRATEGY = 32000  # strategy/review stages need most room

STAGE_MAX_TOKENS: dict[str, int] = {
    "crown_prince": MAX_TOKENS_STRATEGY,
    "secretariat": MAX_TOKENS_STRATEGY,
    "chancellery": MAX_TOKENS_STRATEGY,
    "dispatcher": 20000,
    # v0.29.12: 吏部经常超 20K(多画像 × authenticity_card 字段长),撞
    # max_tokens 截断产出破损 JSON。修复 JSON 重建能救大多数,但直接
    # 给足上限从源头减少截断。
    "ministry_personnel": 32000,
    "ministry_revenue": 20000,
    "ministry_rites": 20000,
    "ministry_war": 20000,
    "ministry_justice": 20000,
    "ministry_works": MAX_TOKENS_STRATEGY,
    "ministry_works_cell_planner": 20000,
    "ministry_works_builder": 32000,
    "vibe_critic": 20000,
    "vibe_rewriter": 24000,
    "structural_rewriter": 24000,  # v0.29.0: 和 vibe_rewriter 一致
    "chancellery_final": MAX_TOKENS_STRATEGY,
}

# ── Matrix Execution ─────────────────────────────────────────────────────
MATRIX_BATCH_CONCURRENCY = 3    # parallel builder calls
MATRIX_CELLS_PER_BATCH = 1      # one cell per call (safest for JSON structure)

# ── Cell Planner Batching ────────────────────────────────────────────────
CELL_PLANNER_BATCH_SIZE = 5     # cells per cell-planner call
CELL_PLANNER_CONCURRENCY = 3    # parallel cell-planner calls

# ── Extended Thinking ─────────────────────────────────────────────────────
# 5 strategy/review stages use extended thinking (budget_tokens on relay,
# adaptive on Vertex 4.6). Execution stages skip thinking for speed.

THINKING_STAGES: frozenset[str] = frozenset({
    "crown_prince",
    "secretariat",
    "chancellery",
    "ministry_works",
    "chancellery_final",
})

THINKING_BUDGET_TOKENS = 10000  # for relay mode (budget_tokens)

# ── Rate Limiting ─────────────────────────────────────────────────────────
# Sliding-window rate limiter. The active window respects two constraints
# simultaneously:
#
#   1. RPM cap (sustained):    at most CLAUDE_RPM_LIMIT call STARTS per
#                              rolling 60-second window. When the window
#                              fills, the next call sleeps until the
#                              oldest entry ages out.
#   2. Concurrency cap (peak): at most CLAUDE_MAX_CONCURRENT calls in
#                              flight at the same instant.
#
# Tune these to your backend's published limits. Defaults are calibrated
# for a typical paid relay quota (15 RPM / 16 concurrent — actually
# observed on a real account). Set CLAUDE_RPM_LIMIT to 0 to disable the
# rate cap entirely (e.g. on Vertex with high project quota).
#
# Vertex AI mode bypasses this limiter entirely — Vertex enforces quota
# server-side and returns 429 we'd just retry into. See
# agents/__init__.py::_get_active_limiter.
CLAUDE_RPM_LIMIT = 15
CLAUDE_MAX_CONCURRENT = 16

# ── Per-run budget ───────────────────────────────────────────────────────
# Hard ceiling on combined input + output tokens accumulated within a
# single pipeline run. Once exceeded, the next agent call raises
# RunBudgetExceededError and the orchestrator marks the run as failed.
# Safety net against runaway retry loops — 14 stages × worst-case retries
# × thinking budgets can compound quickly without a cap.
#
# Opus at $15/M input + $75/M output → 1M tokens ≈ $45 ceiling per run
# (assuming roughly balanced in/out). Tune to your cost tolerance.
MAX_TOKENS_PER_RUN = 2_000_000

# ── Gemini auxiliary assist (second-opinion critic + structure reviewer) ──
# Independent of the primary Claude backend. When configured, Gemini:
#   1. Re-evaluates cells Claude's vibe_critic passed (B: 分歧仲裁).
#      If Gemini says fail, the cell is sent back to vibe_rewriter.
#      Use case: catch AI-tone outputs Claude's critic gives face-saving
#      borderline → pass scores to.
#   2. Audits structure completeness of every built prompt_cell (5 pools,
#      persona integration, compliance block, keyword list). Output is
#      appended to _revision_directives as advisory notes.
#
# Failure mode: advisory-only. Gemini call errors log a warning and the
# pipeline proceeds with Claude-only verdicts. Never blocks a run.
#
# Auth: Vertex Express API key (the `?key=${API_KEY}` variant — see
# https://cloud.google.com/vertex-ai/docs/general/vertex-express).
# Secrets.toml field: VERTEX_EXPRESS_API_KEY.
ENABLE_GEMINI_ASSIST = True

# Model identifier. Must be in your Vertex Express account's accessible
# model list — use the "📋 列出可用 Gemini 模型" button on the Settings
# page to see exactly what your key can call.
#
# NOTE: Google uses DOTS for decimal version numbers (gemini-2.5-pro,
# gemini-3.1-pro-preview), not dashes. `gemini-3-1-...` is wrong.
#
# Common picks:
#   - gemini-3.1-pro-preview        (latest Gemini 3.1 Pro preview)
#   - gemini-3.1-pro-preview-customtools  (same + tool-use features)
#   - gemini-3-pro-preview          (earlier Gemini 3 Pro preview)
#   - gemini-2.5-pro                (stable, widely available)
#   - gemini-2.5-flash              (cheapest, fine for critic role)
GEMINI_MODEL = "gemini-3.1-pro-preview"

# Trend scout — when True, Gemini runs a live Google Search
# (site:xiaohongshu.com) in two places:
#   - PRE: before secretariat, to enrich the brief with real current
#          post samples (titles + snippets) so strategy is calibrated
#          against concrete examples, not abstract assumptions.
#   - POST: after chancellery_final, per direction, for side-by-side
#           comparison with our produced demos. Advisory, non-blocking.
# Costs: Google Search grounding is billed separately by Google
# (~$35 / 1000 queries). PRE is 1 query/run; POST is 1 query per
# direction (~5-8/run). Budget ~$0.30 extra per full run.
#
# IMPORTANT CONTRACT — scout output is forced to be RAW POSTS ONLY,
# never trend analysis. See pipeline/prompts/gemini_trend_scout.md +
# pipeline/agents/gemini_trend_scout.py _FORBIDDEN_SUMMARY_KEYS.
ENABLE_GEMINI_TREND_SCOUT_PRE = False
ENABLE_GEMINI_TREND_SCOUT_POST = False
# How many posts to ask the scout to pull per invocation. Each post is
# ~150 chars in the output, so 10 is a reasonable default — gives
# secretariat meaningful calibration without ballooning the prompt.
GEMINI_TREND_SCOUT_TARGET_COUNT = 10

# Max output tokens per Gemini call. 16K handles 6-cell structure
# reviews without truncation. Grounding calls auto-bump to 24K (see
# gemini_client.py). Gemini 3.x supports up to 64K output, but 16K
# is a good default ceiling — our critic/reviewer/scout outputs are
# typically 2-8K so the model will stop early anyway.
GEMINI_MAX_OUTPUT_TOKENS = 16384

# Rough per-1M-token prices (USD) for cost accounting. Update when
# Google publishes final pricing for the chosen model. These values are
# intentionally on the high side so reported cost errs toward "expensive"
# rather than "surprise bill".
GEMINI_COST_PER_1M_INPUT = 1.25
GEMINI_COST_PER_1M_OUTPUT = 10.0


# ── Prompt Caching ────────────────────────────────────────────────────────
# When True, the system prompt is sent with cache_control={"type":"ephemeral"}
# so Anthropic caches it for ~5 minutes. Re-use across retries/batches hits
# the cache and pays ~10% input-token rate for the system portion. Requires
# system prompt to be ≥ 1024 tokens (small prompts silently don't cache).
#
# Vertex AI: native support.
# Anthropic direct: native support.
# Relay proxies: depends on the relay. Most pass the field through; some
# drop it (cache just misses, no error). If your relay 400s on it, set this
# to False.
ENABLE_PROMPT_CACHING = True

# ── Cost tracking (per 1M tokens, approximate) ────────────────────────────

COST_PER_1M_INPUT: dict[str, float] = {
    "claude-opus-4-7": 15.0,
    "claude-opus-4-6": 15.0,
    "claude-opus-4-1": 5.0,
    "claude-sonnet-4-6": 3.0,
    "claude-sonnet-3-7": 3.0,  # Sonnet 3.7 价格与 Sonnet 4.x 同量级
    # Anthropic 官方名兼容(如果 relay 要求用全名)
    "claude-3-7-sonnet-20250219": 3.0,
    "claude-3-7-sonnet-latest": 3.0,
}

COST_PER_1M_OUTPUT: dict[str, float] = {
    "claude-opus-4-7": 75.0,
    "claude-opus-4-6": 75.0,
    "claude-opus-4-1": 25.0,
    "claude-sonnet-4-6": 15.0,
    "claude-sonnet-3-7": 15.0,
    "claude-3-7-sonnet-20250219": 15.0,
    "claude-3-7-sonnet-latest": 15.0,
}

# ── Defaults ──────────────────────────────────────────────────────────────

DEFAULT_PLATFORM = "小红书"

# ── Vibe loop parameters ──────────────────────────────────────────────────
VIBE_LOOP_HARD_CAP = 3       # absolute max iterations
VIBE_LOOP_INITIAL_CAP = 2    # start with this many rounds
VIBE_LOOP_ESCALATE_THRESHOLD = 0.30  # failure rate to unlock extra round

# v0.29.0: critic-rewriter 责任分流 feature flag
# 打开时:按 critic 输出的 root_cause_kind 把 fail 的 cell 分流到
#   - structural_rewriter(身份错位 / 缺口方向错)
#   - vibe_rewriter(表层钩子弱 / 模板性 fail)
#   - strategic_warnings(策略层错配,rewriter 改不了)
# 关闭时:全部塞给 vibe_rewriter(v0.28 及之前的行为),作为稳妥兜底。
ENABLE_STRUCTURAL_REWRITER = True

# v0.29.1: 策略层自动升级(C.2.1)— vibe_loop 结束后若仍有 strategic_warnings,
# 自动回 secretariat 修订受影响的 direction(更新 stop_trigger / reward_type /
# role_embodiment 等锚点),然后再跑一次 vibe_loop 让 critic + rewriter
# 用新锚点重新判决。关闭时保持 v0.29.0 行为(只写 warnings 由用户人工介入)。
ENABLE_STRATEGIC_ESCALATION = True
# 硬上限:策略层循环的最大轮数。默认 1 — 只允许一次自动升级,避免 direction
# 来回摆动陷入死循环。超过后 strategic_warnings 仍然会写到 final_system
# 让用户人工处理。
STRATEGIC_LOOP_MAX_ITERATIONS = 1

# v0.29.1: 消费者模拟(C.2.2)— 在 vibe_loop 结束后、终审前,让
# persona_simulator 以 stop_trigger 描述的具体目标用户身份对每个 cell
# 做 stop / scroll 二元判决,作为 interest_align 的第二层校验。结果存到
# final_system._consumer_simulation,UI 显示;cell 若被目标用户 scroll,会
# 追加进 strategic_warnings 走人工审查通道。
ENABLE_CONSUMER_SIMULATION = True

# ── Advisory stage concurrency ────────────────────────────────────────────
RED_BLUE_CONCURRENCY = 3
TREND_SCOUT_POST_CONCURRENCY = 3

# ── UI polling ─────────────────────────────────────────────────────────────

POLL_INTERVAL_SECONDS = 3

# ── Stage ordering (for display) ──────────────────────────────────────────

PIPELINE_STAGES = [
    ("crown_prince", "太子", "📋"),
    # Advisory-only (Gemini). Skipped if user didn't paste URLs on
    # page 2 OR if Gemini isn't configured. Fetches user-specified
    # xiaohongshu post URLs via url_context — higher-signal than
    # keyword search because the user directly picked the references.
    ("gemini_reference_analyzer", "参考帖子·Gemini", "🔗"),
    # Advisory-only (Gemini). Skipped if Gemini isn't configured.
    # Pulls real current Xiaohongshu post samples via Google Search
    # (site:xiaohongshu.com), injects raw titles + snippets into
    # brief._trend_intel so secretariat's strategy is calibrated
    # against concrete current examples, not abstract guesses.
    ("gemini_trend_scout_pre", "趋势取样·Gemini", "🔭"),
    ("secretariat", "中书省", "📜"),
    ("chancellery", "门下省", "🔍"),
    ("dispatcher", "尚书省", "📋"),
    ("ministry_personnel", "吏部", "👤"),
    ("ministry_revenue", "户部", "🔑"),
    ("ministry_rites", "礼部", "🎭"),
    ("ministry_war", "兵部", "⚔️"),
    ("ministry_justice", "刑部", "⚖️"),
    ("ministry_works", "工部·架构", "🏗️"),
    ("ministry_works_cell_planner", "工部·格子规划", "📐"),
    ("ministry_works_builder", "工部·构建", "🔨"),
    ("narrative_director", "叙事导演", "🎬"),
    ("red_blue_refiner", "红蓝精炼", "⚔️"),
    ("persona_simulator", "画像模拟", "👥"),
    ("ministry_works_structure_review", "结构审·Gemini", "🔎"),
    ("vibe_critic", "网感复检", "🎯"),
    # v0.29.3: 补展示 — 这两个阶段其实一直在跑也各自记 stage_log,
    # 但 PIPELINE_STAGES 漏了,导致 Settings 页面"模型配置"看不到它们
    # 用的是哪个模型(用户手动在 secrets 里配了 override 也找不到对应行)。
    ("vibe_rewriter", "网感重写", "✏️"),
    ("structural_rewriter", "叙事结构重写", "🧱"),
    ("chancellery_final", "终审", "✅"),
]
