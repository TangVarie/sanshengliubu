"""户部 · Revenue — Keyword strategy and search ecosystem."""

from pipeline.agents import BaseAgent


class Revenue(BaseAgent):
    stage_name = "ministry_revenue"
    prompt_file = "ministries/revenue.md"
