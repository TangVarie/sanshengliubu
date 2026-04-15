"""Pipeline configuration — model assignments, retry strategy, constants."""

# ── Version ────────────────────────────────────────────────────────────────
# Bump on every meaningful release. Format: vMAJOR.MINOR.PATCH (date) — feature
VERSION = "v0.13.0"
VERSION_DATE = "2026-04-14"
VERSION_NOTES = "适配新中转站：JSON thinking 参数 + 滑动窗口限速（15 RPM / 16 并发）"

# ── Model assignments per stage ────────────────────────────────────────────
# All stages use the same Claude Opus model name. Whether thinking is
# enabled is controlled per-call via the standard Anthropic API
# `thinking={"type":"enabled","budget_tokens":N}` parameter — see
# THINKING_STAGES below + agents/__init__.py::_call_claude.
#
# Why one model name everywhere: Anthropic native, modern relay proxies,
# and Vertex all accept the standard JSON `thinking` parameter. The old
# convention of using a `-thinking` suffix in the model name was a
# relay-specific routing hack; new relays + native API don't need it.
# Keeping a single model name makes prompt caching cache across thinking
# and non-thinking stages (same system prompt, same model = same cache key).

OPUS_MODEL = "claude-opus-4-6"

MODELS: dict[str, str] = {
    "crown_prince": OPUS_MODEL,
    "secretariat": OPUS_MODEL,
    "chancellery": OPUS_MODEL,
    "ministry_works": OPUS_MODEL,
    "chancellery_final": OPUS_MODEL,
    "dispatcher": OPUS_MODEL,
    "ministry_personnel": OPUS_MODEL,
    "ministry_revenue": OPUS_MODEL,
    "ministry_rites": OPUS_MODEL,
    "ministry_war": OPUS_MODEL,
    "ministry_justice": OPUS_MODEL,
    "ministry_works_cell_planner": OPUS_MODEL,
    "ministry_works_builder": OPUS_MODEL,
    "vibe_critic": OPUS_MODEL,
    "vibe_rewriter": OPUS_MODEL,
}

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

MAX_CHANCELLERY_REJECTIONS = 2  # plan_review: force pass on round 3

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
    "ministry_personnel": 20000,
    "ministry_revenue": 20000,
    "ministry_rites": 20000,
    "ministry_war": 20000,
    "ministry_justice": 20000,
    "ministry_works": MAX_TOKENS_STRATEGY,
    "ministry_works_cell_planner": 20000,
    "ministry_works_builder": 32000,
    "vibe_critic": 20000,
    "vibe_rewriter": 24000,
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

# Model identifier. The value below is what the user requested; if Vertex
# returns 404 model-not-found, replace with an ID you can actually call
# (e.g. "gemini-2.5-pro" / "gemini-2.5-flash" / "gemini-2.5-flash-lite").
# Check available models at
# https://cloud.google.com/vertex-ai/generative-ai/docs/models
GEMINI_MODEL = "gemini-3-1-pro-preview"

# Max output tokens per Gemini call. Gemini's output cap is per-model;
# 8K is safe across 2.5 Pro and most preview tiers.
GEMINI_MAX_OUTPUT_TOKENS = 8192

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
    "claude-opus-4-6": 15.0,
    "claude-opus-4-6-thinking": 15.0,
    "claude-opus-4-1": 5.0,
    "claude-sonnet-4-6": 3.0,
}

COST_PER_1M_OUTPUT: dict[str, float] = {
    "claude-opus-4-6": 75.0,
    "claude-opus-4-6-thinking": 75.0,
    "claude-opus-4-1": 25.0,
    "claude-sonnet-4-6": 15.0,
}

# ── UI polling ─────────────────────────────────────────────────────────────

POLL_INTERVAL_SECONDS = 3

# ── Stage ordering (for display) ──────────────────────────────────────────

PIPELINE_STAGES = [
    ("crown_prince", "太子", "📋"),
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
    # advisory-only (Gemini). Skipped if Gemini isn't configured.
    ("ministry_works_structure_review", "结构审·Gemini", "🔎"),
    ("vibe_critic", "网感复检", "🎯"),
    ("chancellery_final", "终审", "✅"),
]
