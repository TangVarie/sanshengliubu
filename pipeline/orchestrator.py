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
    CELL_PLANNER_BATCH_SIZE,
    CELL_PLANNER_CONCURRENCY,
    MAX_CHANCELLERY_REJECTIONS,
    MATRIX_BATCH_CONCURRENCY,
    MATRIX_CELLS_PER_BATCH,
)
from pipeline.agents import ClarificationNeeded
from pipeline.agents.crown_prince import CrownPrince
from pipeline.agents.secretariat import Secretariat
from pipeline.agents.chancellery import Chancellery
from pipeline.agents.dispatcher import Dispatcher
from pipeline.agents.ministries.personnel import Personnel
from pipeline.agents.ministries.revenue import Revenue
from pipeline.agents.ministries.rites import Rites
from pipeline.agents.ministries.war import War
from pipeline.agents.ministries.justice import Justice
from pipeline.agents.ministries.works import (
    Works,
    WorksCellPlanner,
    WorksBuilder,
    VibeCritic,
    VibeRewriter,
)

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
        self.works_cell_planner = WorksCellPlanner()
        self.works_builder = WorksBuilder()
        self.vibe_critic = VibeCritic()
        self.vibe_rewriter = VibeRewriter()

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

            # 3. 尚书省 — dispatch
            if "dispatcher" in done:
                logger.info("Resuming: skipping dispatcher (already completed)")
                tasks = done["dispatcher"]
            else:
                dispatch_input = {"plan": plan, "brief": structured_brief}
                tasks = await self._run_with_clarification(self.dispatcher, dispatch_input)

            # 4. 六部（前五部并行，跳过已完成的）
            ministry_outputs = await self._run_ministries(tasks, structured_brief, plan, done)

            # 5a. 工部·架构 — global skeleton design (Opus, small output)
            if "ministry_works" in done:
                logger.info("Resuming: skipping ministry_works architect (already completed)")
                works_arch = done["ministry_works"]
            else:
                works_planner_input = {
                    "ministry_outputs": ministry_outputs,
                    "brief": structured_brief,
                    "plan": plan,
                    "tasks": tasks.get("tasks", {}).get("ministry_works", {}),
                }
                works_arch = await self._run_with_clarification(
                    self.works, works_planner_input
                )

            # 5b. 工部·格子规划 — per-cell plans (Sonnet, batched + parallel)
            cell_plans = await self._run_cell_planners(
                works_arch, ministry_outputs, structured_brief, plan
            )

            if not cell_plans:
                raise RuntimeError(
                    f"工部·格子规划产出为空，无法继续。"
                    f"plan keys: {list(plan.keys())}, "
                    f"matrix_skeleton: {plan.get('matrix_skeleton', 'MISSING')}, "
                    f"tactical_directions count: {len(plan.get('tactical_directions', []))}, "
                    f"target_platforms: {plan.get('target_platforms', [])}"
                )

            # Assemble full works_plan for builder
            works_plan = {**works_arch, "cell_plans": cell_plans}

            # 5c. 工部·构建 — generate per-cell prompts (Sonnet, batched)
            final_system = await self._run_works_builders(works_plan)

            if not final_system.get("prompt_matrix"):
                raise RuntimeError(
                    f"工部·构建产出为空。cell_plans count: {len(cell_plans)}, "
                    f"works_plan keys: {list(works_plan.keys())}"
                )

            # 5d. 网感复检循环 — critic checks demo, rewriter fixes failed cells
            final_system = await self._run_vibe_loop(final_system)

            # 6. 门下省终审
            final_review = await self.chancellery.run_final_review(
                final_system, plan, structured_brief, self.run_id, self.db
            )

            # 7. Save output (always — user wants to see partial output even on revision_required)
            self.db.save_output(self.run_id, final_system, final_review)

            # 8. Determine final status from chancellery_final verdict
            verdict = (final_review or {}).get("verdict", "").strip().lower()
            if verdict == "approved":
                final_status = "completed"
                logger.info(f"Pipeline {self.run_id} completed (终审 approved)")
            else:
                # revision_required / rejected / unknown — DO NOT mark as completed
                final_status = "needs_revision"
                logger.warning(
                    f"Pipeline {self.run_id} 终审 verdict={verdict!r}, "
                    f"marking as needs_revision. revision_instructions: "
                    f"{(final_review or {}).get('revision_instructions', '')[:300]}"
                )
            self.db.update_pipeline_run(
                self.run_id,
                status=final_status,
                completed_at=datetime.now(timezone.utc).isoformat(),
            )
            self.db.update_project(self.project_id, status=final_status)

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
        """Run first 5 ministries in parallel. Any failure aborts the pipeline."""
        task_map = tasks.get("tasks", {})
        done = done or {}

        async def run_one(name: str) -> tuple[str, dict]:
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
            except Exception as e:
                # Do NOT silently swallow — a failed ministry means the
                # downstream output will be broken. Surface the error so the
                # pipeline marks itself failed and the user can resume.
                logger.exception(f"Ministry {name} failed: {e}")
                raise RuntimeError(f"{name} 执行失败：{e}") from e

        results = await asyncio.gather(
            *[run_one(name) for name in self.ministries]
        )

        return {name: output for name, output in results}

    async def _run_cell_planners(
        self,
        works_arch: dict,
        ministry_outputs: dict,
        brief: dict,
        plan: dict,
    ) -> list[dict]:
        """Run WorksCellPlanner in batches to generate cell_plans.

        Splits active_cells into batches and runs them in parallel with Sonnet.
        Each batch receives shared_skeleton + ministry_outputs + its cell subset.
        """
        logger.info(
            f"[cell_planners] plan keys: {list(plan.keys())}, "
            f"matrix_skeleton type: {type(plan.get('matrix_skeleton'))}, "
            f"active_cells: {plan.get('matrix_skeleton', {}).get('active_cells', 'MISSING')!r:.200s}, "
            f"tactical_directions count: {len(plan.get('tactical_directions', []))}, "
            f"target_platforms: {plan.get('target_platforms', [])}"
        )

        active_cells = plan.get("matrix_skeleton", {}).get("active_cells", [])

        # Fallback: reconstruct active_cells from directions × platforms if missing
        if not active_cells:
            directions = plan.get("tactical_directions", [])
            platforms = plan.get("target_platforms", [])
            if directions and platforms:
                logger.warning(
                    "matrix_skeleton.active_cells is empty, reconstructing from "
                    f"{len(directions)} directions × {len(platforms)} platforms"
                )
                for d in directions:
                    d_id = d.get("direction_id", d) if isinstance(d, dict) else d
                    for p in platforms:
                        p_name = p if isinstance(p, str) else str(p)
                        p_key = p_name.replace(" ", "").lower()
                        active_cells.append({
                            "cell_id": f"{d_id}_{p_key}",
                            "direction_id": d_id,
                            "platform": p_name,
                        })

        if not active_cells:
            logger.error(
                "No active_cells found and could not reconstruct. "
                f"plan keys: {list(plan.keys())}, "
                f"matrix_skeleton: {plan.get('matrix_skeleton', 'MISSING')}"
            )
            return []

        shared_skeleton = works_arch.get("shared_skeleton", {})
        semaphore = asyncio.Semaphore(CELL_PLANNER_CONCURRENCY)

        # Split into batches
        batches: list[list] = []
        for i in range(0, len(active_cells), CELL_PLANNER_BATCH_SIZE):
            batches.append(active_cells[i : i + CELL_PLANNER_BATCH_SIZE])

        logger.info(
            f"Cell planner: {len(active_cells)} cells → {len(batches)} batches "
            f"(size={CELL_PLANNER_BATCH_SIZE}, concurrency={CELL_PLANNER_CONCURRENCY})"
        )

        async def plan_batch(batch: list, batch_idx: int) -> dict:
            async with semaphore:
                batch_input = {
                    "active_cells": batch,
                    "shared_skeleton": shared_skeleton,
                    "ministry_outputs": ministry_outputs,
                    "brief": brief,
                    "plan_summary": {
                        "tactical_directions": plan.get("tactical_directions", []),
                        "target_platforms": plan.get("target_platforms", []),
                    },
                }
                return await self.works_cell_planner.run(
                    batch_input, self.run_id, self.db
                )

        results = await asyncio.gather(
            *[plan_batch(b, i) for i, b in enumerate(batches)]
        )

        # Merge all batch cell_plans
        all_cell_plans: list[dict] = []
        for r in results:
            all_cell_plans.extend(r.get("cell_plans", []))

        logger.info(f"Cell planner completed: {len(all_cell_plans)} cell_plans generated")
        return all_cell_plans

    async def _run_works_builders(self, works_plan: dict) -> dict:
        """Run WorksBuilder in batches with concurrency control.

        Smart batching: same-platform cells are grouped together for context reuse.
        Builder only receives shared_skeleton + cell_plans (with ministry_digest),
        NOT the full ministry outputs.
        """
        cell_plans = works_plan.get("cell_plans", [])
        shared_skeleton = works_plan.get("shared_skeleton", {})
        semaphore = asyncio.Semaphore(MATRIX_BATCH_CONCURRENCY)

        logger.info(
            f"Works builder: {len(cell_plans)} cell_plans, "
            f"shared_skeleton keys: {list(shared_skeleton.keys()) if shared_skeleton else 'EMPTY'}"
        )

        if not cell_plans:
            logger.error("No cell_plans to build! works_plan keys: %s", list(works_plan.keys()))
            return {
                "prompt_matrix": [],
                "prompt_templates": [],
                "matrix_dimensions": works_plan.get("matrix_dimensions", {}),
                "batch_rules": works_plan.get("batch_rules", {}),
                "usage_guide": works_plan.get("usage_guide", ""),
                "demo_outputs": [],
                "_uncertainty_summary": works_plan.get("_uncertainty_summary", {}),
            }

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
        for idx, r in enumerate(results):
            cells = r.get("prompt_cells", [])
            demos = r.get("demo_outputs", [])
            logger.info(f"Builder batch {idx}: {len(cells)} prompt_cells, {len(demos)} demos, keys={list(r.keys())}")
            all_cells.extend(cells)
            all_demos.extend(demos)

        logger.info(f"Works builder completed: {len(all_cells)} total prompt_cells, {len(all_demos)} demos")

        return {
            "prompt_matrix": all_cells,
            "prompt_templates": all_cells,  # backward compatibility
            "matrix_dimensions": works_plan.get("matrix_dimensions", {}),
            "batch_rules": works_plan.get("batch_rules", {}),
            "usage_guide": works_plan.get("usage_guide", ""),
            "demo_outputs": all_demos,
            "shared_skeleton": works_plan.get("shared_skeleton", {}),
            "_uncertainty_summary": works_plan.get("_uncertainty_summary", {}),
        }

    async def _run_vibe_loop(self, final_system: dict) -> dict:
        """Critic → Rewriter loop. Up to 2 iterations.

        网感不行 = system_prompt 设计有缺陷 → 重写 system_prompt（不是改 demo）。
        """
        max_iterations = 2
        prompt_cells = final_system.get("prompt_matrix", [])
        if not prompt_cells:
            logger.warning("Vibe loop skipped: no prompt_cells")
            return final_system

        shared_skeleton = final_system.get("shared_skeleton", {})

        for iteration in range(max_iterations):
            logger.info(f"Vibe loop iteration {iteration + 1}/{max_iterations}")

            # Critic input: only the fields critic needs
            critic_input = {
                "prompt_cells": [
                    {
                        "cell_id": c.get("cell_id"),
                        "direction_id": c.get("direction_id"),
                        "direction_name": c.get("direction_name"),
                        "platform": c.get("platform"),
                        "system_prompt": c.get("system_prompt", ""),
                        "demo_output": c.get("demo_output", ""),
                    }
                    for c in prompt_cells
                ]
            }
            try:
                critic_result = await self.vibe_critic.run(
                    critic_input, self.run_id, self.db
                )
            except Exception as e:
                logger.warning(f"Vibe critic failed ({e}), proceeding without critique")
                break

            failed = critic_result.get("failed_cells", [])
            if not failed:
                logger.info(f"Vibe critic passed all {len(prompt_cells)} cells "
                            f"on iteration {iteration + 1}")
                final_system["vibe_critic_result"] = critic_result
                return final_system

            logger.warning(
                f"Vibe critic iteration {iteration + 1}: "
                f"{len(failed)}/{len(prompt_cells)} cells failed"
            )

            # Build rewrite input — failed cells with their original prompts + critic feedback
            failed_ids = {f["cell_id"] for f in failed}
            critic_map = {f["cell_id"]: f for f in failed}
            failed_full_cells = [
                {
                    **c,
                    "rewrite_directives": critic_map[c["cell_id"]].get("rewrite_directives", ""),
                    "severity": critic_map[c["cell_id"]].get("severity", "fail"),
                    "taste_gap": critic_map[c["cell_id"]].get("taste_gap", ""),
                }
                for c in prompt_cells
                if c.get("cell_id") in failed_ids
            ]

            try:
                rewritten = await self.vibe_rewriter.run(
                    {
                        "failed_cells": failed_full_cells,
                        "shared_skeleton": shared_skeleton,
                    },
                    self.run_id,
                    self.db,
                )
            except Exception as e:
                logger.warning(f"Vibe rewriter failed ({e}), keeping original cells")
                break

            new_cells_by_id = {
                c["cell_id"]: c for c in rewritten.get("prompt_cells", [])
            }
            prompt_cells = [
                new_cells_by_id.get(c["cell_id"], c) for c in prompt_cells
            ]
            final_system["prompt_matrix"] = prompt_cells
            final_system["prompt_templates"] = prompt_cells

        # Out of iterations
        logger.warning(
            f"Vibe loop exhausted {max_iterations} iterations, "
            "proceeding with remaining issues"
        )
        return final_system


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
