"""User persona simulator — simulated reader reactions.

Creates 3 target-audience personas and has each react to every cell's
demo_output with a 0.5-second gut reaction (click/skip/save). Runs
AFTER red-blue refinement, results injected as advisory input to
vibe_critic so the critic sees "real user reactions" alongside its
own expert judgment.
"""

from pipeline.agents import BaseAgent


class PersonaSimulator(BaseAgent):
    stage_name = "persona_simulator"
    prompt_file = "persona_simulator.md"
