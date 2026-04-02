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
