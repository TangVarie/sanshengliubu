"""兵部 · War — Competitive strategy and controversy design."""

from pipeline.agents import BaseAgent


class War(BaseAgent):
    stage_name = "ministry_war"
    prompt_file = "ministries/war.md"
