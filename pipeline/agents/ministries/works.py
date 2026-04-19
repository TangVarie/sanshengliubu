"""工部 · Works — Architect designs global skeleton, CellPlanner fills per-cell plans, Builder generates prompts."""

from pipeline.agents import BaseAgent


class Works(BaseAgent):
    """工部架构师 — designs shared skeleton and global architecture only."""
    stage_name = "ministry_works"
    prompt_file = "ministries/works.md"


class WorksCellPlanner(BaseAgent):
    """工部格子规划者 — generates cell_plans per batch with platform logic + ministry digest."""
    stage_name = "ministry_works_cell_planner"
    prompt_file = "ministries/works_cell_planner.md"


class WorksBuilder(BaseAgent):
    """工部构建者 — generates actual prompts per cell batch."""
    stage_name = "ministry_works_builder"
    prompt_file = "ministries/works_builder.md"


class VibeCritic(BaseAgent):
    """网感复检官 — evaluates demo_outputs against per-platform vibe checklists."""
    stage_name = "vibe_critic"
    prompt_file = "vibe_critic.md"


class VibeRewriter(BaseAgent):
    """网感重写者 — rewrites system_prompt for cells that failed vibe critic."""
    stage_name = "vibe_rewriter"
    prompt_file = "vibe_rewriter.md"


class StructuralRewriter(BaseAgent):
    """叙事结构重写者 (v0.29.0) — 处理 identity/gap 类结构性 fail。

    职责边界严格窄于 VibeRewriter:只做叙事身份重构 和 信息缺口方向重构。
    不加钩子 / 不塞真人样本 / 不做具体性补齐——那些是 VibeRewriter 的活。
    由 orchestrator 按 critic 标注的 root_cause_kind 分流到本 agent。
    """
    stage_name = "structural_rewriter"
    prompt_file = "structural_rewriter.md"
