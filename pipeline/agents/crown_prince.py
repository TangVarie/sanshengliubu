"""太子 · Crown Prince — Parses raw user input into structured brief."""

from pipeline.agents import BaseAgent


class CrownPrince(BaseAgent):
    stage_name = "crown_prince"
    prompt_file = "crown_prince.md"
