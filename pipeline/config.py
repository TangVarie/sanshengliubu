"""Pipeline configuration — model assignments, retry strategy, constants."""

# ── Version ────────────────────────────────────────────────────────────────
# Bump on every meaningful release. Format: vMAJOR.MINOR.PATCH (date) — feature
VERSION = "v0.25.0"
VERSION_DATE = "2026-04-18"
VERSION_NOTES = "参考样本库(证据包) + 自动 AI 分析 + vibe loop 注入评论区 DNA(需跑 migration 005)"

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

OPUS_MODEL = "claude-opus-4-6"
SONNET_MODEL = "claude-sonnet-4-6"

MODEL_PRESET = "content_sonnet"

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
}


def _resolve_models(preset: str) -> dict[str, str]:
    """Assemble the MODELS dict from role tags + preset. Returning a dict
    keeps consumers (logging, cost accounting, settings UI) unchanged."""
    if preset == "all_sonnet":
        return {k: SONNET_MODEL for k in _STAGE_ROLES}
    if preset == "content_sonnet":
        return {
            k: (SONNET_MODEL if role == "content" else OPUS_MODEL)
            for k, role in _STAGE_ROLES.items()
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
    "claude-opus-4-6": 15.0,
    "claude-opus-4-1": 5.0,
    "claude-sonnet-4-6": 3.0,
}

COST_PER_1M_OUTPUT: dict[str, float] = {
    "claude-opus-4-6": 75.0,
    "claude-opus-4-1": 25.0,
    "claude-sonnet-4-6": 15.0,
}

# ── Defaults ──────────────────────────────────────────────────────────────

DEFAULT_PLATFORM = "小红书"

# ── Vibe loop parameters ──────────────────────────────────────────────────
VIBE_LOOP_HARD_CAP = 3       # absolute max iterations
VIBE_LOOP_INITIAL_CAP = 2    # start with this many rounds
VIBE_LOOP_ESCALATE_THRESHOLD = 0.30  # failure rate to unlock extra round

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
    ("chancellery_final", "终审", "✅"),
]
