"""Pipeline configuration — model assignments, retry strategy, constants."""

# ── Model assignments per stage ────────────────────────────────────────────
# Opus for strategy/review agents, Sonnet for execution agents

MODELS: dict[str, str] = {
    # 太子 — parsing
    "crown_prince": "claude-opus-4-6",
    # 中书省 — strategy (needs Opus depth)
    "secretariat": "claude-opus-4-6",
    # 门下省 — review (needs Opus for critical analysis)
    "chancellery": "claude-opus-4-6",
    # 尚书省 — task splitting (Sonnet sufficient)
    "dispatcher": "claude-sonnet-4-6",
    # 六部 — execution (Sonnet for parallel efficiency)
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

MAX_TOKENS_DEFAULT = 4096
MAX_TOKENS_STRATEGY = 32000  # Opus stages with thinking need more headroom

STAGE_MAX_TOKENS: dict[str, int] = {
    "crown_prince": MAX_TOKENS_STRATEGY,
    "secretariat": MAX_TOKENS_STRATEGY,
    "chancellery": MAX_TOKENS_STRATEGY,
    # Execution ministries — Sonnet, large structured outputs
    "ministry_personnel": 8000,
    "ministry_revenue": 8000,
    "ministry_rites": 8000,
    "ministry_war": 8000,
    "ministry_justice": 8000,
    "ministry_works": 16000,  # architect only — small output, no cell_plans
    "ministry_works_cell_planner": 8000,
    "ministry_works_builder": 12000,  # self-contained prompts are larger
    "vibe_critic": 8000,
    "vibe_rewriter": 12000,
    "chancellery_final": MAX_TOKENS_STRATEGY,
}

# ── Matrix Execution ─────────────────────────────────────────────────────
MATRIX_BATCH_CONCURRENCY = 5    # max parallel builder calls
MATRIX_CELLS_PER_BATCH = 3      # cells per builder call

# ── Cell Planner Batching ────────────────────────────────────────────────
CELL_PLANNER_BATCH_SIZE = 5     # cells per cell-planner call
CELL_PLANNER_CONCURRENCY = 5    # max parallel cell-planner calls

# ── Extended Thinking ─────────────────────────────────────────────────────
# Opus stages benefit from deep reasoning; Sonnet stages skip thinking for speed
THINKING_BUDGET_TOKENS = 10000  # max tokens for thinking before answering

# Per-stage thinking budget overrides (merged tasks need more thinking room)
STAGE_THINKING_BUDGET: dict[str, int] = {
    "ministry_works": 10000,  # architect only — global skeleton design
}

# ── Cost tracking (per 1M tokens, approximate) ────────────────────────────

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
