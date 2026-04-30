"""门下省 · Chancellery — Reviews plans and final output. Deliberately adversarial."""

from pipeline.agents import BaseAgent
from pipeline.config import (
    MAX_CHANCELLERY_REJECTIONS,
    MAX_FINAL_REJECTIONS,
    MODELS,
    STAGE_MAX_TOKENS,
    MAX_TOKENS_DEFAULT,
)
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
        round_number: int = 1,
        prior_review: dict | None = None,
    ) -> dict:
        """Final review of the assembled prompt system.

        Round semantics:
          - round_number == 1 → first review of a freshly built matrix.
          - round_number >= 2 → re-review after the user applied revisions. In
            this case `prior_review` should contain the previous round's
            mandatory_revisions / revision_instructions / review_dimensions so
            the model can do a DELTA review (only flag unresolved or newly
            introduced issues, not repeat what was already flagged).
          - round_number > MAX_FINAL_REJECTIONS → force approve. Prevents
            infinite rejection loops when the model keeps finding new angles of
            the same structural problem.
        """
        # Save and swap all three attributes atomically, restore in finally.
        orig_stage = self.stage_name
        orig_model = self.model
        orig_max_tokens = self.max_tokens
        self.stage_name = "chancellery_final"
        self.model = MODELS.get("chancellery_final", orig_model)
        self.max_tokens = STAGE_MAX_TOKENS.get("chancellery_final", MAX_TOKENS_DEFAULT)

        try:
            if round_number > MAX_FINAL_REJECTIONS:
                # Force pass — log a synthetic stage_log so UI still shows the
                # decision, and return a structured result with a risk note.
                log = db.create_stage_log(
                    run_id,
                    "chancellery_final",
                    {
                        "review_type": "final_review",
                        "round_number": round_number,
                        "force_pass": True,
                        "prior_verdict": (prior_review or {}).get("verdict", "unknown"),
                    },
                )
                result = {
                    "verdict": "approved",
                    "review_dimensions": {},
                    "mandatory_revisions": [],
                    "suggestions": [
                        f"⚠️ 终审强制通过：已达最大驳回轮次 {MAX_FINAL_REJECTIONS}。"
                        f"上一轮 verdict={(prior_review or {}).get('verdict', 'unknown')}。"
                        f"建议人工复核 prompt_matrix。"
                    ],
                    "revision_instructions": "",
                    "_forced_pass": True,
                    "_round_number": round_number,
                }
                db.update_stage_log(log["id"], status="completed", output_data=result)
                return result

            input_data = {
                "review_type": "final_review",
                "prompt_system": final_system,
                "plan": plan,
                "brief": brief,
                "round_number": round_number,
            }

            # v0.30.5 fix H4: 把叙事导演的诊断结果显式提取出来给终审,
            # final_system 里有 _narrative_director_result 但 chancellery
            # prompt 不知道去翻 final_system 子字段,所以等于看不到。
            # 抽 slim 摘要(verdict + 重建过的 cell 列表 + issues 简表)。
            _nd = final_system.get("_narrative_director_result") or {}
            if _nd:
                input_data["narrative_director_summary"] = {
                    "verdict": _nd.get("verdict", "unknown"),
                    "issues": (_nd.get("issues") or [])[:10],
                    "cells_rebuilt": [
                        c.get("cell_id")
                        for c in (_nd.get("cells_to_revise") or [])
                        if c.get("cell_id")
                    ],
                    "cross_cell_summary": _nd.get("cross_cell_summary", "")[:500],
                }

            if prior_review and round_number > 1:
                input_data["prior_review"] = {
                    "verdict": prior_review.get("verdict", "unknown"),
                    "mandatory_revisions": prior_review.get("mandatory_revisions", []),
                    "revision_instructions": prior_review.get(
                        "revision_instructions", ""
                    ),
                    "review_dimensions": prior_review.get("review_dimensions", {}),
                }

            return await self.run(input_data, run_id, db)
        finally:
            # Restore ALL mutated attributes — prevents corruption if an
            # exception occurs partway through the method.
            self.stage_name = orig_stage
            self.model = orig_model
            self.max_tokens = orig_max_tokens
