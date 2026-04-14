"""Pipeline orchestrator — sequences all agents, handles clarification pauses."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
from datetime import datetime, timezone
from itertools import groupby

from db.supabase_client import SupabaseClient
from pipeline.config import (
    CELL_PLANNER_BATCH_SIZE,
    CELL_PLANNER_CONCURRENCY,
    CLARIFICATION_POLL_SECONDS,
    CLARIFICATION_TIMEOUT_SECONDS,
    MAX_CHANCELLERY_REJECTIONS,
    MAX_CLARIFICATION_PER_AGENT,
    MAX_FINAL_REJECTIONS,
    MATRIX_BATCH_CONCURRENCY,
    MATRIX_CELLS_PER_BATCH,
    PLATFORM_DEMO_LENGTH_DEFAULT,
    PLATFORM_DEMO_LENGTH_RANGES,
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

# All clarification timing is centralized in pipeline/config.py — these
# aliases preserve the short local names used throughout this file.
CLARIFICATION_POLL_INTERVAL = CLARIFICATION_POLL_SECONDS
CLARIFICATION_TIMEOUT = CLARIFICATION_TIMEOUT_SECONDS


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
        """Load outputs from already-completed stages in this run.

        Defensive: if a log row is missing `stage_name` (schema drift /
        partial write), skip it instead of crashing the whole pipeline on a
        KeyError at resume time.
        """
        logs = self.db.get_stage_logs(self.run_id)
        done: dict[str, dict] = {}
        for log in logs:
            if log.get("status") != "completed":
                continue
            output = log.get("output_data")
            if not output:
                continue
            name = log.get("stage_name")
            if not name:
                logger.warning(
                    f"[resume] skipping stage_log with missing stage_name: "
                    f"id={log.get('id', '?')}"
                )
                continue
            done[name] = output
        return done

    def _recover_completed_cells(
        self, stage_name: str, field: str
    ) -> tuple[dict[str, dict], list[dict]]:
        """Scan stage_logs for all successful batches of the given stage_name
        in this run, and collect every {cell_id: cell_dict} that was produced.

        Used for cell-level resume: when the pipeline failed mid-batch, we
        don't want to re-run the batches that already succeeded. This walks
        every successful ministry_works_builder (or cell_planner) stage_log,
        extracts the cells from output_data[field], and de-duplicates by
        cell_id (later successful log wins).

        Returns: (recovered_cells_by_id, recovered_demo_outputs)
        """
        logs = self.db.get_stage_logs(self.run_id)
        recovered: dict[str, dict] = {}
        demo_outputs: list[dict] = []
        for log in logs:
            if log.get("stage_name") != stage_name:
                continue
            if log.get("status") != "completed":
                continue
            output = log.get("output_data") or {}
            for item in output.get(field, []) or []:
                if isinstance(item, dict) and item.get("cell_id"):
                    recovered[item["cell_id"]] = item
            # demo_outputs is builder-specific but harmless to collect for planner too
            for d in output.get("demo_outputs", []) or []:
                if isinstance(d, dict):
                    demo_outputs.append(d)
        return recovered, demo_outputs

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

        raise TimeoutError(
            f"Clarification request timed out after "
            f"{CLARIFICATION_TIMEOUT} seconds "
            f"(~{CLARIFICATION_TIMEOUT // 60} minutes)"
        )

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

            # Revision mode: if chancellery_final rejected this run and the user
            # clicked "应用修订意见并重跑", revise_and_resume_pipeline_in_background
            # stored the review feedback in project.brief._revision_context and
            # deleted the downstream stage_logs. Load it here so we can inject
            # it into the works agents' inputs.
            _project_brief = (self.db.get_project(self.project_id) or {}).get("brief") or {}
            self._revision_context: dict | None = _project_brief.get("_revision_context")
            if self._revision_context:
                logger.info(
                    f"[revise] revision mode active, "
                    f"{len(self._revision_context.get('mandatory_revisions', []))} "
                    f"mandatory_revisions to address"
                )

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
                if self._revision_context:
                    works_planner_input["_revision_directives"] = self._revision_context
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
            # Track review round for the final-review force-pass mechanism.
            # Round 1 = fresh run. Round ≥ 2 = user applied revisions at least
            # once; revise_and_resume_pipeline_in_background stored the round
            # counter + prior review in _revision_context.
            _rc = self._revision_context or {}
            _final_round = int(_rc.get("round", 1)) if _rc else 1
            _prior_review = _rc.get("prior_review") if _rc else None
            final_review = await self.chancellery.run_final_review(
                final_system,
                plan,
                structured_brief,
                self.run_id,
                self.db,
                round_number=_final_round,
                prior_review=_prior_review,
            )

            # 6.5 Safety: if chancellery flagged revision_required but didn't
            # populate mandatory_revisions / revision_instructions, synthesize
            # fallback content from review_dimensions so the revision loop has
            # something actionable to feed back into the works agents.
            _v = (final_review or {}).get("verdict", "").strip().lower()
            if _v in ("revision_required", "rejected"):
                _existing_revs = (final_review or {}).get("mandatory_revisions", []) or []
                _existing_instr = (final_review or {}).get("revision_instructions", "") or ""
                if not _existing_revs or not _existing_instr:
                    synth_revs, synth_instr = _synthesize_revisions_from_review(final_review)
                    if synth_revs and not _existing_revs:
                        final_review["mandatory_revisions"] = synth_revs
                        logger.warning(
                            f"[chancellery_final] synthesized {len(synth_revs)} "
                            f"mandatory_revisions from review_dimensions because model "
                            f"returned empty list alongside verdict={_v!r}"
                        )
                    if synth_instr and not _existing_instr:
                        final_review["revision_instructions"] = synth_instr
                        logger.warning(
                            "[chancellery_final] synthesized revision_instructions "
                            "from review_dimensions + suggestions"
                        )

            # 7. Save output (always — user wants to see partial output even on revision_required)
            self.db.save_output(self.run_id, final_system, final_review)

            # 8. Determine final status from chancellery_final verdict
            verdict = (final_review or {}).get("verdict", "").strip().lower()
            if verdict == "approved":
                final_status = "completed"
                logger.info(f"Pipeline {self.run_id} completed (终审 approved)")
                # Clear stale revision_context from project.brief so next run is clean
                if self._revision_context:
                    project = self.db.get_project(self.project_id) or {}
                    brief = project.get("brief") or {}
                    if "_revision_context" in brief:
                        brief.pop("_revision_context", None)
                        self.db.update_project(self.project_id, brief=brief)
                        logger.info("[revise] cleared _revision_context after approval")
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
        """Run first 5 ministries in parallel. Any failure aborts the pipeline.

        Context-slimming: the 5 non-works ministries don't need the full plan
        (matrix_skeleton / module_plan / platform_direction_matrix are all
        downstream concerns). They only reference tactical_directions /
        target_platforms / strategic_insight. Passing a slim plan cuts 5 ×
        ~30KB of redundant input tokens per run. Dispatcher's task.context
        already surfaces per-ministry direction info, so this is safe.
        """
        task_map = tasks.get("tasks", {})
        done = done or {}

        # Extract only the fields the 5 non-works ministries actually read.
        # If a field is missing, it's fine — the prompt will just not get it.
        slim_plan = {
            k: plan[k]
            for k in ("tactical_directions", "target_platforms", "strategic_insight", "system_name")
            if k in plan
        }

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
                "plan": slim_plan,
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

        Splits active_cells into batches and runs them in parallel.
        Each batch receives shared_skeleton + ministry_outputs + its cell subset.
        Validates per-batch return count and retries with stricter prompt if short.
        """
        logger.info(
            f"[cell_planners] plan keys: {list(plan.keys())}, "
            f"matrix_skeleton type: {type(plan.get('matrix_skeleton'))}, "
            f"active_cells: {plan.get('matrix_skeleton', {}).get('active_cells', 'MISSING')!r:.200s}, "
            f"tactical_directions count: {len(plan.get('tactical_directions', []))}, "
            f"target_platforms: {plan.get('target_platforms', [])}"
        )

        active_cells = plan.get("matrix_skeleton", {}).get("active_cells", []) or []

        # Compute the EXPECTED set of cells from directions × platforms − excluded.
        # This is the source of truth — if secretariat's active_cells doesn't match,
        # reconstruct from D × P. (Common failure: model only emits D1 cells.)
        directions = plan.get("tactical_directions", []) or []
        platforms = plan.get("target_platforms", []) or []
        excluded = (plan.get("matrix_skeleton", {}) or {}).get("excluded_cells", []) or []

        def _norm_platform(p) -> str:
            return p if isinstance(p, str) else str(p)

        def _platform_key(p) -> str:
            return _norm_platform(p).replace(" ", "").lower()

        excluded_pairs = set()
        for ex in excluded:
            if isinstance(ex, dict):
                d_id = ex.get("direction_id")
                p_name = ex.get("platform")
                if d_id and p_name:
                    excluded_pairs.add((str(d_id), _platform_key(p_name)))

        expected_cells: list[dict] = []
        for d in directions:
            d_id = d.get("direction_id") if isinstance(d, dict) else d
            d_name = d.get("direction_name", "") if isinstance(d, dict) else ""
            d_paradigm = d.get("paradigm", "A_emotional_hook") if isinstance(d, dict) else "A_emotional_hook"
            if not d_id:
                continue
            for p in platforms:
                p_name = _norm_platform(p)
                p_key = _platform_key(p_name)
                if (str(d_id), p_key) in excluded_pairs:
                    continue
                expected_cells.append({
                    "cell_id": f"{d_id}_{p_key}",
                    "direction_id": str(d_id),
                    "direction_name": d_name,
                    "platform": p_name,
                    "paradigm": d_paradigm,
                })

        # Decide how to reconcile secretariat's active_cells vs our D×P
        # reconstruction. Historical bug: model sometimes only emits D1 cells
        # and omits the rest — reconstruction catches that. New concern (#6):
        # previously we replaced active_cells wholesale with the reconstruction
        # as soon as ANY direction was missing, which could force-plan
        # directions that secretariat intentionally dropped (without listing
        # them in excluded_cells). Fix: only ADD truly-missing (direction_id,
        # platform) pairs and keep everything else from secretariat, so
        # intentional non-coverage is preserved.
        active_dir_set = {str(c.get("direction_id")) for c in active_cells if isinstance(c, dict)}
        expected_dir_set = {str(c.get("direction_id")) for c in expected_cells}

        if not active_cells and expected_cells:
            # Case A: secretariat gave us nothing at all. Use reconstruction.
            logger.warning(
                f"matrix_skeleton.active_cells is empty, using reconstruction "
                f"({len(expected_cells)} cells from {len(directions)}×{len(platforms)})"
            )
            active_cells = expected_cells
        elif expected_cells:
            # Case B: secretariat gave us some cells. Only splice in D×P pairs
            # that secretariat literally didn't produce AND aren't in excluded.
            existing_pairs = {
                (str(c.get("direction_id")), _platform_key(c.get("platform", "")))
                for c in active_cells
                if isinstance(c, dict)
            }
            splice_in = [
                cell for cell in expected_cells
                if (cell["direction_id"], _platform_key(cell["platform"]))
                not in existing_pairs
            ]
            if splice_in:
                missing_dirs = sorted(expected_dir_set - active_dir_set)
                logger.warning(
                    f"matrix_skeleton.active_cells is missing "
                    f"{len(splice_in)} (direction×platform) pairs that "
                    f"tactical_directions × target_platforms expects "
                    f"(missing dirs: {missing_dirs}); splicing them in. "
                    f"Secretariat's original cells are preserved."
                )
                active_cells = list(active_cells) + splice_in

        if not active_cells:
            logger.error(
                "No active_cells found and could not reconstruct. "
                f"plan keys: {list(plan.keys())}, "
                f"matrix_skeleton: {plan.get('matrix_skeleton', 'MISSING')}"
            )
            return []

        shared_skeleton = works_arch.get("shared_skeleton", {})
        semaphore = asyncio.Semaphore(CELL_PLANNER_CONCURRENCY)

        # Cell-level resume: scan this run's stage_logs for any cell_plans that
        # already completed successfully, so a resume doesn't re-run batches
        # that already burned tokens producing good output.
        original_expected_order = list(active_cells)
        recovered_plans, _ = self._recover_completed_cells(
            "ministry_works_cell_planner", "cell_plans"
        )
        # Keep only recoveries whose cell_id is still in our expected set
        expected_ids_set = {c.get("cell_id") for c in active_cells}
        recovered_plans = {
            cid: plan for cid, plan in recovered_plans.items()
            if cid in expected_ids_set
        }
        if recovered_plans:
            logger.info(
                f"[cell_planner resume] recovered {len(recovered_plans)}/{len(active_cells)} "
                f"cell_plans from prior successful batches: {sorted(recovered_plans.keys())}"
            )
            active_cells = [
                c for c in active_cells
                if c.get("cell_id") not in recovered_plans
            ]
            if not active_cells:
                logger.info(
                    "[cell_planner resume] all cells already done, skipping entirely"
                )
                return [
                    recovered_plans[c.get("cell_id")]
                    for c in original_expected_order
                    if c.get("cell_id") in recovered_plans
                ]

        # Split into batches
        batches: list[list] = []
        for i in range(0, len(active_cells), CELL_PLANNER_BATCH_SIZE):
            batches.append(active_cells[i : i + CELL_PLANNER_BATCH_SIZE])

        logger.info(
            f"Cell planner: {len(active_cells)} cells → {len(batches)} batches "
            f"(size={CELL_PLANNER_BATCH_SIZE}, concurrency={CELL_PLANNER_CONCURRENCY})"
        )

        async def plan_single_cell(cell: dict, parent_batch_idx: int) -> dict | None:
            """Cell-level retry: run cell_planner with a single cell as input.
            Returns the cell_plan dict on success, None on failure.
            """
            cid = cell.get("cell_id")
            try:
                single_input = {
                    "active_cells": [cell],
                    "shared_skeleton": shared_skeleton,
                    "ministry_outputs": ministry_outputs,
                    "brief": brief,
                    "plan_summary": {
                        "tactical_directions": plan.get("tactical_directions", []),
                        "target_platforms": plan.get("target_platforms", []),
                    },
                    "_batch_info": {
                        "label": f"单cell修复 {cid}",
                        "round": "cell-retry",
                        "parent_batch": parent_batch_idx,
                        "cell_ids": [cid],
                    },
                    **({"_revision_directives": self._revision_context}
                       if self._revision_context else {}),
                    "_strict_contract": (
                        f"【单 cell 修复模式】上一批次 ({parent_batch_idx}) 漏掉了 cell {cid}。"
                        f"现在只给你一个 cell，你必须完整输出它的 cell_plan。"
                        f"输出的 cell_plans 数组长度必须是 1，cell_id 必须是 '{cid}'。"
                    ),
                }
                result = await self.works_cell_planner.run(
                    single_input, self.run_id, self.db
                )
                plans = result.get("cell_plans", []) or []
                for p in plans:
                    if isinstance(p, dict) and p.get("cell_id") == cid:
                        logger.info(f"[cell_planner cell-retry] recovered {cid}")
                        return p
                # Got a response but the right cell_id wasn't there — accept first
                # plan if we got one, with cell_id corrected
                if plans and isinstance(plans[0], dict):
                    plans[0]["cell_id"] = cid
                    plans[0]["direction_id"] = cell.get("direction_id", plans[0].get("direction_id", ""))
                    plans[0]["platform"] = cell.get("platform", plans[0].get("platform", ""))
                    logger.warning(
                        f"[cell_planner cell-retry] {cid} returned with wrong cell_id, "
                        f"forcing correction"
                    )
                    return plans[0]
                logger.error(f"[cell_planner cell-retry] {cid} returned empty plans")
                return None
            except Exception as e:
                logger.error(f"[cell_planner cell-retry] {cid} failed: {e!r}")
                return None

        async def plan_batch(batch: list, batch_idx: int) -> list[dict]:
            """Run one batch and validate output count.
            Strategy: batch call → batch retry → per-cell retries → hard fail.
            No more silent stubs.
            """
            async with semaphore:
                expected_ids = [c.get("cell_id") for c in batch]
                expected_n = len(batch)

                async def _call(extra_directive: str = "", round_label: str = "initial") -> dict:
                    batch_input = {
                        "active_cells": batch,
                        "shared_skeleton": shared_skeleton,
                        "ministry_outputs": ministry_outputs,
                        "brief": brief,
                        "plan_summary": {
                            "tactical_directions": plan.get("tactical_directions", []),
                            "target_platforms": plan.get("target_platforms", []),
                        },
                        "_batch_info": {
                            "label": f"批次 {batch_idx + 1} · {round_label}",
                            "round": round_label,
                            "batch_idx": batch_idx,
                            "cell_ids": expected_ids,
                        },
                        "_strict_contract": (
                            f"你必须为输入中的每一个 active_cell 都返回一个对应的 cell_plan。"
                            f"输入了 {expected_n} 个 cells: {expected_ids}。"
                            f"输出的 cell_plans 数组长度必须等于 {expected_n}，"
                            f"每个 cell_plan 的 cell_id 必须严格对应输入的 cell_id。"
                            f"不允许跳过任何 cell，不允许合并 cell，不允许少返回。"
                            + (f" 注意：{extra_directive}" if extra_directive else "")
                        ),
                    }
                    if self._revision_context:
                        batch_input["_revision_directives"] = self._revision_context
                    return await self.works_cell_planner.run(
                        batch_input, self.run_id, self.db
                    )

                # Round 1: batch call
                result = await _call()
                returned = result.get("cell_plans", []) or []
                merged_by_id: dict[str, dict] = {
                    p["cell_id"]: p for p in returned
                    if isinstance(p, dict) and p.get("cell_id") in expected_ids
                }
                missing_ids = [cid for cid in expected_ids if cid not in merged_by_id]

                # Round 2: batch retry (only if anything missing)
                if missing_ids:
                    logger.warning(
                        f"[cell_planner batch {batch_idx}] short return: "
                        f"expected {expected_n} ({expected_ids}), "
                        f"got {sorted(merged_by_id.keys())}. "
                        f"Missing: {missing_ids}. Round 2: batch retry..."
                    )
                    try:
                        retry_result = await _call(
                            f"上一次只返回了 {sorted(merged_by_id.keys())}，"
                            f"漏了 {missing_ids}，必须把所有 {expected_n} 个都返回。",
                            round_label="batch-retry",
                        )
                        for p in retry_result.get("cell_plans", []) or []:
                            if isinstance(p, dict) and p.get("cell_id") in expected_ids:
                                merged_by_id[p["cell_id"]] = p
                        missing_ids = [cid for cid in expected_ids if cid not in merged_by_id]
                    except Exception as e:
                        logger.error(
                            f"[cell_planner batch {batch_idx}] batch retry exception: {e!r}"
                        )

                # Round 3: per-cell retry for stubborn missing cells
                if missing_ids:
                    logger.warning(
                        f"[cell_planner batch {batch_idx}] Round 3: per-cell retry for "
                        f"{missing_ids}"
                    )
                    by_id = {c.get("cell_id"): c for c in batch}
                    cell_retry_tasks = [
                        plan_single_cell(by_id[cid], batch_idx)
                        for cid in missing_ids
                        if cid in by_id
                    ]
                    cell_retry_results = await asyncio.gather(*cell_retry_tasks)
                    for cid, single_plan in zip(missing_ids, cell_retry_results):
                        if single_plan is not None:
                            merged_by_id[cid] = single_plan
                    missing_ids = [cid for cid in expected_ids if cid not in merged_by_id]

                if missing_ids:
                    # All three rounds failed — surface a clear error.
                    # We deliberately do NOT stub-fill anymore: stubs were creating
                    # garbage that downstream stages couldn't distinguish from real
                    # output, and final review correctly rejected them as incomplete.
                    raise RuntimeError(
                        f"[cell_planner batch {batch_idx}] cell_planner 三轮尝试后仍缺失 "
                        f"{missing_ids}（batch 调用 → batch 重试 → 单 cell 重试都失败）。"
                        f"已成功的 cell: {sorted(merged_by_id.keys())}。"
                        f"请检查 stage_logs 看每次调用的实际返回。"
                    )

                # Return in the same order as expected_ids
                return [merged_by_id[cid] for cid in expected_ids]

        results = await asyncio.gather(
            *[plan_batch(b, i) for i, b in enumerate(batches)]
        )

        # Merge newly-generated batches
        new_plans_by_id: dict[str, dict] = {}
        for r in results:
            for p in r:
                if isinstance(p, dict) and p.get("cell_id"):
                    new_plans_by_id[p["cell_id"]] = p

        # Combine recovered (from prior successful batches) + newly-generated,
        # preserving the original expected order.
        all_cell_plans: list[dict] = []
        for c in original_expected_order:
            cid = c.get("cell_id")
            if cid in recovered_plans:
                all_cell_plans.append(recovered_plans[cid])
            elif cid in new_plans_by_id:
                all_cell_plans.append(new_plans_by_id[cid])

        logger.info(
            f"Cell planner completed: {len(all_cell_plans)}/{len(original_expected_order)} "
            f"cell_plans (recovered: {len(recovered_plans)}, new: {len(new_plans_by_id)})"
        )

        # Sanity check (should never fire — batches hard fail on missing)
        if len(all_cell_plans) != len(original_expected_order):
            missing = [
                c.get("cell_id") for c in original_expected_order
                if c.get("cell_id") not in recovered_plans
                and c.get("cell_id") not in new_plans_by_id
            ]
            raise RuntimeError(
                f"工部·格子规划数量不匹配：期望 {len(original_expected_order)} 个，"
                f"实际产出 {len(all_cell_plans)} 个。缺失 cell_id: {missing}。"
            )

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
                "matrix_dimensions": works_plan.get("matrix_dimensions", {}),
                "demo_outputs": [],
                "_uncertainty_summary": works_plan.get("_uncertainty_summary", {}),
            }

        # Cell-level resume: scan prior successful builder batches for cells
        # that already completed, so we don't re-run them.
        original_expected_order = list(cell_plans)
        expected_ids_set = {c.get("cell_id") for c in cell_plans}
        recovered_cells, recovered_demos = self._recover_completed_cells(
            "ministry_works_builder", "prompt_cells"
        )
        recovered_cells = {
            cid: cell for cid, cell in recovered_cells.items()
            if cid in expected_ids_set
        }

        # Revision-aware recovery: if a revision cycle is active, exclude cells
        # whose direction_id was called out in mandatory_revisions. These need
        # to be re-built even though they have prior successful stage_logs.
        revision_ctx = getattr(self, "_revision_context", None) or {}
        affected_dirs = set(revision_ctx.get("affected_direction_ids", []))
        is_global = revision_ctx.get("is_global_revision", False)

        if is_global and recovered_cells:
            logger.info(
                "[builder resume] global revision active → discarding all "
                f"{len(recovered_cells)} recovered cells, re-building everything"
            )
            recovered_cells = {}
            recovered_demos = []
        elif affected_dirs and recovered_cells:
            before_count = len(recovered_cells)
            recovered_cells = {
                cid: cell for cid, cell in recovered_cells.items()
                if not any(cid.startswith(f"{d}_") for d in affected_dirs)
            }
            dropped = before_count - len(recovered_cells)
            if dropped:
                logger.info(
                    f"[builder resume] revision targets directions {sorted(affected_dirs)} "
                    f"→ dropped {dropped} recovered cells that need re-building, "
                    f"keeping {len(recovered_cells)} unaffected cells"
                )

        if recovered_cells:
            logger.info(
                f"[builder resume] recovered {len(recovered_cells)}/{len(original_expected_order)} "
                f"prompt_cells from prior successful batches: {sorted(recovered_cells.keys())}"
            )
            cell_plans = [
                c for c in cell_plans
                if c.get("cell_id") not in recovered_cells
            ]
            if not cell_plans:
                logger.info(
                    "[builder resume] all cells already done, skipping entirely"
                )
                all_cells_ordered = [
                    recovered_cells[c.get("cell_id")]
                    for c in original_expected_order
                    if c.get("cell_id") in recovered_cells
                ]
                return {
                    "prompt_matrix": all_cells_ordered,
                    "matrix_dimensions": works_plan.get("matrix_dimensions", {}),
                    "demo_outputs": recovered_demos,
                    "shared_skeleton": works_plan.get("shared_skeleton", {}),
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

        async def build_single_cell(cell_plan: dict, parent_batch_idx: int) -> tuple[dict | None, list[dict]]:
            """Cell-level retry: build a single cell with focused input."""
            cid = cell_plan.get("cell_id")
            try:
                single_input = {
                    "cell_plans": [cell_plan],
                    "shared_skeleton": shared_skeleton,
                    "_batch_info": {
                        "label": f"单cell修复 {cid}",
                        "round": "cell-retry",
                        "parent_batch": parent_batch_idx,
                        "cell_ids": [cid],
                    },
                    **({"_revision_directives": self._revision_context}
                       if self._revision_context else {}),
                    "_strict_contract": (
                        f"【单 cell 修复模式】上一批次 ({parent_batch_idx}) 漏掉了 cell {cid}。"
                        f"现在只给你一个 cell_plan，你必须完整输出它的 prompt_cell + demo_output。"
                        f"输出的 prompt_cells 数组长度必须是 1，cell_id 必须是 '{cid}'。"
                    ),
                }
                result = await self.works_builder.run(
                    single_input, self.run_id, self.db
                )
                cells = result.get("prompt_cells", []) or []
                demos = result.get("demo_outputs", []) or []
                for p in cells:
                    if isinstance(p, dict) and p.get("cell_id") == cid:
                        logger.info(f"[builder cell-retry] recovered {cid}")
                        return p, demos
                # Wrong cell_id but got something — accept first and force-correct
                if cells and isinstance(cells[0], dict):
                    cells[0]["cell_id"] = cid
                    cells[0]["direction_id"] = cell_plan.get("direction_id", cells[0].get("direction_id", ""))
                    cells[0]["platform"] = cell_plan.get("platform", cells[0].get("platform", ""))
                    logger.warning(
                        f"[builder cell-retry] {cid} returned with wrong cell_id, forcing correction"
                    )
                    return cells[0], demos
                logger.error(f"[builder cell-retry] {cid} returned empty prompt_cells")
                return None, demos
            except Exception as e:
                logger.error(f"[builder cell-retry] {cid} failed: {e!r}")
                return None, []

        async def build_batch(batch: list[dict], batch_idx: int) -> tuple[list[dict], list[dict]]:
            """Run one builder batch.
            Strategy: batch call → batch retry → per-cell retries → hard fail.
            No more silent stubs.
            """
            async with semaphore:
                expected_ids = [c.get("cell_id") for c in batch]
                expected_n = len(batch)

                async def _call(extra_directive: str = "", round_label: str = "initial") -> dict:
                    builder_input = {
                        "cell_plans": batch,
                        "shared_skeleton": shared_skeleton,
                        "_batch_info": {
                            "label": f"批次 {batch_idx + 1} · {round_label}",
                            "round": round_label,
                            "batch_idx": batch_idx,
                            "cell_ids": expected_ids,
                        },
                        "_strict_contract": (
                            f"你必须为输入中的每一个 cell_plan 都返回一个对应的 prompt_cell。"
                            f"输入了 {expected_n} 个 cell_plans: {expected_ids}。"
                            f"输出的 prompt_cells 数组长度必须等于 {expected_n}，"
                            f"每个 prompt_cell 的 cell_id 必须严格对应输入的 cell_id。"
                            f"不允许跳过任何 cell，不允许合并 cell。"
                            + (f" 注意：{extra_directive}" if extra_directive else "")
                        ),
                    }
                    if self._revision_context:
                        builder_input["_revision_directives"] = self._revision_context
                    return await self.works_builder.run(
                        builder_input, self.run_id, self.db
                    )

                def _filter_invalid(candidates: dict[str, dict]) -> dict[str, dict]:
                    """Drop any cell that fails quality validation,
                    so it gets treated as missing and retried."""
                    out: dict[str, dict] = {}
                    for cid, cell in candidates.items():
                        is_valid, cell_issues = _validate_prompt_cell(cell)
                        if not is_valid:
                            logger.warning(
                                f"[builder batch {batch_idx}] cell {cid} failed validation "
                                f"({len(cell_issues)} issues) — treating as missing to "
                                f"trigger retry. Issues: {cell_issues[:3]}"
                            )
                        else:
                            if cell_issues:
                                # Soft issues: log but keep the cell
                                logger.info(
                                    f"[builder batch {batch_idx}] cell {cid} has "
                                    f"{len(cell_issues)} soft issues (keeping): "
                                    f"{cell_issues[:3]}"
                                )
                            out[cid] = cell
                    return out

                # Round 1: batch call
                result = await _call()
                merged_by_id: dict[str, dict] = {}
                for p in result.get("prompt_cells", []) or []:
                    if isinstance(p, dict) and p.get("cell_id") in expected_ids:
                        merged_by_id[p["cell_id"]] = p
                merged_by_id = _filter_invalid(merged_by_id)
                merged_demos: list[dict] = list(result.get("demo_outputs", []) or [])
                missing_ids = [cid for cid in expected_ids if cid not in merged_by_id]

                # Round 2: batch retry
                if missing_ids:
                    logger.warning(
                        f"[builder batch {batch_idx}] short/truncated return: "
                        f"expected {expected_n} ({expected_ids}), "
                        f"got {sorted(merged_by_id.keys())}. "
                        f"Missing/truncated: {missing_ids}. Round 2: batch retry..."
                    )
                    try:
                        retry_result = await _call(
                            f"上一次只返回了 {sorted(merged_by_id.keys())}，"
                            f"漏了 {missing_ids}（包括截断不完整的 cell）。必须把所有 "
                            f"{expected_n} 个 cell 都完整返回，每个 cell 的 system_prompt "
                            f"必须以完整句子结尾（。！？」）。",
                            round_label="batch-retry",
                        )
                        new_candidates: dict[str, dict] = {}
                        for p in retry_result.get("prompt_cells", []) or []:
                            if isinstance(p, dict) and p.get("cell_id") in expected_ids:
                                new_candidates[p["cell_id"]] = p
                        new_candidates = _filter_invalid(new_candidates)
                        merged_by_id.update(new_candidates)
                        merged_demos.extend(retry_result.get("demo_outputs", []) or [])
                        missing_ids = [cid for cid in expected_ids if cid not in merged_by_id]
                    except Exception as e:
                        logger.error(
                            f"[builder batch {batch_idx}] batch retry exception: {e!r}"
                        )

                # Round 3: per-cell retry
                if missing_ids:
                    logger.warning(
                        f"[builder batch {batch_idx}] Round 3: per-cell retry for {missing_ids}"
                    )
                    by_id = {c.get("cell_id"): c for c in batch}
                    cell_retry_tasks = [
                        build_single_cell(by_id[cid], batch_idx)
                        for cid in missing_ids
                        if cid in by_id
                    ]
                    cell_retry_results = await asyncio.gather(*cell_retry_tasks)
                    for cid, (single_cell, single_demos) in zip(missing_ids, cell_retry_results):
                        if single_cell is not None:
                            is_valid, cell_issues = _validate_prompt_cell(single_cell)
                            if not is_valid:
                                logger.warning(
                                    f"[builder cell-retry] {cid} still has issues after "
                                    f"single-cell call: {cell_issues[:3]} — accepting anyway (best effort)"
                                )
                            merged_by_id[cid] = single_cell
                        merged_demos.extend(single_demos)
                    missing_ids = [cid for cid in expected_ids if cid not in merged_by_id]

                if missing_ids:
                    raise RuntimeError(
                        f"[builder batch {batch_idx}] works_builder 三轮尝试后仍缺失 "
                        f"{missing_ids}（batch 调用 → batch 重试 → 单 cell 重试都失败）。"
                        f"已成功的 cell: {sorted(merged_by_id.keys())}。"
                        f"请检查 stage_logs 看每次调用的实际返回。"
                    )

                # Return in expected order
                return [merged_by_id[cid] for cid in expected_ids], merged_demos

        results = await asyncio.gather(
            *[build_batch(b, i) for i, b in enumerate(batches)]
        )

        # Collect newly-generated cells/demos from this run
        new_cells_by_id: dict[str, dict] = {}
        new_demos: list[dict] = []
        for idx, (cells, demos) in enumerate(results):
            logger.info(
                f"Builder batch {idx}: {len(cells)} prompt_cells, {len(demos)} demos"
            )
            for c in cells:
                if isinstance(c, dict) and c.get("cell_id"):
                    new_cells_by_id[c["cell_id"]] = c
            new_demos.extend(demos)

        # Merge recovered (from prior successful batches) + newly-generated,
        # preserving the original expected order.
        all_cells: list[dict] = []
        for c in original_expected_order:
            cid = c.get("cell_id")
            if cid in recovered_cells:
                all_cells.append(recovered_cells[cid])
            elif cid in new_cells_by_id:
                all_cells.append(new_cells_by_id[cid])

        all_demos = recovered_demos + new_demos

        logger.info(
            f"Works builder completed: {len(all_cells)}/{len(original_expected_order)} "
            f"total prompt_cells (recovered: {len(recovered_cells)}, new: {len(new_cells_by_id)}), "
            f"{len(all_demos)} demos"
        )

        # Sanity check (should never fire — batches hard fail above)
        if len(all_cells) != len(original_expected_order):
            missing = [
                c.get("cell_id") for c in original_expected_order
                if c.get("cell_id") not in recovered_cells
                and c.get("cell_id") not in new_cells_by_id
            ]
            raise RuntimeError(
                f"工部·构建数量不匹配：期望 {len(original_expected_order)} 个，"
                f"实际产出 {len(all_cells)} 个。缺失 cell_id: {missing}。"
            )

        return {
            "prompt_matrix": all_cells,
            "matrix_dimensions": works_plan.get("matrix_dimensions", {}),
            "demo_outputs": all_demos,
            "shared_skeleton": works_plan.get("shared_skeleton", {}),
            "_uncertainty_summary": works_plan.get("_uncertainty_summary", {}),
        }

    async def _run_vibe_loop(self, final_system: dict) -> dict:
        """Critic → Rewriter loop. Up to 2 iterations.

        网感不行 = system_prompt 设计有缺陷 → 重写 system_prompt（不是改 demo）。
        Round 2+ only re-evaluates cells that were rewritten, not the entire matrix.
        """
        max_iterations = 2
        prompt_cells = final_system.get("prompt_matrix", [])
        if not prompt_cells:
            logger.warning("Vibe loop skipped: no prompt_cells")
            return final_system

        shared_skeleton = final_system.get("shared_skeleton", {})
        # Track which cell_ids were rewritten so round 2 only re-evaluates those
        rewritten_ids: set[str] = set()

        for iteration in range(max_iterations):
            logger.info(f"Vibe loop iteration {iteration + 1}/{max_iterations}")

            # Round 1: evaluate ALL cells. Round 2+: only evaluate rewritten cells.
            if iteration == 0:
                cells_to_critique = prompt_cells
            else:
                cells_to_critique = [
                    c for c in prompt_cells
                    if c.get("cell_id") in rewritten_ids
                ]
                if not cells_to_critique:
                    logger.info("Vibe loop: no rewritten cells to re-evaluate, done")
                    break
                logger.info(
                    f"Vibe loop round {iteration + 1}: only re-evaluating "
                    f"{len(cells_to_critique)} rewritten cells "
                    f"(skipping {len(prompt_cells) - len(cells_to_critique)} unchanged)"
                )

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
                    for c in cells_to_critique
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
            rewritten_ids = set(new_cells_by_id.keys())
            logger.info(
                f"Vibe rewriter rewrote {len(rewritten_ids)} cells: "
                f"{sorted(rewritten_ids)}"
            )
            prompt_cells = [
                new_cells_by_id.get(c["cell_id"], c) for c in prompt_cells
            ]
            final_system["prompt_matrix"] = prompt_cells

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


def _validate_prompt_cell(cell: dict) -> tuple[bool, list[str]]:
    """Validate a works_builder prompt_cell for quality issues that would
    otherwise only be caught by chancellery_final — shifting quality checks
    LEFT to catch problems at the cell level immediately after building.

    Checks performed (zero LLM cost, pure logic):
    1. Required fields present and non-empty
    2. system_prompt not truncated (length + ending heuristic)
    3. system_prompt contains essential sections (compliance, keywords, differentiation)
    4. demo_output within platform length bounds
    5. media_brief and comment_seeds present

    Returns (is_valid, list_of_issues). Empty issues = valid.
    """
    issues: list[str] = []
    if not isinstance(cell, dict):
        return False, ["cell 不是 dict"]

    cid = cell.get("cell_id", "?")
    platform = (cell.get("platform") or "").lower()

    # ── 1. Required fields ─────────────────────────────────────────────
    required_fields = [
        "cell_id", "direction_id", "platform",
        "system_prompt", "user_prompt_template", "demo_output",
    ]
    for field in required_fields:
        val = cell.get(field)
        if not val or (isinstance(val, str) and not val.strip()):
            issues.append(f"{cid}: 必填字段 {field} 为空")

    # media_brief and comment_seeds: warn if missing but don't hard-fail
    # (some older prompts may not produce them)
    if not cell.get("media_brief"):
        issues.append(f"{cid}: media_brief 缺失")
    if not cell.get("comment_seeds"):
        issues.append(f"{cid}: comment_seeds 缺失")

    # ── 2. system_prompt truncation + minimum length ───────────────────
    sp = (cell.get("system_prompt") or "").strip()
    if sp:
        if len(sp) < 500:
            issues.append(f"{cid}: system_prompt 过短（{len(sp)} 字符 < 500）")

        clean_endings = (
            "。", "！", "？", "」", "』", "}", "】", "）", ")",
            ".", "!", "?", "\"", "'", "\u201d", "\u2019",
        )
        if not sp.endswith(clean_endings):
            tail = sp[-40:].replace("\n", " ")
            issues.append(f"{cid}: system_prompt 结尾不完整（末尾: ...{tail!r}）")

    # ── 3. system_prompt must contain essential sections ────────────────
    #
    # These checks mirror what chancellery_final's final_review section in
    # chancellery.md evaluates, so works_builder can self-catch the same
    # class of issues locally (no LLM cost) instead of getting punted back
    # by the reviewer three rounds later.
    #
    # Principle: only hard-fail on items chancellery explicitly flags.
    # Stylistic nits (tone/naturalness) still belong to the reviewer.
    if sp:
        essential_keywords = {
            "合规": "合规/compliance 规则",
            "关键词": "关键词植入指令",
            "禁止": "反 AI 腔禁用清单",
        }
        for keyword, description in essential_keywords.items():
            if keyword not in sp:
                issues.append(f"{cid}: system_prompt 缺少「{description}」（找不到 '{keyword}'）")

        # 5 differentiation pools — works_builder.md:17-23 explicitly says
        # "5 个池必须全部内置，缺一个都算不合格". We accept either the
        # Chinese pool name OR its English key as proof the pool is present.
        # Count distinct pools found; < 4 = hard fail (matches chancellery's
        # "缺一个都算不合格" but with 1-pool tolerance for keyword variations).
        pool_aliases = {
            "叙事结构": ["叙事结构", "叙事", "narrative_structure", "narrative"],
            "开头切入": ["开头切入", "开头", "opening_angle", "opening"],
            "情绪基调": ["情绪基调", "情绪", "emotion_baseline", "emotion"],
            "结尾方式": ["结尾方式", "结尾", "closing_style", "closing"],
            "信息密度": ["信息密度", "密度", "information_density", "密度池"],
        }
        pools_found = [
            name for name, aliases in pool_aliases.items()
            if any(a in sp for a in aliases)
        ]
        pools_missing = [n for n in pool_aliases if n not in pools_found]
        if len(pools_found) < 4:
            issues.append(
                f"{cid}: system_prompt 五个差异化池只命中 {len(pools_found)}/5，"
                f"缺失: {'/'.join(pools_missing) or '无'}"
            )
        elif len(pools_found) == 4:
            # soft warning — 4 out of 5 still lets builder pass but we note it
            issues.append(
                f"{cid}: system_prompt 差异化池命中 4/5（缺: {'/'.join(pools_missing)}），"
                f"建议补齐但不强制重试"
            )

        # Batch generation rules — works_builder.md:24-29 requires 人设轮换
        # + 差异化旋钮轮转 to be baked into every system_prompt. Chancellery
        # has historically rejected for missing these. Mark as SOFT so we
        # surface it but don't spin the builder into 3-round retry hell over
        # wording variations (轮换 / 轮流 / 切换 / 交替 …).
        batch_rule_aliases = [
            "人设轮换", "轮流分配", "persona_rotation", "人设切换",
            "轮流", "交替", "每篇换",
        ]
        if not any(a in sp for a in batch_rule_aliases):
            issues.append(
                f"{cid}: system_prompt 缺少「人设轮换规则」"
                f"（批量生成时必须指定，建议补齐但不强制重试）"
            )

        # Persona integration — if system_prompt doesn't mention 人设 / persona
        # at all, it's broken regardless of paradigm
        if "人设" not in sp and "persona" not in sp.lower():
            issues.append(f"{cid}: system_prompt 完全没有人设相关内容")

    # ── 4. demo_output platform-specific length check ──────────────────
    demo = (cell.get("demo_output") or "").strip()
    if demo:
        # Platform ranges live in pipeline/config.py so operators can tune
        # them without code changes when platform content norms shift.
        min_len, max_len = PLATFORM_DEMO_LENGTH_DEFAULT
        for plat_key, (pmin, pmax) in PLATFORM_DEMO_LENGTH_RANGES.items():
            if plat_key in platform:
                min_len, max_len = pmin, pmax
                break

        if len(demo) < min_len:
            issues.append(
                f"{cid}: demo_output 过短（{len(demo)} 字符 < {min_len} 平台下限）"
            )
        if len(demo) > max_len * 1.5:  # 50% tolerance
            issues.append(
                f"{cid}: demo_output 超长（{len(demo)} 字符 > {int(max_len * 1.5)} 平台上限×1.5）"
            )

        # demo truncation check
        if len(demo) > 50 and not demo.endswith(clean_endings):
            tail = demo[-40:].replace("\n", " ")
            issues.append(f"{cid}: demo_output 结尾不完整（末尾: ...{tail!r}）")

        # AI-cliché blacklist — works_builder.md:60 explicitly lists these
        # as forbidden phrases. If the demo contains them, chancellery will
        # reliably reject for "AI 味". Catch locally to avoid the round trip.
        ai_cliches = [
            "效果显著", "性价比高", "值得推荐", "适合所有人", "温和不刺激",
            "希望对你有帮助", "综上所述", "在如今", "让我们一起", "姐妹们冲",
            "快快收藏",
        ]
        hit_cliches = [c for c in ai_cliches if c in demo]
        if hit_cliches:
            issues.append(
                f"{cid}: demo_output 命中 AI 空话黑名单 {hit_cliches}（works_builder 禁用项）"
            )

    # Classify: hard issues (must retry) vs soft issues (warn only).
    # Soft markers are written in Chinese into the issue text; anything that
    # contains a soft marker is downgraded to warn-only.
    soft_markers = ["建议补齐但不强制重试", "media_brief 缺失", "comment_seeds 缺失"]
    hard_issues = [
        i for i in issues
        if not any(sm in i for sm in soft_markers)
    ]
    soft_issues = [i for i in issues if i not in hard_issues]

    is_valid = len(hard_issues) == 0
    return is_valid, issues


def _synthesize_revisions_from_review(final_review: dict) -> tuple[list[str], str]:
    """Synthesize (mandatory_revisions, revision_instructions) from a chancellery_final
    output when the model returned them empty. Used both by orchestrator.run() step 6.5
    (live synthesis right after chancellery runs) and by revise_and_resume_pipeline
    (retroactive synthesis when the user clicks 应用修订 on an already-stored review).

    Pulls from review_dimensions (low-scoring ones) + suggestions.
    Returns ([], "") if nothing can be synthesized.
    """
    synthetic_revs: list[str] = []
    synthetic_instr_parts: list[str] = []

    dims = (final_review or {}).get("review_dimensions", {}) or {}
    for dim_name, dim_data in dims.items():
        if not isinstance(dim_data, dict):
            continue
        score = dim_data.get("score", 5)
        issues = (dim_data.get("issues") or "").strip()
        if issues and score < 5:
            synthetic_revs.append(
                f"【来自 review_dimensions.{dim_name} 的 {score}/5 分问题】{issues}"
            )
            synthetic_instr_parts.append(f"- {dim_name} ({score}/5): {issues}")

    for s in (final_review or {}).get("suggestions", []) or []:
        if isinstance(s, str) and s.strip():
            synthetic_instr_parts.append(f"- 建议: {s.strip()}")

    synthetic_instr = ""
    if synthetic_instr_parts:
        synthetic_instr = (
            "（下列修订由 orchestrator 从 review_dimensions + suggestions 自动合成——"
            "chancellery 返回时未填写具体的 mandatory_revisions / revision_instructions）\n\n"
            + "\n".join(synthetic_instr_parts)
        )

    return synthetic_revs, synthetic_instr


def resume_pipeline_in_background(project_id: str, run_id: str, db: SupabaseClient):
    """Resume a failed pipeline run — reuses existing run_id and skips completed stages."""
    # Reset run status so UI shows it as running again
    db.update_pipeline_run(run_id, status="running", completed_at=None)
    return start_pipeline_in_background(project_id, run_id, db)


def revise_and_resume_pipeline_in_background(
    project_id: str, run_id: str, db: SupabaseClient
):
    """Apply chancellery_final's revision_instructions and re-run the downstream.

    This is the proper fix for the needs_revision → "rerun from scratch" dead end.
    When 终审 rejects a run with mandatory_revisions:
      1. Load the latest chancellery_final output from stage_logs
      2. Store its revision_instructions + mandatory_revisions into project.brief
         under _revision_context (orchestrator.run() reads this and injects it
         into the works agents' inputs as _revision_directives)
      3. Delete all downstream stage_logs (works_arch + cell_planner + builder
         + vibe_critic + vibe_rewriter + chancellery_final) so resume re-runs them
      4. Trigger the normal resume path

    Preserves: crown_prince, secretariat, chancellery_*, dispatcher, 五部.
    These are upstream of works and don't need to re-run.
    """
    # 1. Load the latest chancellery_final output
    logs = db.get_stage_logs(run_id)
    final_logs = [
        l for l in logs
        if l.get("stage_name") == "chancellery_final"
        and l.get("status") == "completed"
    ]
    if not final_logs:
        raise ValueError(
            "找不到已完成的 chancellery_final stage_log，无法提取修订意见。"
            "可能该 run 还没跑到终审，或终审本身失败了。"
        )
    # Use the latest one (later wins if there were prior revision rounds)
    final_review = final_logs[-1].get("output_data") or {}
    stored_revs = final_review.get("mandatory_revisions", []) or []
    stored_instr = final_review.get("revision_instructions", "") or ""

    # Retroactive synthesis: if chancellery returned empty revisions (common
    # when its own output got truncated), synthesize from review_dimensions
    # + suggestions on the fly so the revision loop has something to inject.
    if not stored_revs or not stored_instr:
        synth_revs, synth_instr = _synthesize_revisions_from_review(final_review)
        if synth_revs and not stored_revs:
            stored_revs = synth_revs
            logger.warning(
                f"[revise] synthesized {len(synth_revs)} mandatory_revisions "
                f"from review_dimensions (chancellery had left them empty)"
            )
        if synth_instr and not stored_instr:
            stored_instr = synth_instr
            logger.warning(
                "[revise] synthesized revision_instructions from review_dimensions"
            )

    if not stored_revs and not stored_instr:
        raise ValueError(
            "chancellery_final 既没填 mandatory_revisions 也没填 revision_instructions，"
            "而且 review_dimensions 里也没有低分维度可以合成。无法触发修订流程——"
            "请直接点「重跑流水线」创建新 run。"
        )

    # 1.5 Determine WHICH cells are affected vs which can be reused.
    # Scan revisions for D\d+ direction IDs and global-concern keywords.
    all_revision_text = " ".join(
        [r for r in stored_revs if isinstance(r, str)] + [stored_instr]
    )
    affected_direction_ids = sorted(set(re.findall(r"D\d+", all_revision_text)))

    global_keywords = [
        "shared_skeleton", "persona_library", "title_rules",
        "全局", "所有方向", "所有cell", "全部cell", "每个方向",
        "每个cell", "统一", "全部方向",
    ]
    is_global_revision = any(
        kw in all_revision_text.lower() or kw in all_revision_text
        for kw in global_keywords
    )
    logger.info(
        f"[revise] affected_direction_ids={affected_direction_ids}, "
        f"is_global_revision={is_global_revision}"
    )

    # Determine the next final-review round number. The first chancellery_final
    # call is round 1; every "apply revisions" click bumps it. chancellery.md
    # uses this to do incremental/delta reviews instead of from-scratch ones.
    project = db.get_project(project_id)
    brief = project.get("brief") or {}
    prior_rc = (brief or {}).get("_revision_context") or {}
    next_round = int(prior_rc.get("round", 1)) + 1
    logger.info(f"[revise] advancing final-review round to {next_round}")

    revision_context = {
        "round": next_round,
        "prior_verdict": final_review.get("verdict", "unknown"),
        "mandatory_revisions": stored_revs,
        "revision_instructions": stored_instr,
        "review_dimensions": final_review.get("review_dimensions", {}),
        "suggestions": final_review.get("suggestions", []),
        "affected_direction_ids": affected_direction_ids,
        "is_global_revision": is_global_revision,
        # prior_review is fed back into chancellery_final for delta evaluation.
        # Keep only the fields the reviewer prompt consumes to limit context
        # growth across multiple revision rounds.
        "prior_review": {
            "verdict": final_review.get("verdict", "unknown"),
            "mandatory_revisions": stored_revs,
            "revision_instructions": stored_instr,
            "review_dimensions": final_review.get("review_dimensions", {}),
        },
    }
    logger.info(
        f"[revise] Loaded revision_context round={next_round} with "
        f"{len(revision_context['mandatory_revisions'])} mandatory_revisions"
    )

    # 2. Store revision_context in project.brief so orchestrator.run() can read it
    brief["_revision_context"] = revision_context
    db.update_project(project_id, brief=brief)

    # 3. Selective deletion: only re-run what's actually needed.
    # Always re-run: vibe loop + chancellery_final (they evaluate the full matrix)
    stages_to_redo = ["vibe_critic", "vibe_rewriter", "chancellery_final"]

    if is_global_revision:
        # Global concern (persona_library / title_rules etc.)
        # → must re-run architect + all cell_planner + all builder
        stages_to_redo += [
            "ministry_works",
            "ministry_works_cell_planner",
            "ministry_works_builder",
        ]
        logger.info(
            "[revise] global revision → deleting ALL works stages + vibe + final"
        )
    else:
        # Cell-specific revision → keep builder stage_logs intact.
        # Cell-level resume will skip recovered cells; affected cells
        # (matching affected_direction_ids) will be excluded from recovery
        # and forced to re-build.
        logger.info(
            f"[revise] cell-specific revision → keeping builder stage_logs, "
            f"only D{'/D'.join(affected_direction_ids)} will be re-built"
        )

    deleted = db.delete_stage_logs_by_names(run_id, stages_to_redo)
    logger.info(
        f"[revise] Deleted {deleted} stage_logs: {stages_to_redo}"
    )

    # 4. Reset status and trigger resume
    db.update_pipeline_run(run_id, status="running", completed_at=None)
    db.update_project(project_id, status="running")
    return start_pipeline_in_background(project_id, run_id, db)
