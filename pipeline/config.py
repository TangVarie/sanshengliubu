"""Pipeline configuration — model assignments, retry strategy, constants."""

# ── Version ────────────────────────────────────────────────────────────────
# Bump on every meaningful release. Format: vMAJOR.MINOR.PATCH (date) — feature
VERSION = "v0.9.0"
VERSION_DATE = "2026-04-13"
VERSION_NOTES = "删除 usage_guide/batch_rules 静态字段 — 断开终审无限循环"

# ── Model assignments per stage ────────────────────────────────────────────
# Relay mode: strategy stages use -thinking suffix (relay routes to thinking channel).
# Execution stages use plain opus (no thinking, faster).
# When switching to Vertex: change both to "claude-opus-4-6" (Vertex doesn't
# use -thinking suffix, thinking is controlled by THINKING_STAGES).

OPUS_THINKING = "claude-opus-4-6-thinking"
OPUS_PLAIN = "claude-opus-4-6"

MODELS: dict[str, str] = {
    # Thinking stages: relay routes -thinking suffix to thinking backend
    "crown_prince": OPUS_THINKING,
    "secretariat": OPUS_THINKING,
    "chancellery": OPUS_THINKING,
    "ministry_works": OPUS_THINKING,
    "chancellery_final": OPUS_THINKING,
    # Execution stages: plain opus, no thinking
    "dispatcher": OPUS_PLAIN,
    "ministry_personnel": OPUS_PLAIN,
    "ministry_revenue": OPUS_PLAIN,
    "ministry_rites": OPUS_PLAIN,
    "ministry_war": OPUS_PLAIN,
    "ministry_justice": OPUS_PLAIN,
    "ministry_works_cell_planner": OPUS_PLAIN,
    "ministry_works_builder": OPUS_PLAIN,
    "vibe_critic": OPUS_PLAIN,
    "vibe_rewriter": OPUS_PLAIN,
}

# ── Retry & timeout ────────────────────────────────────────────────────────

MAX_RETRIES = 2
RETRY_BASE_DELAY_SECONDS = 3  # exponential backoff: 3s, 6s

# ── Chancellery review ─────────────────────────────────────────────────────

MAX_CHANCELLERY_REJECTIONS = 2  # force pass on round 3

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
# Set to 0 for relay/direct (no rate limit needed). Increase for Vertex if
# quota is tight.
MIN_SECONDS_BETWEEN_CALLS = 0

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
    ("vibe_critic", "网感复检", "🎯"),
    ("chancellery_final", "终审", "✅"),
]
