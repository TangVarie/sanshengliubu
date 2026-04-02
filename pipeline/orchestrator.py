"""Pipeline orchestrator — sequences all agents through the full flow."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from datetime import datetime, timezone

from db.supabase_client import SupabaseClient
from pipeline.config import MAX_CHANCELLERY_REJECTIONS
from pipeline.agents.crown_prince import CrownPrince
from pipeline.agents.secretariat import Secretariat
from pipeline.agents.chancellery import Chancellery
from pipeline.agents.dispatcher import Dispatcher
from pipeline.agents.ministries.personnel import Personnel
from pipeline.agents.ministries.revenue import Revenue
from pipeline.agents.ministries.rites import Rites
from pipeline.agents.ministries.war import War
from pipeline.agents.ministries.justice import Justice
from pipeline.agents.ministries.works import Works

logger = logging.getLogger(__name__)


class PipelineOrchestrator:
    def __init__(self, project_id: str, run_id: str, db: SupabaseClient):
        self.project_id = project_id
        self.run_id = run_id
        self.db = db

        self.crown_prince = CrownPrince()
        self.secretariat = Secretariat()
        self.chancellery = Chancellery()
        self.dispatcher = Dispatcher()
        self.ministries = {
            "ministry_personnel": Personnel(),
            "ministry_revenue": Revenue(),
            "ministry_rites": Rites(),
            "ministry_war": War(),
            "ministry_justice": Justice(),
        }
        self.works = Works()

    async def run(self):
        """Execute the full pipeline."""
        try:
            self.db.update_project(self.project_id, status="running")

            # 1. 太子 — parse brief
            project = self.db.get_project(self.project_id)
            raw_input = {
                "free_text": project.get("free_text", ""),
                "brief": project.get("brief") or {},
            }
            structured_brief = await self.crown_prince.run(raw_input, self.run_id, self.db)
            self.db.update_project(self.project_id, brief=structured_brief)

            # 2. 中书省 ↔ 门下省 — strategy loop
            plan = await self._strategy_loop(structured_brief)

            # 3. 尚书省 — dispatch
            dispatch_input = {"plan": plan, "brief": structured_brief}
            tasks = await self.dispatcher.run(dispatch_input, self.run_id, self.db)

            # 4. 六部（前五部并行）
            ministry_outputs = await self._run_ministries(tasks, structured_brief, plan)

            # 5. 工部 — assembly
            works_input = {
                "ministry_outputs": ministry_outputs,
                "brief": structured_brief,
                "plan": plan,
                "tasks": tasks.get("tasks", {}).get("ministry_works", {}),
            }
            final_system = await self.works.run(works_input, self.run_id, self.db)

            # 6. 门下省终审
            final_review = await self.chancellery.run_final_review(
                final_system, plan, structured_brief, self.run_id, self.db
            )

            # 7. Save output
            self.db.save_output(self.run_id, final_system, final_review)
            self.db.update_pipeline_run(
                self.run_id,
                status="completed",
                completed_at=datetime.now(timezone.utc).isoformat(),
            )
            self.db.update_project(self.project_id, status="completed")

        except Exception as e:
            logger.exception("Pipeline failed")
            self.db.update_pipeline_run(
                self.run_id,
                status="failed",
                completed_at=datetime.now(timezone.utc).isoformat(),
            )
            self.db.update_project(self.project_id, status="failed")
            raise

    async def _strategy_loop(self, brief: dict) -> dict:
        """Secretariat → Chancellery review, max 2 rejections then force pass."""
        plan = None
        review = None

        for round_num in range(1, MAX_CHANCELLERY_REJECTIONS + 2):
            # Generate / revise plan
            if plan is None:
                plan = await self.secretariat.run({"brief": brief}, self.run_id, self.db)
            else:
                plan = await self.secretariat.run_with_revision(
                    brief=brief,
                    revision_feedback=review,
                    previous_plan=plan,
                    run_id=self.run_id,
                    db=self.db,
                )

            # Review
            review = await self.chancellery.run_review(
                plan, brief, self.run_id, self.db, round_number=round_num
            )

            if review.get("verdict") == "approved":
                return plan

        return plan  # Forced pass on last round

    async def _run_ministries(self, tasks: dict, brief: dict, plan: dict) -> dict:
        """Run first 5 ministries in parallel, return dict of outputs."""
        task_map = tasks.get("tasks", {})

        async def run_one(name: str) -> tuple[str, dict | None]:
            agent = self.ministries[name]
            ministry_task = task_map.get(name, {})
            input_data = {
                "task": ministry_task,
                "brief": brief,
                "plan": plan,
            }
            try:
                result = await agent.run(input_data, self.run_id, self.db)
                return name, result
            except Exception:
                logger.exception(f"Ministry {name} failed, marking as skipped")
                return name, None

        results = await asyncio.gather(
            *[run_one(name) for name in self.ministries]
        )

        return {name: output for name, output in results if output is not None}


def start_pipeline_in_background(project_id: str, run_id: str, db: SupabaseClient):
    """Launch pipeline in a background thread (for Streamlit compatibility)."""

    def _thread_target():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            orchestrator = PipelineOrchestrator(project_id, run_id, db)
            loop.run_until_complete(orchestrator.run())
        except Exception:
            logger.exception("Background pipeline thread failed")
        finally:
            loop.close()

    thread = threading.Thread(target=_thread_target, daemon=True)
    thread.start()
    return thread
