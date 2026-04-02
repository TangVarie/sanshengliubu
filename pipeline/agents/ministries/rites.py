"""礼部 · Rites — Content tone and platform adaptation."""

from pipeline.agents import BaseAgent


class Rites(BaseAgent):
    stage_name = "ministry_rites"
    prompt_file = "ministries/rites.md"
