"""Pipeline configuration — model assignments, retry strategy, constants."""

# ── Model assignments per stage ────────────────────────────────────────────
# Opus for strategy/review agents, Sonnet for execution agents

MODELS: dict[str, str] = {
    # 太子 — parsing (Sonnet sufficient)
    "crown_prince": "claude-opus-4-6-thinking",
    # 中书省 — strategy (needs Opus depth)
    "secretariat": "claude-opus-4-6-thinking",
    # 门下省 — review (needs Opus for critical analysis)
    "chancellery": "claude-opus-4-6-thinking",
    # 尚书省 — task splitting (Sonnet sufficient)
    "dispatcher": "claude-sonnet-4-6",
    # 六部 — execution (Sonnet for parallel efficiency)
    "ministry_personnel": "claude-sonnet-4-6",
    "ministry_revenue": "claude-sonnet-4-6",
    "ministry_rites": "claude-sonnet-4-6",
    "ministry_war": "claude-sonnet-4-6",
    "ministry_justice": "claude-sonnet-4-6",
    # 工部 — planner (Opus for complex synthesis), builder (Sonnet for execution)
    "ministry_works": "claude-opus-4-6-thinking",
    "ministry_works_builder": "claude-sonnet-4-6",
    # 中书省矩阵填充 (Opus)
    "secretariat_matrix": "claude-opus-4-6-thinking",
    # 终审
    "chancellery_final": "claude-opus-4-6-thinking",
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
    "secretariat_matrix": MAX_TOKENS_STRATEGY,
    "chancellery": MAX_TOKENS_STRATEGY,
    "ministry_works": MAX_TOKENS_STRATEGY,
    "ministry_works_builder": 8000,
    "chancellery_final": MAX_TOKENS_STRATEGY,
}

# ── Matrix Execution ─────────────────────────────────────────────────────
MATRIX_BATCH_CONCURRENCY = 3    # max parallel builder calls
MATRIX_CELLS_PER_BATCH = 2      # cells per builder call

# ── Extended Thinking ─────────────────────────────────────────────────────
# Opus stages benefit from deep reasoning; Sonnet stages skip thinking for speed
THINKING_BUDGET_TOKENS = 10000  # max tokens for thinking before answering

# ── Cost tracking (per 1M tokens, approximate) ────────────────────────────

COST_PER_1M_INPUT: dict[str, float] = {
    "claude-opus-4-6-thinking": 15.0,
    "claude-sonnet-4-6": 3.0,
}

COST_PER_1M_OUTPUT: dict[str, float] = {
    "claude-opus-4-6-thinking": 75.0,
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
    ("ministry_works", "工部·规划", "🏗️"),
    ("ministry_works_builder", "工部·构建", "🔨"),
    ("chancellery_final", "终审", "✅"),
]
