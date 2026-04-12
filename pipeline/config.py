"""Pipeline configuration — model assignments, retry strategy, constants."""

# ── Version ────────────────────────────────────────────────────────────────
# Bump on every meaningful release. Format: vMAJOR.MINOR.PATCH (date) — feature
VERSION = "v0.8.0"
VERSION_DATE = "2026-04-12"
VERSION_NOTES = "Vertex AI 支持 + adaptive thinking（告别中转站 8K 截断）"

# ── Model assignments per stage ────────────────────────────────────────────
# All stages use the same base model. On Vertex AI, thinking is controlled
# by _use_thinking (adaptive thinking) not model name suffix. On legacy relay,
# the -thinking suffix may still be appended at runtime by _call_claude.

MODEL = "claude-opus-4-6"

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
    "ministry_works": MAX_TOKENS_STRATEGY,        # architect — global skeleton
    "ministry_works_cell_planner": 20000,
    "ministry_works_builder": 32000,              # self-contained prompts
    "vibe_critic": 20000,
    "vibe_rewriter": 24000,
    "chancellery_final": MAX_TOKENS_STRATEGY,
}

# ── Matrix Execution ─────────────────────────────────────────────────────
# Concurrency kept at 3 (opus-thinking is heavy on the relay).
# Cells-per-batch = 1: each cell's system_prompt is already 2-4K tokens with
# all 5 differentiation pools + personas + compliance + real-person samples +
# media_brief + comment_seeds + demo_output. Batching multiple cells per call
# causes JSON malformation (unescaped inner quotes, truncation). Single-cell
# batches give the simplest possible JSON structure and isolate any failure
# to exactly one cell (which Round 3 cell-retry will recover anyway).
MATRIX_BATCH_CONCURRENCY = 3    # max parallel builder calls
MATRIX_CELLS_PER_BATCH = 1      # cells per builder call

# ── Cell Planner Batching ────────────────────────────────────────────────
CELL_PLANNER_BATCH_SIZE = 5     # cells per cell-planner call
CELL_PLANNER_CONCURRENCY = 3    # max parallel cell-planner calls

# ── Extended Thinking ─────────────────────────────────────────────────────
# On Vertex AI: uses adaptive thinking (model decides when/how much to think).
# On legacy relay: uses budget_tokens=10000 as fallback.
# Thinking is enabled for the 5 strategy/review stages listed in
# _THINKING_STAGES below. Execution stages skip thinking for speed.

THINKING_STAGES: frozenset[str] = frozenset({
    "crown_prince",
    "secretariat",
    "chancellery",
    "ministry_works",
    "chancellery_final",
})

# ── Cost tracking (per 1M tokens, approximate) ────────────────────────────
# Vertex AI pricing: Opus 4.6 input $5, output $25 (direct Anthropic is same).
# Thinking tokens count as output tokens.

COST_PER_1M_INPUT: dict[str, float] = {
    "claude-opus-4-6": 5.0,
    "claude-opus-4-6-thinking": 5.0,  # legacy relay logs
    "claude-sonnet-4-6": 3.0,         # legacy logs
}

COST_PER_1M_OUTPUT: dict[str, float] = {
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
