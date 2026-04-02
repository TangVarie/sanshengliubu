"""吏部 · Personnel — Designs persona library."""

from pipeline.agents import BaseAgent


class Personnel(BaseAgent):
    stage_name = "ministry_personnel"
    prompt_file = "ministries/personnel.md"
