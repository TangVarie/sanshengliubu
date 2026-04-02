"""Pipeline configuration — model assignments, retry strategy, constants."""

# ── Model assignments per stage ────────────────────────────────────────────
# Opus for strategy/review agents, Sonnet for execution agents

MODELS: dict[str, str] = {
    # 太子 — parsing (Sonnet sufficient)
    "crown_prince": "claude-sonnet-4-20250514",
    # 中书省 — strategy (needs Opus depth)
    "secretariat": "claude-opus-4-20250514",
    # 门下省 — review (needs Opus for critical analysis)
    "chancellery": "claude-opus-4-20250514",
    # 尚书省 — task splitting (Sonnet sufficient)
    "dispatcher": "claude-sonnet-4-20250514",
    # 六部 — execution (Sonnet for parallel efficiency)
    "ministry_personnel": "claude-sonnet-4-20250514",
    "ministry_revenue": "claude-sonnet-4-20250514",
    "ministry_rites": "claude-sonnet-4-20250514",
    "ministry_war": "claude-sonnet-4-20250514",
    "ministry_justice": "claude-sonnet-4-20250514",
    # 工部 — assembly (Opus for complex synthesis)
    "ministry_works": "claude-opus-4-20250514",
    # 终审
    "chancellery_final": "claude-opus-4-20250514",
}

# ── Retry & timeout ────────────────────────────────────────────────────────

MAX_RETRIES = 2
RETRY_BASE_DELAY_SECONDS = 3  # exponential backoff: 3s, 6s

# ── Chancellery review ─────────────────────────────────────────────────────

MAX_CHANCELLERY_REJECTIONS = 2  # force pass on round 3

# ── Token limits ───────────────────────────────────────────────────────────

MAX_TOKENS_DEFAULT = 4096
MAX_TOKENS_STRATEGY = 8192  # Secretariat, Works, Chancellery

STAGE_MAX_TOKENS: dict[str, int] = {
    "secretariat": MAX_TOKENS_STRATEGY,
    "chancellery": MAX_TOKENS_STRATEGY,
    "ministry_works": MAX_TOKENS_STRATEGY,
    "chancellery_final": MAX_TOKENS_STRATEGY,
}

# ── Cost tracking (per 1M tokens, approximate) ────────────────────────────

COST_PER_1M_INPUT: dict[str, float] = {
    "claude-opus-4-20250514": 15.0,
    "claude-sonnet-4-20250514": 3.0,
}

COST_PER_1M_OUTPUT: dict[str, float] = {
    "claude-opus-4-20250514": 75.0,
    "claude-sonnet-4-20250514": 15.0,
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
    ("ministry_works", "工部", "🏗️"),
    ("chancellery_final", "终审", "✅"),
]
