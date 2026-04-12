"""Pipeline configuration — model assignments, retry strategy, constants."""

# ── Version ────────────────────────────────────────────────────────────────
# Bump on every meaningful release. Format: vMAJOR.MINOR.PATCH (date) — feature
VERSION = "v0.8.2"
VERSION_DATE = "2026-04-12"
VERSION_NOTES = "Vertex Opus 4.1 · thinking 关闭 · 调用间限速 15s · 适配 15K TPM"

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
# DISABLED: 15K TPM quota is too tight for thinking overhead.
# Each thinking call roughly doubles token usage (thinking output counts
# toward billing). With serial execution and 15K/min, we can't afford it.
# Re-enable when quota increases.

THINKING_STAGES: frozenset[str] = frozenset()  # empty = no stage uses thinking

THINKING_BUDGET_TOKENS = 4096  # unused while THINKING_STAGES is empty

# ── Rate Limiting ─────────────────────────────────────────────────────────
# Minimum seconds between API calls. With 15K TPM and ~3-5K input per call,
# we need to spread calls across time to avoid 429.
MIN_SECONDS_BETWEEN_CALLS = 15  # ~4 calls/min × 4K avg = 16K ≈ safe under 15K

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
