"""Pipeline configuration — model assignments, retry strategy, constants."""

# ── Version ────────────────────────────────────────────────────────────────
# Bump on every meaningful release. Format: vMAJOR.MINOR.PATCH (date) — feature
VERSION = "v0.8.1"
VERSION_DATE = "2026-04-12"
VERSION_NOTES = "Vertex AI Opus 4.1 (us-east5, 15K TPM quota) + 低并发适配"

# ── Model assignments per stage ────────────────────────────────────────────
# Using Opus 4.1 on Vertex AI (only model with available quota).
# Opus 4.1 does NOT support adaptive thinking — must use budget_tokens.

MODEL = "claude-opus-4-1"

MODELS: dict[str, str] = {
    # ── Strategy / review / architect (thinking enabled) ──
    "crown_prince": MODEL,
    "secretariat": MODEL,
    "chancellery": MODEL,
    "ministry_works": MODEL,
    "chancellery_final": MODEL,
    # ── Execution stages (thinking disabled for speed) ──
    "dispatcher": MODEL,
    "ministry_personnel": MODEL,
    "ministry_revenue": MODEL,
    "ministry_rites": MODEL,
    "ministry_war": MODEL,
    "ministry_justice": MODEL,
    "ministry_works_cell_planner": MODEL,
    "ministry_works_builder": MODEL,
    "vibe_critic": MODEL,
    "vibe_rewriter": MODEL,
}

# ── Retry & timeout ────────────────────────────────────────────────────────

MAX_RETRIES = 2
RETRY_BASE_DELAY_SECONDS = 3  # exponential backoff: 3s, 6s

# ── Chancellery review ─────────────────────────────────────────────────────

MAX_CHANCELLERY_REJECTIONS = 2  # force pass on round 3

# ── Token limits ───────────────────────────────────────────────────────────
# All stages run opus-thinking now. max_tokens must accommodate
# (thinking_budget + actual_output). Headroom kept generous because opus
# thinking can produce long internal reasoning.

MAX_TOKENS_DEFAULT = 8192
MAX_TOKENS_STRATEGY = 16000  # strategy/review stages

STAGE_MAX_TOKENS: dict[str, int] = {
    "crown_prince": MAX_TOKENS_STRATEGY,
    "secretariat": MAX_TOKENS_STRATEGY,
    "chancellery": MAX_TOKENS_STRATEGY,
    "dispatcher": 8192,
    "ministry_personnel": 8192,
    "ministry_revenue": 8192,
    "ministry_rites": 8192,
    "ministry_war": 8192,
    "ministry_justice": 8192,
    "ministry_works": MAX_TOKENS_STRATEGY,        # architect — global skeleton
    "ministry_works_cell_planner": 8192,
    "ministry_works_builder": 16000,              # self-contained prompts need more
    "vibe_critic": 8192,
    "vibe_rewriter": 12000,
    "chancellery_final": MAX_TOKENS_STRATEGY,
}

# ── Matrix Execution ─────────────────────────────────────────────────────
# Concurrency = 1: Vertex AI quota is only 15K input tokens/min for Opus 4.1.
# Each call uses 3-10K input tokens, so we can only do ~1-2 calls per minute.
# Serial execution prevents quota exhaustion.
MATRIX_BATCH_CONCURRENCY = 1    # serial — one builder call at a time
MATRIX_CELLS_PER_BATCH = 1      # one cell per call (simplest JSON structure)

# ── Cell Planner Batching ────────────────────────────────────────────────
CELL_PLANNER_BATCH_SIZE = 5     # cells per cell-planner call
CELL_PLANNER_CONCURRENCY = 1    # serial — one planner call at a time

# ── Extended Thinking ─────────────────────────────────────────────────────
# Opus 4.1 does NOT support adaptive thinking — must use budget_tokens.
# With 15K TPM quota, thinking budget kept small (4096) to conserve tokens.
# Execution stages skip thinking entirely.

THINKING_STAGES: frozenset[str] = frozenset({
    "crown_prince",
    "secretariat",
    "chancellery",
    "ministry_works",
    "chancellery_final",
})

THINKING_BUDGET_TOKENS = 4096  # small to stay within 15K TPM quota

# ── Cost tracking (per 1M tokens, approximate) ────────────────────────────
# Vertex AI pricing: Opus 4.6 input $5, output $25 (direct Anthropic is same).
# Thinking tokens count as output tokens.

COST_PER_1M_INPUT: dict[str, float] = {
    "claude-opus-4-1": 5.0,
    "claude-opus-4-6": 5.0,
    "claude-opus-4-6-thinking": 5.0,
    "claude-sonnet-4-6": 3.0,
}

COST_PER_1M_OUTPUT: dict[str, float] = {
    "claude-opus-4-1": 25.0,
    "claude-opus-4-6": 25.0,
    "claude-opus-4-6-thinking": 25.0,
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
