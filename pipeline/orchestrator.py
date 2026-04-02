"""Pipeline orchestrator — sequences all agents, handles clarification pauses."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from datetime import datetime, timezone
from itertools import groupby

from db.supabase_client import SupabaseClient
from pipeline.config import (
    MAX_CHANCELLERY_REJECTIONS,
    MATRIX_BATCH_CONCURRENCY,
    MATRIX_CELLS_PER_BATCH,
)
from pipeline.agents import ClarificationNeeded
from pipeline.agents.crown_prince import CrownPrince
from pipeline.agents.secretariat import Secretariat, SecretariatMatrix
from pipeline.agents.chancellery import Chancellery
from pipeline.agents.dispatcher import Dispatcher
from pipeline.agents.ministries.personnel import Personnel
from pipeline.agents.ministries.revenue import Revenue
from pipeline.agents.ministries.rites import Rites
from pipeline.agents.ministries.war import War
from pipeline.agents.ministries.justice import Justice
from pipeline.agents.ministries.works import Works, WorksBuilder

logger = logging.getLogger(__name__)

MAX_CLARIFICATION_PER_AGENT = 2
CLARIFICATION_POLL_INTERVAL = 5  # seconds
CLARIFICATION_TIMEOUT = 3600  # 1 hour


class PipelineOrchestrator:
    def __init__(self, project_id: str, run_id: str, db: SupabaseClient):
        self.project_id = project_id
        self.run_id = run_id
        self.db = db

        self.crown_prince = CrownPrince()
        self.secretariat = Secretariat()
        self.secretariat_matrix = SecretariatMatrix()
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
        self.works_builder = WorksBuilder()

    # ── Completed stages cache (for resume) ───────────────────────────

    def _load_completed_stages(self) -> dict[str, dict]:
        """Load outputs from already-completed stages in this run."""
        logs = self.db.get_stage_logs(self.run_id)
        return {
            log["stage_name"]: log["output_data"]
            for log in logs
            if log.get("status") == "completed" and log.get("output_data")
        }

    # ── Clarification handling ─────────────────────────────────────────

    async def _wait_for_clarification(self, log_id: str) -> dict:
        """Poll DB until user provides clarification or timeout."""
        self.db.update_pipeline_run(self.run_id, status="paused_for_review")
        self.db.update_project(self.project_id, status="paused_for_review")

        start = time.time()
        while time.time() - start < CLARIFICATION_TIMEOUT:
            log = self.db.get_stage_log_by_id(log_id)
            if log and log.get("human_intervention"):
                # User has responded — resume
                self.db.update_pipeline_run(self.run_id, status="running")
                self.db.update_project(self.project_id, status="running")
                return log["human_intervention"]
            await asyncio.sleep(CLARIFICATION_POLL_INTERVAL)

        raise TimeoutError("Clarification request timed out after 1 hour")

    async def _run_with_clarification(
        self, agent, input_data: dict, max_asks: int = MAX_CLARIFICATION_PER_AGENT
    ) -> dict:
        """Run an agent, handling clarification requests up to max_asks times."""
        asks = 0
        current_input = input_data.copy()

        while True:
            try:
                return await agent.run(current_input, self.run_id, self.db)
            except ClarificationNeeded as cn:
                asks += 1
                if asks > max_asks:
                    # Force continue with partial output
                    logger.warning(
                        f"Agent {cn.stage_name} exceeded max clarifications ({max_asks}), forcing continue"
                    )
                    if cn.partial_output:
                        return cn.partial_output
                    raise

                # Wait for user response
                user_response = await self._wait_for_clarification(cn.log_id)

                # Inject user's clarification into input and re-run
                current_input["clarification_response"] = user_response
                current_input["previous_questions"] = cn.questions

    # ── Main pipeline ──────────────────────────────────────────────────

    async def run(self):
        """Execute the full pipeline with resume support.

        If this run has previously completed stages (e.g. after a failure and
        restart), those stages are skipped and their outputs are reused.
        """
        try:
            self.db.update_pipeline_run(self.run_id, status="running")
            self.db.update_project(self.project_id, status="running")

            done = self._load_completed_stages()

            # 1. 太子 — parse brief
            if "crown_prince" in done:
                logger.info("Resuming: skipping crown_prince (already completed)")
                structured_brief = done["crown_prince"]
            else:
                project = self.db.get_project(self.project_id)
                raw_input = {
                    "free_text": project.get("free_text", ""),
                    "brief": project.get("brief") or {},
                }
                structured_brief = await self._run_with_clarification(self.crown_prince, raw_input)
                self.db.update_project(self.project_id, brief=structured_brief)

            # 2. 中书省 ↔ 门下省 — strategy loop (step 1: skeleton + review)
            if "secretariat" in done:
                logger.info("Resuming: skipping strategy loop (already completed)")
                plan = done["secretariat"]
            else:
                plan = await self._strategy_loop(structured_brief)

            # 2b. 中书省矩阵填充 (step 2: fill active cells)
            if "secretariat_matrix" in done:
                logger.info("Resuming: skipping secretariat_matrix (already completed)")
                plan["platform_direction_matrix"] = done["secretariat_matrix"].get(
                    "platform_direction_matrix", []
                )
            else:
                matrix_input = {
                    "plan": plan,
                    "brief": structured_brief,
                    "matrix_skeleton": plan.get("matrix_skeleton", {}),
                    "tactical_directions": plan.get("tactical_directions", []),
                    "target_platforms": plan.get("target_platforms", []),
                }
                matrix_fill = await self._run_with_clarification(
                    self.secretariat_matrix, matrix_input
                )
                plan["platform_direction_matrix"] = matrix_fill.get(
                    "platform_direction_matrix", []
                )

            # 3. 尚书省 — dispatch
            if "dispatcher" in done:
                logger.info("Resuming: skipping dispatcher (already completed)")
                tasks = done["dispatcher"]
            else:
                dispatch_input = {"plan": plan, "brief": structured_brief}
                tasks = await self._run_with_clarification(self.dispatcher, dispatch_input)

            # 4. 六部（前五部并行，跳过已完成的）
            ministry_outputs = await self._run_ministries(tasks, structured_brief, plan, done)

            # 5a. 工部·规划 — design matrix architecture (single Opus call)
            if "ministry_works" in done:
                logger.info("Resuming: skipping ministry_works planner (already completed)")
                works_plan = done["ministry_works"]
            else:
                works_planner_input = {
                    "ministry_outputs": ministry_outputs,
                    "brief": structured_brief,
                    "plan": plan,
                    "tasks": tasks.get("tasks", {}).get("ministry_works", {}),
                }
                works_plan = await self._run_with_clarification(
                    self.works, works_planner_input
                )

            # 5b. 工部·构建 — generate per-cell prompts (Sonnet, batched)
            final_system = await self._run_works_builders(works_plan)

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
            if plan is None:
                plan = await self._run_with_clarification(
                    self.secretariat, {"brief": brief}
                )
            else:
                plan = await self.secretariat.run_with_revision(
                    brief=brief,
                    revision_feedback=review,
                    previous_plan=plan,
                    run_id=self.run_id,
                    db=self.db,
                )

            review = await self.chancellery.run_review(
                plan, brief, self.run_id, self.db, round_number=round_num
            )

            if review.get("verdict") == "approved":
                return plan

        return plan  # Forced pass on last round

    async def _run_ministries(
        self, tasks: dict, brief: dict, plan: dict, done: dict | None = None
    ) -> dict:
        """Run first 5 ministries in parallel, skip already-completed ones."""
        task_map = tasks.get("tasks", {})
        done = done or {}

        async def run_one(name: str) -> tuple[str, dict | None]:
            # Skip if already completed in a previous run attempt
            if name in done:
                logger.info(f"Resuming: skipping {name} (already completed)")
                return name, done[name]

            agent = self.ministries[name]
            ministry_task = task_map.get(name, {})
            input_data = {
                "task": ministry_task,
                "brief": brief,
                "plan": plan,
            }
            try:
                result = await self._run_with_clarification(agent, input_data)
                return name, result
            except Exception:
                logger.exception(f"Ministry {name} failed, marking as skipped")
                return name, None

        results = await asyncio.gather(
            *[run_one(name) for name in self.ministries]
        )

        return {name: output for name, output in results if output is not None}

    async def _run_works_builders(self, works_plan: dict) -> dict:
        """Run WorksBuilder in batches with concurrency control.

        Smart batching: same-platform cells are grouped together for context reuse.
        Builder only receives shared_skeleton + cell_plans (with ministry_digest),
        NOT the full ministry outputs.
        """
        cell_plans = works_plan.get("cell_plans", [])
        shared_skeleton = works_plan.get("shared_skeleton", {})
        semaphore = asyncio.Semaphore(MATRIX_BATCH_CONCURRENCY)

        # Group by platform, then split into batches of MATRIX_CELLS_PER_BATCH
        def platform_key(cell: dict) -> str:
            return cell.get("platform", cell.get("cell_id", "").split("_")[-1])

        sorted_cells = sorted(cell_plans, key=platform_key)
        batches: list[list[dict]] = []
        for _, group in groupby(sorted_cells, key=platform_key):
            platform_cells = list(group)
            for i in range(0, len(platform_cells), MATRIX_CELLS_PER_BATCH):
                batches.append(platform_cells[i : i + MATRIX_CELLS_PER_BATCH])

        async def build_batch(batch: list[dict], batch_idx: int) -> dict:
            async with semaphore:
                builder_input = {
                    "cell_plans": batch,
                    "shared_skeleton": shared_skeleton,
                }
                return await self.works_builder.run(
                    builder_input, self.run_id, self.db
                )

        results = await asyncio.gather(
            *[build_batch(b, i) for i, b in enumerate(batches)]
        )

        # Merge all batch results
        all_cells: list[dict] = []
        all_demos: list[dict] = []
        for r in results:
            all_cells.extend(r.get("prompt_cells", []))
            all_demos.extend(r.get("demo_outputs", []))

        return {
            "prompt_matrix": all_cells,
            "prompt_templates": all_cells,  # backward compatibility
            "matrix_dimensions": works_plan.get("matrix_dimensions", {}),
            "batch_rules": works_plan.get("batch_rules", {}),
            "usage_guide": works_plan.get("usage_guide", ""),
            "demo_outputs": all_demos,
            "_uncertainty_summary": works_plan.get("_uncertainty_summary", {}),
        }


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


def resume_pipeline_in_background(project_id: str, run_id: str, db: SupabaseClient):
    """Resume a failed pipeline run — reuses existing run_id and skips completed stages."""
    # Reset run status so UI shows it as running again
    db.update_pipeline_run(run_id, status="running", completed_at=None)
    return start_pipeline_in_background(project_id, run_id, db)
