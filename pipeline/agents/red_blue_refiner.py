"""Red-Blue adversarial content refiner.

Combined Red Team (attack AI-tone issues) + Blue Team (fix with
minimal changes) in a single Claude call per cell. Runs AFTER
narrative_director, BEFORE vibe_critic. Catches and fixes specific
AI-tone tells that builder tends to leave in demo_output.
"""

from pipeline.agents import BaseAgent


class RedBlueRefiner(BaseAgent):
    stage_name = "red_blue_refiner"
    prompt_file = "red_blue_refiner.md"
