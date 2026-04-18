"""Narrative Director — cross-cell coherence + diversity audit.

Runs after works_builder, before vibe_critic. Looks at the WHOLE
prompt_matrix as a unit and diagnoses cross-cell issues:
  - Hook type repetition across directions
  - Persona differentiation failures
  - Narrative structure monotony
  - Product placement clustering

Outputs specific per-cell fix instructions that the builder can
action on a targeted rebuild (not full matrix re-run).
"""

from pipeline.agents import BaseAgent


class NarrativeDirector(BaseAgent):
    stage_name = "narrative_director"
    prompt_file = "narrative_director.md"
