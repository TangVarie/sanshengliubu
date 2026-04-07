"""Pipeline configuration — model assignments, retry strategy, constants."""

# ── Version ────────────────────────────────────────────────────────────────
# Bump on every meaningful release. Format: vMAJOR.MINOR.PATCH (date) — feature
VERSION = "v0.5.3"
VERSION_DATE = "2026-04-07"
VERSION_NOTES = "全阶段统一 opus-thinking（sonnet 退役）"

# ── Model assignments per stage ────────────────────────────────────────────
# All stages use claude-opus-4-6-thinking. The vip group on the new-api relay
# only has thinking channels for opus, not sonnet. Going all-opus simplifies
# everything and ensures every stage gets extended thinking. Cost is higher
# (opus output is 5x sonnet) but the user explicitly chose this trade-off.
#
# If the -thinking channel ever fails, _call_claude falls back to plain
# claude-opus-4-6 (no thinking) automatically.

OPUS_THINKING = "claude-opus-4-6-thinking"

MODELS: dict[str, str] = {
    "crown_prince": OPUS_THINKING,
    "secretariat": OPUS_THINKING,
    "chancellery": OPUS_THINKING,
    "dispatcher": OPUS_THINKING,
    "ministry_personnel": OPUS_THINKING,
    "ministry_revenue": OPUS_THINKING,
    "ministry_rites": OPUS_THINKING,
    "ministry_war": OPUS_THINKING,
    "ministry_justice": OPUS_THINKING,
    "ministry_works": OPUS_THINKING,
    "ministry_works_cell_planner": OPUS_THINKING,
    "ministry_works_builder": OPUS_THINKING,
    "vibe_critic": OPUS_THINKING,
    "vibe_rewriter": OPUS_THINKING,
    "chancellery_final": OPUS_THINKING,
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
    "ministry_works_builder": 24000,              # self-contained prompts
    "vibe_critic": 20000,
    "vibe_rewriter": 24000,
    "chancellery_final": MAX_TOKENS_STRATEGY,
}

# ── Matrix Execution ─────────────────────────────────────────────────────
# Concurrency lowered from 5 → 3 because every call is now opus-thinking,
# which is heavy on the relay. Slower but more stable.
MATRIX_BATCH_CONCURRENCY = 3    # max parallel builder calls
MATRIX_CELLS_PER_BATCH = 3      # cells per builder call

# ── Cell Planner Batching ────────────────────────────────────────────────
CELL_PLANNER_BATCH_SIZE = 5     # cells per cell-planner call
CELL_PLANNER_CONCURRENCY = 3    # max parallel cell-planner calls

# ── Extended Thinking ─────────────────────────────────────────────────────
# Both Opus and Sonnet stages use thinking. Sonnet gets a smaller default
# budget since execution tasks need less deep deliberation than strategy.
THINKING_BUDGET_TOKENS = 5000  # default budget; per-stage overrides below

# Per-stage thinking budget overrides
STAGE_THINKING_BUDGET: dict[str, int] = {
    # Opus strategy/review stages — deep deliberation
    "crown_prince": 10000,
    "secretariat": 10000,
    "chancellery": 10000,
    "ministry_works": 10000,        # architect — global skeleton design
    "chancellery_final": 10000,
    # Sonnet execution stages — moderate thinking budget
    "dispatcher": 3000,
    "ministry_personnel": 4000,
    "ministry_revenue": 4000,
    "ministry_rites": 4000,
    "ministry_war": 4000,
    "ministry_justice": 4000,
    "ministry_works_cell_planner": 5000,
    "ministry_works_builder": 6000,
    "vibe_critic": 5000,            # taste matching needs space to compare
    "vibe_rewriter": 6000,
}

# ── Cost tracking (per 1M tokens, approximate) ────────────────────────────
# Thinking variants cost the same as base models on the relay; thinking
# tokens are billed as output tokens.

COST_PER_1M_INPUT: dict[str, float] = {
    "claude-opus-4-6-thinking": 15.0,
    "claude-opus-4-6": 15.0,
    "claude-sonnet-4-6": 3.0,  # legacy run logs may reference this
}

COST_PER_1M_OUTPUT: dict[str, float] = {
    "claude-opus-4-6-thinking": 75.0,
    "claude-opus-4-6": 75.0,
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
