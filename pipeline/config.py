"""Pipeline configuration — model assignments, retry strategy, constants."""

# ── Version ────────────────────────────────────────────────────────────────
# Bump on every meaningful release. Format: vMAJOR.MINOR.PATCH (date) — feature
VERSION = "v0.5.2"
VERSION_DATE = "2026-04-07"
VERSION_NOTES = "回到 base 模型名 + API thinking 参数（中转站无 -thinking channel）"

# ── Model assignments per stage ────────────────────────────────────────────
# Use base model names. The new-api relay routes -thinking suffixed names as
# separate channels which may not be provisioned. Thinking is enabled via
# the API `thinking` parameter (Anthropic's official mechanism), and the
# relay should pass it through. Per-stage thinking control is via
# STAGE_THINKING_BUDGET below — presence of an entry == use thinking.

MODELS: dict[str, str] = {
    # 太子 — parsing
    "crown_prince": "claude-opus-4-6",
    # 中书省 — strategy (needs Opus depth)
    "secretariat": "claude-opus-4-6",
    # 门下省 — review (needs Opus for critical analysis)
    "chancellery": "claude-opus-4-6",
    # 尚书省 — task splitting
    "dispatcher": "claude-sonnet-4-6",
    # 六部 — execution (Sonnet for parallel quality)
    "ministry_personnel": "claude-sonnet-4-6",
    "ministry_revenue": "claude-sonnet-4-6",
    "ministry_rites": "claude-sonnet-4-6",
    "ministry_war": "claude-sonnet-4-6",
    "ministry_justice": "claude-sonnet-4-6",
    # 工部 — architect (Opus), cell planner (Sonnet batched), builder (Sonnet batched)
    "ministry_works": "claude-opus-4-6",
    "ministry_works_cell_planner": "claude-sonnet-4-6",
    "ministry_works_builder": "claude-sonnet-4-6",
    # 网感复检循环
    "vibe_critic": "claude-sonnet-4-6",
    "vibe_rewriter": "claude-sonnet-4-6",
    # 终审
    "chancellery_final": "claude-opus-4-6",
}

# ── Retry & timeout ────────────────────────────────────────────────────────

MAX_RETRIES = 2
RETRY_BASE_DELAY_SECONDS = 3  # exponential backoff: 3s, 6s

# ── Chancellery review ─────────────────────────────────────────────────────

MAX_CHANCELLERY_REJECTIONS = 2  # force pass on round 3

# ── Token limits ───────────────────────────────────────────────────────────
# Note: when thinking is enabled, max_tokens must accommodate
# (thinking_budget + actual_output). All sonnet stages bumped accordingly.

MAX_TOKENS_DEFAULT = 8000
MAX_TOKENS_STRATEGY = 32000  # Opus stages with thinking need more headroom

STAGE_MAX_TOKENS: dict[str, int] = {
    "crown_prince": MAX_TOKENS_STRATEGY,
    "secretariat": MAX_TOKENS_STRATEGY,
    "chancellery": MAX_TOKENS_STRATEGY,
    "dispatcher": 16000,  # thinking + structured task split for 6 ministries
    # Execution ministries — Sonnet thinking + structured outputs
    "ministry_personnel": 16000,
    "ministry_revenue": 16000,
    "ministry_rites": 16000,
    "ministry_war": 16000,
    "ministry_justice": 16000,
    "ministry_works": MAX_TOKENS_STRATEGY,  # architect — Opus
    "ministry_works_cell_planner": 16000,
    "ministry_works_builder": 20000,  # self-contained prompts are larger
    "vibe_critic": 16000,  # critic now richer (gut_call + taste_match + diagnostics)
    "vibe_rewriter": 20000,
    "chancellery_final": MAX_TOKENS_STRATEGY,
}

# ── Matrix Execution ─────────────────────────────────────────────────────
MATRIX_BATCH_CONCURRENCY = 5    # max parallel builder calls
MATRIX_CELLS_PER_BATCH = 3      # cells per builder call

# ── Cell Planner Batching ────────────────────────────────────────────────
CELL_PLANNER_BATCH_SIZE = 5     # cells per cell-planner call
CELL_PLANNER_CONCURRENCY = 5    # max parallel cell-planner calls

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
    "claude-opus-4-6": 15.0,
    "claude-sonnet-4-6": 3.0,
}

COST_PER_1M_OUTPUT: dict[str, float] = {
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
