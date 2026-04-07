"""Pipeline configuration — model assignments, retry strategy, constants."""

# ── Version ────────────────────────────────────────────────────────────────
# Bump on every meaningful release. Format: vMAJOR.MINOR.PATCH (date) — feature
VERSION = "v0.5.4"
VERSION_DATE = "2026-04-07"
VERSION_NOTES = "战略阶段 opus-thinking + 执行阶段 plain opus（sonnet 位置全部换成 opus）"

# ── Model assignments per stage ────────────────────────────────────────────
# Sonnet is fully retired (relay vip group has no sonnet channels). The
# original 5 strategy/review stages keep opus-thinking; the previously-sonnet
# 10 execution stages now use plain opus (no thinking) — same Opus quality,
# without the thinking overhead.
#
# If a -thinking channel call fails, _call_claude falls back to plain
# claude-opus-4-6 automatically.

OPUS_THINKING = "claude-opus-4-6-thinking"
OPUS_PLAIN = "claude-opus-4-6"

MODELS: dict[str, str] = {
    # ── Opus + thinking (strategy / review / architect) ──
    "crown_prince": OPUS_THINKING,         # 太子 — parsing
    "secretariat": OPUS_THINKING,          # 中书省 — strategy
    "chancellery": OPUS_THINKING,          # 门下省 — review
    "ministry_works": OPUS_THINKING,       # 工部架构 — global skeleton
    "chancellery_final": OPUS_THINKING,    # 终审
    # ── Plain opus (execution stages — replaces sonnet) ──
    "dispatcher": OPUS_PLAIN,              # 尚书省 — task split
    "ministry_personnel": OPUS_PLAIN,      # 吏部
    "ministry_revenue": OPUS_PLAIN,        # 户部
    "ministry_rites": OPUS_PLAIN,          # 礼部
    "ministry_war": OPUS_PLAIN,            # 兵部
    "ministry_justice": OPUS_PLAIN,        # 刑部
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
# Only the 5 opus-thinking strategy/review stages use extended thinking.
# Plain opus execution stages skip thinking entirely (faster + cheaper).
THINKING_BUDGET_TOKENS = 10000  # default for any thinking stage

STAGE_THINKING_BUDGET: dict[str, int] = {
    "crown_prince": 10000,
    "secretariat": 10000,
    "chancellery": 10000,
    "ministry_works": 10000,        # architect — global skeleton design
    "chancellery_final": 10000,
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
