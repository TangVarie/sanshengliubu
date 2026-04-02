"""工部 · Works — Assembles all ministry outputs into final prompt system."""

from pipeline.agents import BaseAgent


class Works(BaseAgent):
    stage_name = "ministry_works"
    prompt_file = "ministries/works.md"
