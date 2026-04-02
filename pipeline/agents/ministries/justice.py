"""刑部 · Justice — Compliance and risk control."""

from pipeline.agents import BaseAgent


class Justice(BaseAgent):
    stage_name = "ministry_justice"
    prompt_file = "ministries/justice.md"
