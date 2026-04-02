"""门下省 · Chancellery — Reviews plans and final output. Deliberately adversarial."""

import json
import asyncio
import time

import anthropic
import streamlit as st

from pipeline.agents import BaseAgent
from pipeline.config import MAX_CHANCELLERY_REJECTIONS, MODELS, STAGE_MAX_TOKENS, MAX_TOKENS_DEFAULT
from db.supabase_client import SupabaseClient


class Chancellery(BaseAgent):
    stage_name = "chancellery"
    prompt_file = "chancellery.md"

    async def run_review(
        self,
        plan: dict,
        brief: dict,
        run_id: str,
        db: SupabaseClient,
        round_number: int = 1,
    ) -> dict:
        """Review a strategic plan. Force-approve on round > MAX_CHANCELLERY_REJECTIONS."""
        stage_name = f"chancellery_{round_number}"

        if round_number > MAX_CHANCELLERY_REJECTIONS:
            # Force pass with risk annotation
            log = db.create_stage_log(run_id, stage_name, {"plan": plan, "brief": brief})
            result = {
                "verdict": "approved",
                "review_dimensions": {},
                "mandatory_revisions": [],
                "suggestions": ["⚠️ 强制通过：已达最大驳回轮次"],
                "revision_instructions": "",
            }
            db.update_stage_log(log["id"], status="completed", output_data=result)
            return result

        input_data = {
            "review_type": "plan_review",
            "plan": plan,
            "brief": brief,
            "round_number": round_number,
        }
        # Override stage_name for logging
        orig = self.stage_name
        self.stage_name = stage_name
        try:
            return await self.run(input_data, run_id, db)
        finally:
            self.stage_name = orig

    async def run_final_review(
        self,
        final_system: dict,
        plan: dict,
        brief: dict,
        run_id: str,
        db: SupabaseClient,
    ) -> dict:
        """Final review of the assembled prompt system."""
        input_data = {
            "review_type": "final_review",
            "prompt_system": final_system,
            "plan": plan,
            "brief": brief,
        }
        orig = self.stage_name
        self.stage_name = "chancellery_final"
        self.model = MODELS.get("chancellery_final", self.model)
        self.max_tokens = STAGE_MAX_TOKENS.get("chancellery_final", MAX_TOKENS_DEFAULT)
        try:
            return await self.run(input_data, run_id, db)
        finally:
            self.stage_name = orig
