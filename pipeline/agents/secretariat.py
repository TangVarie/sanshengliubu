"""中书省 · Secretariat — Creates strategic plan for the prompt system."""

import json
from pipeline.agents import BaseAgent
from db.supabase_client import SupabaseClient


class Secretariat(BaseAgent):
    stage_name = "secretariat"
    prompt_file = "secretariat.md"

    async def run_with_revision(
        self,
        brief: dict,
        revision_feedback: dict | None,
        previous_plan: dict | None,
        run_id: str,
        db: SupabaseClient,
    ) -> dict:
        """Run with optional revision context from Chancellery rejection."""
        input_data: dict = {"brief": brief}
        if revision_feedback:
            input_data["revision_feedback"] = revision_feedback
        if previous_plan:
            input_data["previous_plan"] = previous_plan
        return await self.run(input_data, run_id, db)


class SecretariatMatrix(BaseAgent):
    """中书省矩阵填充 — fills platform_content_logic for each active cell."""
    stage_name = "secretariat_matrix"
    prompt_file = "secretariat_matrix.md"
