"""工部 · Works — Planner designs matrix architecture, Builder generates per-cell prompts."""

from pipeline.agents import BaseAgent


class Works(BaseAgent):
    """工部规划者 — designs shared skeleton + cell plans with ministry_digest."""
    stage_name = "ministry_works"
    prompt_file = "ministries/works.md"


class WorksBuilder(BaseAgent):
    """工部构建者 — generates actual prompts per cell batch."""
    stage_name = "ministry_works_builder"
    prompt_file = "ministries/works_builder.md"
