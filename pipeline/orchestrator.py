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
    DEFAULT_PLATFORM,
    ENABLE_GEMINI_TREND_SCOUT_POST,
    ENABLE_GEMINI_TREND_SCOUT_PRE,
    ENABLE_STRUCTURAL_REWRITER,
    ENABLE_STRATEGIC_ESCALATION,
    ENABLE_CONSUMER_SIMULATION,
    STRATEGIC_LOOP_MAX_ITERATIONS,
    GEMINI_TREND_SCOUT_TARGET_COUNT,
    MAX_CHANCELLERY_REJECTIONS,
    MAX_CLARIFICATION_PER_AGENT,
    MAX_DEBATE_TURNS,
    MAX_FINAL_REJECTIONS,
    MATRIX_BATCH_CONCURRENCY,
    MATRIX_CELLS_PER_BATCH,
    PLATFORM_DEMO_LENGTH_DEFAULT,
    PLATFORM_DEMO_LENGTH_RANGES,
    RED_BLUE_CONCURRENCY,
    TREND_SCOUT_POST_CONCURRENCY,
    VIBE_LOOP_ESCALATE_THRESHOLD,
    VIBE_LOOP_HARD_CAP,
    VIBE_LOOP_INITIAL_CAP,
)
from pipeline.agents import (
    ClarificationNeeded,
    RunBudgetExceededError,
    accumulate_auxiliary_cost,
    get_run_totals,
    release_run_budget,
    reset_run_budget,
)
from pipeline.agents.gemini_critic import run_gemini_critic
from pipeline.agents.gemini_structure_reviewer import (
    format_revision_hints as _format_gemini_structure_hints,
    run_gemini_structure_review,
)
from pipeline.agents.gemini_reference_analyzer import run_reference_analyzer
from pipeline.retrieve_samples import retrieve_reference_packs
from pipeline.agents.gemini_trend_scout import (
    format_trend_intel_for_prompt,
    run_trend_scout,
)
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
    StructuralRewriter,
)
from pipeline.agents.narrative_director import NarrativeDirector
from pipeline.agents.red_blue_refiner import RedBlueRefiner
from pipeline.agents.persona_simulator import PersonaSimulator

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
        # v0.29.0: 叙事结构重写者 — 按 critic 的 root_cause_kind 分流。
        # 未启用 flag 时对象还是创建(零成本),但 _run_vibe_loop 不会调它。
        self.structural_rewriter = StructuralRewriter()
        self.narrative_director = NarrativeDirector()
        self.red_blue_refiner = RedBlueRefiner()
        self.persona_simulator = PersonaSimulator()

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
        self, stage_name: str, field: str, validate: bool = False,
    ) -> tuple[dict[str, dict], list[dict]]:
        """Scan stage_logs for all successful batches of the given stage_name
        in this run, and collect every {cell_id: cell_dict} that was produced.

        Used for cell-level resume: when the pipeline failed mid-batch, we
        don't want to re-run the batches that already succeeded. This walks
        every successful ministry_works_builder (or cell_planner) stage_log,
        extracts the cells from output_data[field], and de-duplicates by
        cell_id (later successful log wins).

        When `validate=True` (builder recovery path), cells are pushed through
        `_validate_prompt_cell` before being accepted — a log marked
        `status=completed` can still hold a cell that's empty/truncated/
        corrupt (validation was best-effort on round 3 of the builder
        retry loop, see _run_works_builders). Skipping invalid recovered
        cells forces a rebuild instead of propagating broken content into
        the final matrix, which was the root cause of the "D4 is empty in
        final output" class of bugs.

        Returns: (recovered_cells_by_id, recovered_demo_outputs)
        """
        logs = self.db.get_stage_logs(self.run_id, stage_name=stage_name)
        recovered: dict[str, dict] = {}
        demo_outputs: list[dict] = []
        rejected = 0
        for log in logs:
            if log.get("status") != "completed":
                continue
            output = log.get("output_data") or {}
            for item in output.get(field, []) or []:
                if not isinstance(item, dict) or not item.get("cell_id"):
                    continue
                if validate:
                    is_valid, _issues = _validate_prompt_cell(item)
                    if not is_valid:
                        rejected += 1
                        continue
                recovered[item["cell_id"]] = item
            # demo_outputs is builder-specific but harmless to collect for planner too
            for d in output.get("demo_outputs", []) or []:
                if isinstance(d, dict):
                    demo_outputs.append(d)
        if rejected:
            logger.warning(
                f"[recover {stage_name}] rejected {rejected} cells whose prior "
                f"'completed' stage_log held invalid/empty content — they'll "
                f"be rebuilt instead of propagated."
            )
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

            # Zero out per-run token counters so this attempt starts fresh.
            # Reset is essential for resumed/revised runs — otherwise the
            # previously-charged tokens would still count toward the budget
            # cap and a run could trip the guardrail on its first retry call.
            reset_run_budget(self.run_id)

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
                # Preserve original free_text on the brief so downstream stages
                # (especially 工部) can access rich raw materials directly,
                # not just Crown Prince's summarized fields.
                _raw_text = project.get("free_text", "")
                if _raw_text:
                    structured_brief["_raw_input_text"] = _raw_text
                self.db.update_project(self.project_id, brief=structured_brief)

            # 1a. User-pasted reference posts (B) — if page 2 recorded
            # any xiaohongshu.com URLs onto project.brief._reference_post_urls,
            # ask Gemini to fetch each via url_context and attach the
            # results to structured_brief._reference_posts for downstream
            # strategy use. Runs before trend scout because user-specified
            # references are higher-signal than generic keyword search.
            # Advisory-only.
            await self._run_gemini_reference_analyzer(structured_brief)

            # 1b. Gemini 趋势取样（advisory, A1）— 在策略规划之前拉一批当前
            # 平台的真实帖子（标题 + snippet 原文），注入到 structured_brief
            # 的 _trend_intel 字段。这些是**原文样本**不是趋势分析。
            # 失败时跳过不阻塞。
            await self._run_gemini_trend_scout_pre(structured_brief)

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

            # v0.27.0: 把 cell_plan / tactical_direction 索引挂到 self,
            # vibe_loop 需要把这些 ground-truth 字段(paradigm / reward_type /
            # role_embodiment / gap_direction / path_combination / product_role)
            # 注入到 critic_input,让 critic 做 4 乘数硬门槛对照判决。
            self._cell_plan_index = {
                cp.get("cell_id"): cp for cp in cell_plans if cp.get("cell_id")
            }
            self._direction_index = {
                d.get("direction_id"): d
                for d in plan.get("tactical_directions", [])
                if d.get("direction_id")
            }

            # Assemble full works_plan for builder
            works_plan = {**works_arch, "cell_plans": cell_plans}

            # 5c. 工部·构建 — generate per-cell prompts (Sonnet, batched)
            final_system = await self._run_works_builders(works_plan)

            if not final_system.get("prompt_matrix"):
                raise RuntimeError(
                    f"工部·构建产出为空。cell_plans count: {len(cell_plans)}, "
                    f"works_plan keys: {list(works_plan.keys())}"
                )

            # 5c+. 叙事导演（Claude, 跨 cell 一致性+差异化诊断）
            # 看完整矩阵后判断：钩子类型有没有重复？人设有没有立住？
            # 正反面叙事比例对不对？如果有问题，只重跑有问题的 cell。
            await self._run_narrative_director(final_system, works_plan)

            # 5c+++. 红蓝对抗精炼 — Red Team 找 AI 腔，Blue Team 做最小修复
            # 每个 cell 独立跑一次（并行，受 RPM 限速）。精修后的 demo
            # 直接替换回 prompt_matrix，所以后续 vibe/终审看到的是
            # 已经被精炼过的版本。
            await self._run_red_blue_refinement(final_system)

            # 5c++++. 画像模拟审稿 — 3 个模拟读者对每个 cell 的 demo
            # 给出 0.5 秒反应（click/skip/save）。结果存到
            # final_system._persona_reactions 让 UI 展示，也作为
            # vibe_critic 的补充输入。
            await self._run_persona_simulation(final_system, structured_brief)

            # 5c+++++. Gemini 结构审（advisory）— audits each prompt_cell for
            # structural completeness (5 pools / persona / compliance /
            # keywords / banlist / platform voice). Fires ONCE, between
            # builder and vibe. Hints get merged into the rewriter's
            # directives so the vibe stage also catches structural gaps,
            # not just taste issues. Advisory-only: any Gemini failure →
            # log warn, proceed with unchanged final_system.
            await self._run_gemini_structure_review_stage(final_system)

            # 5d. 网感复检循环 — critic checks demo, rewriter fixes failed cells
            final_system = await self._run_vibe_loop(final_system, structured_brief)

            # 5d+. 策略层自动升级(v0.29.1, C.2.1)— 如果 vibe_loop 留下了
            # strategic_warnings(interest_align / reward_signal 级错配,
            # rewriter 改不了),回 secretariat 修订受影响 direction 的策略
            # 锚点(stop_trigger / reward_type / role_embodiment),然后再
            # 跑一次 vibe_loop 让 critic + rewriter 用新锚点重新判决。
            # 上限 STRATEGIC_LOOP_MAX_ITERATIONS(默认 1)防止死循环。
            # Feature-flagged:ENABLE_STRATEGIC_ESCALATION=False 时跳过。
            if ENABLE_STRATEGIC_ESCALATION:
                final_system, plan = await self._run_strategic_escalation(
                    final_system, structured_brief, plan,
                )

            # 5d++. 消费者模拟(v0.29.1, C.2.2)— 在终审前用 persona_simulator
            # 以 direction.stop_trigger 描述的具体目标用户扮演者对每个 cell
            # 做 stop / scroll 二元判决,作为 interest_align 的第二层校验。
            # 被目标用户 scroll 的 cell 会追加进 strategic_warnings 让用户审查。
            if ENABLE_CONSUMER_SIMULATION:
                await self._run_consumer_simulation(
                    final_system, structured_brief, plan,
                )

            # 5e. Cross-cell duplicate re-check after vibe. Builder's own
            # check (at end of _run_works_builders) catches builder-produced
            # duplicates; this one catches rewriter-produced duplicates,
            # which is historically the bigger source because the rewriter
            # uses a shared reference-sample pool across all cells in a
            # batch (see vibe_rewriter.md's 真人参照样本 list).
            _post_vibe_dups = _find_cross_cell_duplicates(
                final_system.get("prompt_matrix", []) or []
            )
            if _post_vibe_dups:
                logger.error(
                    "网感复检产出跨 cell 重复:\n  - %s",
                    "\n  - ".join(_post_vibe_dups),
                )
                raise RuntimeError(
                    "网感重写之后的 prompt_matrix 存在跨 cell 重复：\n- "
                    + "\n- ".join(_post_vibe_dups)
                    + "\n\n这是 vibe_rewriter 多个 cell 被收敛到同一参照样本导致的。"
                    "流水线已终止以防交付重复内容。重跑一次应能抽到不同样本；"
                    "如果反复出现同一对 cell 冲突，考虑调整 vibe_rewriter 提示词里的样本差异化约束。"
                )

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

            # 6.9. Gemini 网感对标（advisory, A2）— 为每个 direction 拉一批
            # 当前小红书真实帖子，存到 final_system._per_direction_references
            # 让用户在产出页做"我的 demo vs 真实爆款"的肉眼对比。不改变
            # prompt_matrix 的内容——纯观察。跑在 save_output 之前好让
            # references 一起持久化。
            await self._run_gemini_trend_scout_post(final_system, plan)

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
        finally:
            # Release budget tracker memory for this run
            release_run_budget(self.run_id)

    async def _strategy_loop(self, brief: dict) -> dict:
        """Multi-turn strategy debate between secretariat and chancellery.

        Replaces the old submit→review→revise cycle with a real
        adversarial dialogue. Both agents see the full conversation
        history so their positions evolve naturally through challenge
        and response, not just "fix what I pointed out".

        The debate runs up to MAX_DEBATE_TURNS (config). Secretariat
        speaks on even turns (0, 2, 4...), chancellery on odd (1, 3, 5...).
        Chancellery can approve at any odd turn to end early.

        Each turn produces a stage_log entry for UI visibility:
          strategy_debate_0 (secretariat proposes)
          strategy_debate_1 (chancellery challenges)
          strategy_debate_2 (secretariat responds)
          ...
        """
        max_turns = int(MAX_DEBATE_TURNS)
        debate_history: list[dict] = []
        plan = None

        for turn in range(max_turns):
            stage_name = f"strategy_debate_{turn}"
            is_secretariat = (turn % 2 == 0)

            if is_secretariat:
                # Secretariat's turn: propose or respond to challenges
                input_data = {"brief": brief}
                if debate_history:
                    input_data["debate_history"] = debate_history
                    input_data["debate_mode"] = True

                agent = self.secretariat
                orig_stage = agent.stage_name
                agent.stage_name = stage_name
                try:
                    response = await self._run_with_clarification(agent, input_data)
                finally:
                    agent.stage_name = orig_stage

                # Extract the plan from secretariat's response. In debate
                # mode it's in current_plan; in initial mode it's the
                # top-level output.
                plan = response.get("current_plan") or response
                debate_history.append({
                    "role": "secretariat",
                    "turn": turn,
                    "content": response,
                })
                logger.info(
                    f"[strategy_debate] turn {turn} (secretariat): "
                    f"directions={[d.get('direction_id') for d in plan.get('tactical_directions', [])]}"
                )
            else:
                # Chancellery's turn: challenge or approve
                input_data = {
                    "review_type": "plan_review",
                    "plan": plan,
                    "brief": brief,
                    "debate_history": debate_history,
                    "debate_mode": True,
                    "round_number": (turn // 2) + 1,
                }

                # Force pass on last chancellery turn
                if turn >= max_turns - 1:
                    response = {
                        "verdict": "approved",
                        "challenges": [],
                        "overall_assessment": "⚠️ 辩论达到最大轮次，强制通过",
                    }
                    log = self.db.create_stage_log(
                        self.run_id, stage_name, input_data
                    )
                    self.db.update_stage_log(
                        log["id"], status="completed", output_data=response
                    )
                else:
                    agent = self.chancellery
                    orig_stage = agent.stage_name
                    agent.stage_name = stage_name
                    try:
                        response = await agent.run(input_data, self.run_id, self.db)
                    finally:
                        agent.stage_name = orig_stage

                debate_history.append({
                    "role": "chancellery",
                    "turn": turn,
                    "content": response,
                })

                verdict = (response.get("verdict") or "").strip().lower()
                logger.info(
                    f"[strategy_debate] turn {turn} (chancellery): "
                    f"verdict={verdict}, "
                    f"challenges={len(response.get('challenges', []))}"
                )

                if verdict == "approved":
                    logger.info(
                        f"[strategy_debate] approved after {turn + 1} turns"
                    )
                    return plan

        logger.warning(
            f"[strategy_debate] max turns ({max_turns}) reached, "
            f"forcing plan through"
        )
        return plan

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

    def _reconstruct_active_cells(self, plan: dict) -> list[dict]:
        """Compute the active cell list from the plan.

        Reconciles secretariat's active_cells with the D×P (directions ×
        platforms − excluded) reconstruction.  Historical bug: model
        sometimes only emits D1 cells and omits the rest.  Fix: splice
        in missing (direction_id, platform) pairs without dropping what
        secretariat intentionally kept.

        Returns the final active_cells list (may be empty).
        """
        active_cells = plan.get("matrix_skeleton", {}).get("active_cells", []) or []

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

        active_dir_set = {str(c.get("direction_id")) for c in active_cells if isinstance(c, dict)}
        expected_dir_set = {str(c.get("direction_id")) for c in expected_cells}

        if not active_cells and expected_cells:
            logger.warning(
                f"matrix_skeleton.active_cells is empty, using reconstruction "
                f"({len(expected_cells)} cells from {len(directions)}×{len(platforms)})"
            )
            active_cells = expected_cells
        elif expected_cells:
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

        return active_cells

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

        active_cells = self._reconstruct_active_cells(plan)
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
        # validate=True: re-run _validate_prompt_cell on every recovered cell.
        # Prior-run stage_logs marked "completed" can still contain broken
        # output (round-3 builder retry accepts best-effort cells), and
        # silently propagating a broken D4 cell into the final matrix was
        # the root cause of the "D4 is empty in final output" bug class.
        recovered_cells, recovered_demos = self._recover_completed_cells(
            "ministry_works_builder", "prompt_cells", validate=True,
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

        # Expected-id coverage: catch the case where all_cells has the right
        # COUNT but some cell_ids are duplicated (e.g. D4 produced twice,
        # D5 missing entirely, len() accidentally matches). This is belt-
        # and-suspenders on top of the count check above.
        produced_ids = {c.get("cell_id") for c in all_cells}
        expected_ids = {c.get("cell_id") for c in original_expected_order}
        missing_expected = expected_ids - produced_ids
        if missing_expected:
            raise RuntimeError(
                f"工部·构建 cell_id 集合对不上：期望 {sorted(expected_ids)}，"
                f"实际产出 {sorted(produced_ids)}。缺失: {sorted(missing_expected)}。"
            )

        # Cross-cell duplicate detection. Rewriter convergence (see
        # vibe_rewriter prompt — historically shared a single reference
        # sample list across all cells in a batch) could produce a matrix
        # where e.g. D1 and D5 both open with the exact same first line.
        # The loop runs AFTER the vibe stage so it catches both builder-
        # level duplication and rewriter-introduced duplication. Raises
        # instead of shipping a matrix where multiple cells read the same.
        # Runs late enough that we have a full picture but before save_output.
        dup_issues = _find_cross_cell_duplicates(all_cells)
        if dup_issues:
            # Log before raising so UI can surface the specific duplicates
            # from the stage_log error_message.
            logger.error(
                "工部·构建跨 cell 重复检测 fail:\n  - %s",
                "\n  - ".join(dup_issues),
            )
            raise RuntimeError(
                "工部·构建产出的 prompt_matrix 存在跨 cell 重复（两个 cell "
                "用了完全相同的 demo 或开场）：\n- " + "\n- ".join(dup_issues)
                + "\n\n这通常是 vibe_rewriter 在同一批次内没对不同 cell "
                "做样本差异化导致的收敛。重跑流水线应能抽到不同样本。"
            )

        return {
            "prompt_matrix": all_cells,
            "matrix_dimensions": works_plan.get("matrix_dimensions", {}),
            "demo_outputs": all_demos,
            "shared_skeleton": works_plan.get("shared_skeleton", {}),
            "_uncertainty_summary": works_plan.get("_uncertainty_summary", {}),
        }

    async def _run_narrative_director(
        self, final_system: dict, works_plan: dict
    ) -> None:
        """Narrative Director: cross-cell coherence + diversity audit.

        Looks at the WHOLE prompt_matrix as a unit. If it finds issues
        (e.g. 3/6 cells use the same hook type), outputs per-cell fix
        instructions. Affected cells get selectively rebuilt via the
        same builder flow (not a full matrix re-run).
        """
        prompt_cells = final_system.get("prompt_matrix") or []
        if len(prompt_cells) < 2:
            # Single cell — nothing to compare
            return

        # Build slim input (director doesn't need full system_prompt text,
        # just demo_output + direction/persona/hook metadata)
        slim_cells = [
            {
                "cell_id": c.get("cell_id"),
                "direction_id": c.get("direction_id"),
                "direction_name": c.get("direction_name"),
                "platform": c.get("platform"),
                "demo_output": (c.get("demo_output") or "")[:500],
                "system_prompt_preview": (c.get("system_prompt") or "")[:300],
            }
            for c in prompt_cells
        ]

        try:
            review = await self.narrative_director.run(
                {"prompt_cells": slim_cells},
                self.run_id,
                self.db,
            )
        except Exception as e:
            logger.warning(
                "[narrative_director] failed, skipping: %r", e
            )
            return

        verdict = (review.get("verdict") or "").lower()
        cells_to_revise = review.get("cells_to_revise") or []

        if verdict == "all_coherent" or not cells_to_revise:
            logger.info(
                "[narrative_director] all_coherent — no cross-cell issues"
            )
            final_system["_narrative_director_result"] = review
            return

        logger.warning(
            "[narrative_director] needs_adjustment — %d cells flagged: %s",
            len(cells_to_revise),
            [c.get("cell_id") for c in cells_to_revise],
        )
        final_system["_narrative_director_result"] = review

        # Selective rebuild: for each flagged cell, inject the director's
        # fix_instruction into the builder's input as a _narrative_directive
        # and re-run just that cell. Reuse the existing single-cell builder
        # flow from _run_works_builders (build_single_cell).
        cell_plans_by_id = {
            c.get("cell_id"): c
            for c in works_plan.get("cell_plans", [])
        }
        shared_skeleton = works_plan.get("shared_skeleton", {})

        for revision in cells_to_revise:
            cid = revision.get("cell_id")
            fix = revision.get("fix_instruction", "")
            if not cid or not fix:
                continue
            cell_plan = cell_plans_by_id.get(cid)
            if not cell_plan:
                continue

            logger.info(
                "[narrative_director] rebuilding %s: %s",
                cid,
                fix[:100],
            )
            try:
                rebuild_input = {
                    "active_cells": [cell_plan],
                    "shared_skeleton": shared_skeleton,
                    "_batch_info": {
                        "label": f"叙事导演修复 {cid}",
                        "round": "narrative_fix",
                        "cell_ids": [cid],
                    },
                    "_narrative_directive": fix,
                    "_strict_contract": (
                        f"叙事导演要求修改这个 cell：{fix}\n"
                        f"在保留原有合规/关键词/人设的前提下，按指令调整 "
                        f"demo_output 的钩子结构或叙事方式。"
                    ),
                }
                rebuilt = await self.works_builder.run(
                    rebuild_input, self.run_id, self.db
                )
                new_cells = rebuilt.get("prompt_cells") or []
                for nc in new_cells:
                    if nc.get("cell_id") == cid:
                        # Replace in prompt_matrix
                        for i, c in enumerate(prompt_cells):
                            if c.get("cell_id") == cid:
                                prompt_cells[i] = nc
                                logger.info(
                                    "[narrative_director] rebuilt %s OK",
                                    cid,
                                )
                                break
                        break
            except Exception as e:
                logger.warning(
                    "[narrative_director] rebuild %s failed: %r",
                    cid,
                    e,
                )

        final_system["prompt_matrix"] = prompt_cells

    async def _run_red_blue_refinement(self, final_system: dict) -> None:
        """Red-Blue adversarial refinement: for each cell, Red Team
        attacks AI-tone issues in demo_output, Blue Team fixes them
        with minimal changes. Combined in a single agent call per cell.

        Runs cells in parallel (bounded by rate limiter). Refined
        demo_output replaces the original in prompt_matrix in place.
        """
        prompt_cells = final_system.get("prompt_matrix") or []
        if not prompt_cells:
            return

        semaphore = asyncio.Semaphore(RED_BLUE_CONCURRENCY)

        async def refine_one(cell: dict) -> dict | None:
            cid = cell.get("cell_id", "?")
            async with semaphore:
                try:
                    result = await self.red_blue_refiner.run(
                        {
                            "cell_id": cid,
                            "direction_name": cell.get("direction_name", ""),
                            "platform": cell.get("platform", ""),
                            "system_prompt": (cell.get("system_prompt") or "")[:500],
                            "demo_output": cell.get("demo_output", ""),
                            "paradigm": cell.get("paradigm", "A_emotional_hook"),
                        },
                        self.run_id,
                        self.db,
                    )
                    return result
                except Exception as e:
                    logger.warning(
                        "[red_blue] cell %s failed: %r", cid, e
                    )
                    return None

        results = await asyncio.gather(
            *[refine_one(c) for c in prompt_cells]
        )

        refined_count = 0
        for i, (cell, result) in enumerate(zip(prompt_cells, results)):
            if not result:
                continue
            new_demo = result.get("refined_demo_output")
            if new_demo and new_demo.strip():
                prompt_cells[i]["demo_output"] = new_demo
                prompt_cells[i]["_red_blue_summary"] = result.get(
                    "changes_summary", ""
                )
                refined_count += 1
            # Also apply system_prompt refinement if provided
            new_sp = result.get("refined_system_prompt")
            if new_sp and new_sp.strip():
                prompt_cells[i]["system_prompt"] = new_sp

        logger.info(
            "[red_blue] refined %d/%d cells", refined_count, len(prompt_cells)
        )

    async def _run_persona_simulation(
        self, final_system: dict, brief: dict
    ) -> None:
        """Persona simulation: 3 target-audience personas react to each
        cell's demo. Results stored on final_system for UI display and
        as supplementary input to vibe_critic.
        """
        prompt_cells = final_system.get("prompt_matrix") or []
        if not prompt_cells:
            return

        target_audience = brief.get("target_audience", "")
        if isinstance(target_audience, list):
            target_audience = ", ".join(str(t) for t in target_audience)

        slim_cells = [
            {
                "cell_id": c.get("cell_id"),
                "direction_name": c.get("direction_name"),
                "demo_output": (c.get("demo_output") or "")[:500],
            }
            for c in prompt_cells
        ]

        try:
            result = await self.persona_simulator.run(
                {
                    "target_audience": target_audience or "未指定（请根据 brief 推断）",
                    "platform": brief.get("target_platforms", [DEFAULT_PLATFORM])[0]
                    if brief.get("target_platforms")
                    else DEFAULT_PLATFORM,
                    "cells": slim_cells,
                },
                self.run_id,
                self.db,
            )
        except Exception as e:
            logger.warning("[persona_sim] failed: %r", e)
            return

        final_system["_persona_reactions"] = result
        summary = result.get("summary") or {}
        weak = summary.get("weak_cells") or []
        if weak:
            logger.warning(
                "[persona_sim] weak cells (all 3 personas skip): %s", weak
            )
        else:
            logger.info(
                "[persona_sim] strong=%s, narrow=%s, weak=%s",
                summary.get("strong_cells", []),
                summary.get("narrow_cells", []),
                summary.get("weak_cells", []),
            )

    async def _run_gemini_reference_analyzer(self, brief: dict) -> None:
        """B: fetch user-pasted xiaohongshu post URLs via Gemini url_context.

        URL list is pulled from either:
          - project.brief._reference_post_urls (set by page 2 at create
            time)
          - brief._reference_post_urls (after crown_prince may have moved
            it into structured brief)

        Results land on brief['_reference_posts'] as raw per-URL records
        with fetch_status, so downstream consumers can tell which posts
        actually got content vs hit the login wall.
        """
        # Pull URLs from either pre- or post-crown_prince brief location.
        project = self.db.get_project(self.project_id) or {}
        pre_brief = project.get("brief") or {}
        urls = (
            brief.get("_reference_post_urls")
            or pre_brief.get("_reference_post_urls")
            or []
        )
        urls = [u for u in urls if isinstance(u, str) and u.strip()]
        if not urls:
            return

        stage_name = "gemini_reference_analyzer"
        log = self.db.create_stage_log(
            self.run_id,
            stage_name,
            {"url_count": len(urls)},
        )
        log_id = log["id"]

        try:
            result = await run_reference_analyzer(urls)
        except Exception as e:
            logger.warning(
                "[ref_analyzer] unexpected exception: %r", e
            )
            self.db.update_stage_log(
                log_id,
                status="skipped",
                output_data={"_skip_reason": f"unexpected_exception: {e}"},
            )
            return

        usage = result.get("_gemini_usage") or {}
        if usage:
            accumulate_auxiliary_cost(
                self.run_id,
                cost_usd=float(usage.get("cost_usd", 0.0)),
                input_tokens=int(usage.get("input_tokens", 0)),
                output_tokens=int(usage.get("output_tokens", 0)),
                source="gemini_reference_analyzer",
            )

        verdict = result.get("verdict", "skipped")
        if verdict == "skipped":
            self.db.update_stage_log(
                log_id,
                status="skipped",
                output_data={
                    "_skip_reason": result.get("_skip_reason", "unknown"),
                    "_raw_text_preview": result.get("_raw_text_preview", ""),
                    "_gemini_usage": usage,
                },
                tokens_used=int(usage.get("input_tokens", 0))
                + int(usage.get("output_tokens", 0)),
                model_used=usage.get("model"),
            )
            return

        brief["_reference_posts"] = {
            "posts": result.get("posts", []),
            "summary_stats": result.get("_summary_stats", {}),
            "verdict": verdict,
        }
        self.db.update_project(self.project_id, brief=brief)

        self.db.update_stage_log(
            log_id,
            status="completed",
            output_data={
                "verdict": verdict,
                "posts": result.get("posts", []),
                "_summary_stats": result.get("_summary_stats", {}),
            },
            model_used=usage.get("model"),
            tokens_used=int(usage.get("input_tokens", 0))
            + int(usage.get("output_tokens", 0)),
        )
        logger.info(
            "[ref_analyzer] fetched %d/%d user-pasted references (verdict=%s)",
            len(result.get("posts", [])),
            len(urls),
            verdict,
        )

    async def _run_gemini_trend_scout_pre(self, brief: dict) -> None:
        """A1: pre-secretariat trend scout. Pulls current real Xiaohongshu
        VIRAL素人 post samples (not product-relevant; just "what's hot
        right now") via Gemini + Google Search to calibrate downstream
        copy writing against current platform voice. Advisory-only.

        Design note: we deliberately do NOT search product/brand
        keywords here. Searching "珂润精华液" returns 软广 + 分析 articles
        that have no素人 vibe at all. Instead we pass the brief's
        target_audience snippet as a soft "vibe_hints" — the scout's
        prompt tells Gemini to use it for sorting results (prefer viral
        posts that feel like they target the same demographic), never
        as a direct search term. The scout's queries are format-anchored
        (反差 / 社死 / 身份标签 / reposts on weibo) so what comes back
        is a set of raw current viral first-sentences we can calibrate
        against, regardless of topic.
        """
        if not ENABLE_GEMINI_TREND_SCOUT_PRE:
            return

        # vibe_hints: demographic/audience context used by the scout
        # only for soft sorting of results. Never searched directly.
        # We deliberately exclude product_name / product_category /
        # core_claim — those lead to ads and analysis articles.
        vibe_hints: list[str] = []
        for k in ("target_audience", "campaign_objective"):
            v = brief.get(k)
            if v and isinstance(v, str) and v.strip():
                vibe_hints.append(v.strip())
            elif isinstance(v, list):
                for item in v:
                    if isinstance(item, str) and item.strip():
                        vibe_hints.append(item.strip())
        # Empty vibe_hints is fine — the scout falls back to generic
        # "current xhs viral" queries.

        platforms = brief.get("target_platforms") or []
        platform = platforms[0] if platforms else DEFAULT_PLATFORM

        stage_name = "gemini_trend_scout_pre"
        log = self.db.create_stage_log(
            self.run_id,
            stage_name,
            {"vibe_hints": vibe_hints, "platform": platform},
        )
        log_id = log["id"]

        try:
            result = await run_trend_scout(
                vibe_hints=vibe_hints,
                platform=platform,
                target_count=GEMINI_TREND_SCOUT_TARGET_COUNT,
            )
        except Exception as e:
            logger.warning(
                "[trend_scout pre] unexpected exception, skipping: %r", e
            )
            self.db.update_stage_log(
                log_id,
                status="skipped",
                output_data={"_skip_reason": f"unexpected_exception: {e}"},
            )
            return

        usage = result.get("_gemini_usage") or {}
        if usage:
            accumulate_auxiliary_cost(
                self.run_id,
                cost_usd=float(usage.get("cost_usd", 0.0)),
                input_tokens=int(usage.get("input_tokens", 0)),
                output_tokens=int(usage.get("output_tokens", 0)),
                source="gemini_trend_scout_pre",
            )

        verdict = result.get("verdict", "skipped")
        if verdict == "skipped":
            self.db.update_stage_log(
                log_id,
                status="skipped",
                output_data={
                    "_skip_reason": result.get("_skip_reason", "unknown"),
                    "queries_used": result.get("queries_used", []),
                    "_raw_text_preview": result.get("_raw_text_preview", ""),
                    "_gemini_usage": usage,
                },
                tokens_used=int(usage.get("input_tokens", 0))
                + int(usage.get("output_tokens", 0)),
                model_used=usage.get("model"),
            )
            logger.info(
                "[trend_scout pre] skipped: %s",
                result.get("_skip_reason", "unknown"),
            )
            return

        # Inject raw posts into brief so secretariat sees them. Store
        # both the structured list (for UI + downstream programmatic
        # access) and a formatted block (for direct prompt injection).
        brief["_trend_intel"] = {
            "posts": result.get("posts", []),
            "queries_used": result.get("queries_used", []),
            "grounding_urls": result.get("grounding_urls", []),
            "formatted_block": format_trend_intel_for_prompt(result),
        }
        self.db.update_project(self.project_id, brief=brief)

        self.db.update_stage_log(
            log_id,
            status="completed",
            output_data={
                "verdict": verdict,
                "posts": result.get("posts", []),
                "queries_used": result.get("queries_used", []),
                "grounding_urls": result.get("grounding_urls", []),
                "_rejected_off_domain_count": result.get(
                    "_rejected_off_domain_count", 0
                ),
                "_not_found_reason": result.get("_not_found_reason"),
            },
            model_used=usage.get("model"),
            tokens_used=int(usage.get("input_tokens", 0))
            + int(usage.get("output_tokens", 0)),
        )
        logger.info(
            "[trend_scout pre] captured %d raw viral posts for vibe_hints=%s",
            len(result.get("posts", [])),
            vibe_hints,
        )

    async def _run_gemini_trend_scout_post(
        self, final_system: dict, plan: dict
    ) -> None:
        """A2: per-direction scout after final review. For each tactical
        direction in the strategic plan, pull 5 real current Xiaohongshu
        posts matching that direction's theme. Result lands on
        final_system['_per_direction_references'][direction_id] as raw
        posts — advisory only, no prompt-matrix mutation.

        Runs in parallel per direction (bounded concurrency) because
        each direction is an independent Gemini call.

        Skipped wholesale if ENABLE_GEMINI_TREND_SCOUT_POST=False or
        Gemini's unavailable. One stage_log per direction so the UI
        can display status independently.
        """
        if not ENABLE_GEMINI_TREND_SCOUT_POST:
            return

        directions = plan.get("tactical_directions") or []
        if not directions:
            logger.info(
                "[trend_scout post] skipping — no tactical_directions in plan"
            )
            return

        platforms = plan.get("target_platforms") or []
        platform = platforms[0] if platforms else DEFAULT_PLATFORM

        # Target per-direction sample count — keep smaller than pre (10)
        # because we hit this once per direction, and too many samples
        # overwhelm the UI comparison.
        per_direction_count = min(GEMINI_TREND_SCOUT_TARGET_COUNT, 5)

        semaphore = asyncio.Semaphore(TREND_SCOUT_POST_CONCURRENCY)

        async def _scout_one(direction: dict) -> tuple[str, dict]:
            d_id = str(direction.get("direction_id", "")).strip()
            d_name = str(direction.get("direction_name", "")).strip()
            if not d_id:
                return "", {}
            # vibe_hints = soft context about this direction's flavor.
            # Like the pre-scout, the scout prompt will NOT search these
            # directly — it uses them to sort results among viral-format
            # query returns. direction_name + paradigm tells Gemini
            # "among all current viral xhs posts, prefer ones that
            # match this particular flavor" without hard-filtering.
            vibe_hints: list[str] = []
            if d_name:
                vibe_hints.append(d_name)
            paradigm = direction.get("paradigm", "")
            if paradigm == "A_emotional_hook":
                vibe_hints.append("情绪钩子型")
            elif paradigm == "B_meta_response":
                vibe_hints.append("成分党元评论体")

            async with semaphore:
                stage_name = f"gemini_trend_scout_post_{d_id}"
                log = self.db.create_stage_log(
                    self.run_id,
                    stage_name,
                    {"direction_id": d_id, "vibe_hints": vibe_hints},
                )
                log_id = log["id"]
                try:
                    result = await run_trend_scout(
                        vibe_hints=vibe_hints,
                        platform=platform,
                        target_count=per_direction_count,
                    )
                except Exception as e:
                    logger.warning(
                        "[trend_scout post/%s] unexpected exception: %r",
                        d_id,
                        e,
                    )
                    self.db.update_stage_log(
                        log_id,
                        status="skipped",
                        output_data={
                            "_skip_reason": f"unexpected_exception: {e}"
                        },
                    )
                    return d_id, {}

                usage = result.get("_gemini_usage") or {}
                if usage:
                    accumulate_auxiliary_cost(
                        self.run_id,
                        cost_usd=float(usage.get("cost_usd", 0.0)),
                        input_tokens=int(usage.get("input_tokens", 0)),
                        output_tokens=int(usage.get("output_tokens", 0)),
                        source="gemini_trend_scout_post",
                    )

                verdict = result.get("verdict", "skipped")
                if verdict == "skipped":
                    self.db.update_stage_log(
                        log_id,
                        status="skipped",
                        output_data={
                            "_skip_reason": result.get(
                                "_skip_reason", "unknown"
                            ),
                            "_gemini_usage": usage,
                        },
                        tokens_used=int(usage.get("input_tokens", 0))
                        + int(usage.get("output_tokens", 0)),
                        model_used=usage.get("model"),
                    )
                    return d_id, {}

                self.db.update_stage_log(
                    log_id,
                    status="completed",
                    output_data={
                        "verdict": verdict,
                        "direction_id": d_id,
                        "direction_name": d_name,
                        "posts": result.get("posts", []),
                        "queries_used": result.get("queries_used", []),
                        "grounding_urls": result.get("grounding_urls", []),
                        "_not_found_reason": result.get(
                            "_not_found_reason"
                        ),
                    },
                    model_used=usage.get("model"),
                    tokens_used=int(usage.get("input_tokens", 0))
                    + int(usage.get("output_tokens", 0)),
                )
                return d_id, {
                    "direction_id": d_id,
                    "direction_name": d_name,
                    "posts": result.get("posts", []),
                    "queries_used": result.get("queries_used", []),
                }

        results = await asyncio.gather(
            *[_scout_one(d) for d in directions], return_exceptions=True
        )
        per_direction: dict[str, dict] = {}
        for r in results:
            if isinstance(r, BaseException):
                logger.warning("[trend_scout post] direction failed: %r", r)
                continue
            d_id, data = r
            if d_id and data:
                per_direction[d_id] = data

        if per_direction:
            final_system["_per_direction_references"] = per_direction
            logger.info(
                "[trend_scout post] captured references for %d directions: %s",
                len(per_direction),
                sorted(per_direction.keys()),
            )

    async def _run_gemini_structure_review_stage(
        self, final_system: dict
    ) -> None:
        """Gemini 结构审（advisory）— 在工部构建之后、网感复检之前跑一次。

        审查每条 prompt_cell 的 5 池 / 人设 / 合规 / 关键词 / AI 禁用清单 /
        平台调性是否结构完整。结果写成一条 stage_log（名为
        ministry_works_structure_review）给 UI 看，并把每 cell 的结构缺失
        hint 附到对应 cell 的 _structure_hint 字段，让下游 rewriter 和
        chancellery_final 都能读到。

        Advisory-only：Gemini 未配置 / 调用失败 / 解析失败全部 → 跳过，
        不阻塞流水线。Gemini 的 token + 成本通过 accumulate_auxiliary_cost
        累到 pipeline_runs.total_cost_usd 里，不占 MAX_TOKENS_PER_RUN 预算。
        """
        prompt_cells = final_system.get("prompt_matrix", []) or []
        if not prompt_cells:
            return

        stage_name = "ministry_works_structure_review"
        log = self.db.create_stage_log(
            self.run_id,
            stage_name,
            {"cell_count": len(prompt_cells)},
        )
        log_id = log["id"]

        try:
            result = await run_gemini_structure_review(prompt_cells)
        except Exception as e:
            # run_gemini_structure_review swallows its own errors; if
            # something still leaks treat as non-fatal.
            logger.warning(
                "[structure review] unexpected exception, skipping: %r", e
            )
            self.db.update_stage_log(
                log_id,
                status="skipped",
                output_data={"_skip_reason": f"unexpected_exception: {e}"},
            )
            return

        verdict = result.get("verdict", "skipped")
        usage = result.get("_gemini_usage") or {}

        # Cost accounting — even on skipped we may have been charged 0
        # tokens; accumulate_auxiliary_cost is idempotent on 0.
        if usage:
            accumulate_auxiliary_cost(
                self.run_id,
                cost_usd=float(usage.get("cost_usd", 0.0)),
                input_tokens=int(usage.get("input_tokens", 0)),
                output_tokens=int(usage.get("output_tokens", 0)),
                source="gemini_structure",
            )

        # Annotate cells with their per-cell structure hint so the
        # downstream rewriter / chancellery can consume them in
        # subsequent stages. Mutates prompt_matrix in place.
        incomplete = result.get("cells_incomplete") or []
        incomplete_by_id = {
            item.get("cell_id"): item
            for item in incomplete
            if isinstance(item, dict) and item.get("cell_id")
        }
        for cell in prompt_cells:
            cid = cell.get("cell_id")
            if cid in incomplete_by_id:
                item = incomplete_by_id[cid]
                cell["_structure_hint"] = {
                    "missing_items": item.get("missing_items", []),
                    "revision_hint": item.get("revision_hint", ""),
                }

        # Stash the full review on final_system so chancellery_final
        # can see the structural context and terminal UI can display.
        final_system["_structure_review"] = {
            "verdict": verdict,
            "summary": result.get("summary", ""),
            "cell_reviews": result.get("cell_reviews", []),
            "cells_incomplete": incomplete,
            "_model": usage.get("model"),
        }

        if verdict == "skipped":
            self.db.update_stage_log(
                log_id,
                status="skipped",
                output_data={
                    "_skip_reason": result.get("_skip_reason", "unknown"),
                    "_raw_text_preview": result.get("_raw_text_preview", ""),
                },
                tokens_used=int(usage.get("input_tokens", 0))
                + int(usage.get("output_tokens", 0)),
                model_used=usage.get("model"),
            )
            return

        self.db.update_stage_log(
            log_id,
            status="completed",
            output_data={
                "verdict": verdict,
                "summary": result.get("summary", ""),
                "cells_incomplete": incomplete,
                "cell_reviews": result.get("cell_reviews", []),
            },
            model_used=usage.get("model"),
            tokens_used=int(usage.get("input_tokens", 0))
            + int(usage.get("output_tokens", 0)),
        )

        if verdict == "some_incomplete":
            logger.warning(
                "[structure review] %d/%d cells flagged incomplete by Gemini: %s",
                len(incomplete),
                len(prompt_cells),
                sorted(incomplete_by_id.keys())[:10],
            )
        else:
            logger.info(
                "[structure review] all %d cells structurally complete",
                len(prompt_cells),
            )

    async def _run_vibe_loop(
        self,
        final_system: dict,
        structured_brief: dict | None = None,
    ) -> dict:
        """Critic → Rewriter loop with elastic iteration count.

        网感不行 = system_prompt 设计有缺陷 → 重写 system_prompt（不是改 demo）。
        Round 2+ only re-evaluates cells that were rewritten, not the entire matrix.

        Hard cap = 3. Initially the loop runs up to 2 rounds (same as v0.10.x).
        If, after round 2, the failure rate is still ≥ `vibe_escalate_threshold`
        (30% of total cells), one additional round is authorized. This
        gives stubborn cases a last chance instead of silently shipping
        bad taste, while still bounding worst-case token spend.
        """
        hard_cap = VIBE_LOOP_HARD_CAP
        initial_cap = VIBE_LOOP_INITIAL_CAP
        escalate_threshold = VIBE_LOOP_ESCALATE_THRESHOLD
        prompt_cells = final_system.get("prompt_matrix", [])
        if not prompt_cells:
            logger.warning("Vibe loop skipped: no prompt_cells")
            return final_system

        shared_skeleton = final_system.get("shared_skeleton", {})

        # ── 人工证据包检索(v0.25.0)───────────────────────────────────────
        # 为每个平台预取匹配的参考样本(用户在 pages/6_reference_library.py 录入),
        # 注入到 critic/rewriter 的 input。评论区 DNA / hook / comment
        # resonance_points 等来源于 reference_pack_analyzer 的结构化输出,
        # rewriter 可以 per-cell 按 platform 挑自己关心的。
        # 零样本情况下(用户还没录入)静默跳过,不阻塞也不警告。
        _brief_category = (
            (structured_brief or {}).get("product_category")
            or (structured_brief or {}).get("category")
        )
        _unique_platforms = sorted({
            (c.get("platform") or "").strip()
            for c in prompt_cells
            if c.get("platform")
        })
        reference_packs_by_platform: dict[str, list] = {}
        for _plat in _unique_platforms:
            _packs = retrieve_reference_packs(
                self.db,
                platform=_plat,
                category=_brief_category,
                limit=6,
            )
            if _packs:
                reference_packs_by_platform[_plat] = _packs
        if reference_packs_by_platform:
            logger.info(
                "[vibe_loop] injecting reference_packs: %s",
                {k: len(v) for k, v in reference_packs_by_platform.items()},
            )

        # Track which cell_ids were rewritten so round 2+ only re-evaluates those
        rewritten_ids: set[str] = set()
        # Total matrix size doesn't change across iterations (rewriter preserves
        # cell_ids), so capture it once for the escalation-threshold math.
        total_cells_count = max(len(prompt_cells), 1)
        # max_iterations starts at the initial cap and may be bumped to hard_cap
        # after round 2 if too many cells are still failing. See escalate_threshold.
        max_iterations = initial_cap

        iteration = 0
        while iteration < max_iterations:
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

            # v0.27.0: 注入 secretariat + cell_planner 标注的 ground-truth
            # 字段(reward_type / role_embodiment / gap_direction / paradigm /
            # path_combination / product_role),让 critic 做 4 乘数硬门槛
            # 对照判决(见 vibe_critic.md 第 0.3 步)。
            # cell_plans 和 direction 索引在 orchestrator.run() 5b 步挂到
            # self 上;resume 路径若索引为空,critic 会自动退到启发式判断。
            _cell_idx = getattr(self, "_cell_plan_index", {}) or {}
            _dir_idx = getattr(self, "_direction_index", {}) or {}

            def _enriched_cell(c: dict) -> dict:
                cp = _cell_idx.get(c.get("cell_id"), {}) or {}
                direction = _dir_idx.get(c.get("direction_id"), {}) or {}
                return {
                    "cell_id": c.get("cell_id"),
                    "direction_id": c.get("direction_id"),
                    "direction_name": c.get("direction_name"),
                    "platform": c.get("platform"),
                    "system_prompt": c.get("system_prompt", ""),
                    "demo_output": c.get("demo_output", ""),
                    # ground-truth intent from upstream stages
                    "paradigm": cp.get("paradigm") or direction.get("paradigm"),
                    "reward_type": direction.get("reward_type"),
                    "role_embodiment": direction.get("role_embodiment"),
                    "gap_direction": direction.get("gap_direction"),
                    # v0.29.0: stop_trigger — critic 的 interest_align 判决硬
                    # 锚点,替代宽泛的 target_audience 散文。secretariat 未填
                    # 时为 None,critic 会自动退回到启发式判断。
                    "stop_trigger": direction.get("stop_trigger"),
                    "path_combination": cp.get("path_combination"),
                    "product_role": cp.get("product_role"),
                }

            critic_input = {
                "prompt_cells": [_enriched_cell(c) for c in cells_to_critique]
            }
            # Pass brief-level context so critic can check interest_align
            # against target_audience + campaign_objective.
            if structured_brief:
                critic_input["brief_context"] = {
                    "target_audience": structured_brief.get("target_audience", ""),
                    "campaign_objective": structured_brief.get("campaign_objective", []),
                    "product_category": structured_brief.get("product_category", ""),
                    # v0.29.0: 让 critic 的 identity_consistency 判决按
                    # advertising_stance 分流,而不是硬假设 stealth。
                    "advertising_stance": structured_brief.get(
                        "advertising_stance", ""
                    ),
                }
            if reference_packs_by_platform:
                critic_input["reference_packs_by_platform"] = reference_packs_by_platform
            try:
                critic_result = await self.vibe_critic.run(
                    critic_input, self.run_id, self.db
                )
            except Exception:
                # Full traceback so a vibe-critic failure isn't a mystery
                # ("why did this run skip the vibe loop?"). Still non-fatal:
                # we proceed with whatever the builder produced.
                logger.exception(
                    "Vibe critic failed, proceeding without critique"
                )
                break

            failed = critic_result.get("failed_cells", [])
            failed_ids = {f.get("cell_id") for f in failed if f.get("cell_id")}

            # ── Gemini arbitration (Plan B: 分歧仲裁) ──────────────────
            # Claude's critic often gives face-saving borderline → pass on
            # AI-tone content. Ask Gemini to re-evaluate the cells Claude
            # passed; if Gemini flags them, add to failed list.
            # Advisory-only: Gemini errors / not-configured → silently
            # keep Claude's verdict (logged at debug / warn respectively).
            claude_passed = [
                c for c in cells_to_critique
                if c.get("cell_id") not in failed_ids
            ]
            if claude_passed:
                try:
                    gemini_result = await run_gemini_critic(claude_passed)
                except Exception as e:
                    # run_gemini_critic is supposed to swallow everything.
                    # If something still leaks, treat as non-fatal.
                    logger.warning(
                        f"[gemini arbitration] unexpected exception, skipping: {e!r}"
                    )
                    gemini_result = {"verdict": "skipped", "failed_cells": []}

                gemini_verdict = gemini_result.get("verdict", "skipped")
                gemini_failed = gemini_result.get("failed_cells") or []
                # Account Gemini tokens + cost into the run totals so the UI
                # cost metric reflects multi-backend spend, even though
                # Gemini doesn't count toward MAX_TOKENS_PER_RUN.
                _usage = gemini_result.get("_gemini_usage") or {}
                if _usage.get("cost_usd") or _usage.get("input_tokens") or _usage.get("output_tokens"):
                    accumulate_auxiliary_cost(
                        self.run_id,
                        cost_usd=float(_usage.get("cost_usd", 0.0)),
                        input_tokens=int(_usage.get("input_tokens", 0)),
                        output_tokens=int(_usage.get("output_tokens", 0)),
                        source="gemini_critic",
                    )
                if gemini_verdict != "skipped" and gemini_failed:
                    # Accumulate Gemini-flagged cells into Claude's failed
                    # list so the rewriter gets a combined set.
                    # Dedup by cell_id — Gemini could theoretically flag
                    # a cell Claude also flagged (shouldn't happen in
                    # Plan B because we only show Gemini Claude's passed
                    # cells, but defend anyway).
                    existing_ids = {f.get("cell_id") for f in failed}
                    added = 0
                    for gf in gemini_failed:
                        if gf.get("cell_id") and gf["cell_id"] not in existing_ids:
                            # Tag Gemini-flagged cells so the rewrite
                            # directives are traceable in stage_logs.
                            gf = {
                                **gf,
                                "_flagged_by": "gemini",
                                "rewrite_directives": (
                                    "【Gemini 二审提出】"
                                    + (gf.get("rewrite_directives") or "")
                                ),
                            }
                            failed.append(gf)
                            added += 1
                    if added:
                        logger.info(
                            f"[gemini arbitration] Gemini flagged {added} "
                            f"additional cells Claude had passed: "
                            f"{sorted(gf.get('cell_id') for gf in gemini_failed)[:10]}"
                        )
                # Even if Gemini didn't add new fails, stash its raw
                # result on the critic_result for UI / stage_log record.
                critic_result["_gemini_arbitration"] = gemini_result

            if not failed:
                logger.info(f"Vibe critic passed all {len(prompt_cells)} cells "
                            f"on iteration {iteration + 1} (Claude + Gemini both cleared)")
                final_system["vibe_critic_result"] = critic_result
                return final_system

            logger.warning(
                f"Vibe critic iteration {iteration + 1}: "
                f"{len(failed)}/{len(prompt_cells)} cells failed"
            )

            # Build rewrite input — failed cells with their original prompts + critic feedback.
            # failed is the combined Claude + Gemini list (see arbitration
            # above); rebuild failed_ids from the full set so Gemini-added
            # cells make it into the rewrite batch.
            failed_ids = {f.get("cell_id") for f in failed if f.get("cell_id")}
            critic_map = {f["cell_id"]: f for f in failed if f.get("cell_id")}

            def _augment_rewrite_directives(c: dict, base: str) -> str:
                """If an incoming cell has a Gemini structure_hint from the
                5c+ stage, weave it into rewrite_directives so the rewriter
                addresses taste AND structural gaps in one pass instead of
                leaving the latter for the next chancellery round trip."""
                hint = c.get("_structure_hint") or {}
                missing = hint.get("missing_items") or []
                rh = hint.get("revision_hint") or ""
                if not missing and not rh:
                    return base
                addendum_parts = ["\n\n【顺带补结构】Gemini 结构审提示本 cell 还缺："]
                if missing:
                    addendum_parts.append(f"- {', '.join(str(m) for m in missing)}")
                if rh:
                    addendum_parts.append(f"- 建议：{rh}")
                return base + "\n".join(addendum_parts)

            failed_full_cells = [
                {
                    **c,
                    "rewrite_directives": _augment_rewrite_directives(
                        c,
                        critic_map[c["cell_id"]].get("rewrite_directives", ""),
                    ),
                    "severity": critic_map[c["cell_id"]].get("severity", "fail"),
                    "taste_gap": critic_map[c["cell_id"]].get("taste_gap", ""),
                    # v0.29.0: 让下游 rewriter 能读到 critic 的诊断字段,structural_rewriter
                    # 需要这些做身份/缺口手术。Gemini arbitration 过来的 cell 不一定有,
                    # 自动 fallback 到 None。
                    "root_cause_kind": critic_map[c["cell_id"]].get("root_cause_kind"),
                    "root_cause_explanation": critic_map[c["cell_id"]].get(
                        "root_cause_explanation", ""
                    ),
                    "multiplier_gate": critic_map[c["cell_id"]].get("multiplier_gate", {}),
                }
                for c in prompt_cells
                if c.get("cell_id") in failed_ids
            ]

            # v0.29.0: 按 root_cause_kind 分流 — critic-rewriter 责任边界划分
            # 的具体落地点。关闭 flag 时回到老行为(全部给 vibe_rewriter)。
            if ENABLE_STRUCTURAL_REWRITER:
                buckets = _classify_failed_cells(failed_full_cells)
                logger.info(
                    "[vibe_loop] fail routing: surface=%d template=%d "
                    "structural_identity=%d structural_gap=%d strategic=%d",
                    len(buckets["surface"]),
                    len(buckets["template"]),
                    len(buckets["structural_identity"]),
                    len(buckets["structural_gap"]),
                    len(buckets["strategic"]),
                )

                # ① strategic 类 — rewriter 改不了,记录到 final_system 让
                # 用户审查时决定是人工重跑策略层还是接受风险。硬 escalate 回
                # secretariat 留给 v0.29.x 做。
                if buckets["strategic"]:
                    strat_entries = [
                        {
                            "cell_id": fc.get("cell_id"),
                            "direction_id": fc.get("direction_id"),
                            "platform": fc.get("platform"),
                            "root_cause_explanation": fc.get("root_cause_explanation", ""),
                            "multiplier_gate": fc.get("multiplier_gate", {}),
                            "iteration": iteration + 1,
                        }
                        for fc in buckets["strategic"]
                    ]
                    final_system.setdefault("strategic_warnings", []).extend(
                        strat_entries
                    )
                    logger.warning(
                        "[vibe_loop] %d cells flagged strategic (not sending to "
                        "rewriter — needs secretariat/cell_planner revision): %s",
                        len(strat_entries),
                        [e["cell_id"] for e in strat_entries],
                    )

                # ② structural 类 — 走 structural_rewriter(叙事身份/缺口手术)
                structural_cells = (
                    buckets["structural_identity"] + buckets["structural_gap"]
                )
                structural_new_by_id: dict = {}
                if structural_cells:
                    structural_input = {
                        "failed_cells": structural_cells,
                        "shared_skeleton": shared_skeleton,
                    }
                    if reference_packs_by_platform:
                        structural_input["reference_packs_by_platform"] = (
                            reference_packs_by_platform
                        )
                    try:
                        structural_result = await self.structural_rewriter.run(
                            structural_input, self.run_id, self.db,
                        )
                        structural_new_by_id = {
                            c["cell_id"]: c
                            for c in structural_result.get("prompt_cells", [])
                        }
                    except Exception:
                        logger.exception(
                            "Structural rewriter failed; falling back to "
                            "vibe_rewriter for these cells"
                        )
                        # Fallback: merge back into surface bucket so they still
                        # get some treatment instead of being silently dropped.
                        buckets["surface"].extend(structural_cells)

                # ③ surface + template — 走原 vibe_rewriter
                vibe_cells = buckets["surface"] + buckets["template"]
                vibe_new_by_id: dict = {}
                if vibe_cells:
                    rewriter_input = {
                        "failed_cells": vibe_cells,
                        "shared_skeleton": shared_skeleton,
                    }
                    if reference_packs_by_platform:
                        rewriter_input["reference_packs_by_platform"] = (
                            reference_packs_by_platform
                        )
                    try:
                        rewritten = await self.vibe_rewriter.run(
                            rewriter_input, self.run_id, self.db,
                        )
                        vibe_new_by_id = {
                            c["cell_id"]: c
                            for c in rewritten.get("prompt_cells", [])
                        }
                    except Exception:
                        logger.exception(
                            "Vibe rewriter failed, keeping original cells for this batch"
                        )

                # Merge both rewriter outputs. If both emitted the same cell_id
                # (shouldn't happen — we bucket disjointly — but defend anyway),
                # structural wins because it's the more targeted surgery.
                new_cells_by_id = {**vibe_new_by_id, **structural_new_by_id}
            else:
                # Legacy path (flag off): everything through vibe_rewriter.
                rewriter_input = {
                    "failed_cells": failed_full_cells,
                    "shared_skeleton": shared_skeleton,
                }
                if reference_packs_by_platform:
                    rewriter_input["reference_packs_by_platform"] = (
                        reference_packs_by_platform
                    )
                try:
                    rewritten = await self.vibe_rewriter.run(
                        rewriter_input, self.run_id, self.db,
                    )
                except Exception:
                    logger.exception("Vibe rewriter failed, keeping original cells")
                    break
                new_cells_by_id = {
                    c["cell_id"]: c for c in rewritten.get("prompt_cells", [])
                }

            rewritten_ids = set(new_cells_by_id.keys())
            logger.info(
                f"Rewriters produced {len(rewritten_ids)} updated cells: "
                f"{sorted(rewritten_ids)}"
            )
            prompt_cells = [
                new_cells_by_id.get(c["cell_id"], c) for c in prompt_cells
            ]
            final_system["prompt_matrix"] = prompt_cells

            iteration += 1

            # Elastic escalation: after the initial cap is hit and we'd
            # otherwise exit, check the current failure rate. If a
            # substantial chunk is still bad, authorize one more round
            # (bounded by hard_cap=3). This stops the loop from silently
            # shipping stubborn bad-taste matrices while keeping a firm
            # ceiling on token spend.
            if iteration == max_iterations and max_iterations < hard_cap:
                fail_ratio = len(failed) / total_cells_count
                if fail_ratio >= escalate_threshold:
                    max_iterations = hard_cap
                    logger.warning(
                        f"Vibe loop: {len(failed)}/{total_cells_count} cells "
                        f"still failing ({fail_ratio:.0%} ≥ {escalate_threshold:.0%}) "
                        f"after round {iteration} — escalating cap to "
                        f"{hard_cap} for one more rewrite pass."
                    )
                else:
                    logger.info(
                        f"Vibe loop: {len(failed)}/{total_cells_count} cells "
                        f"still failing ({fail_ratio:.0%} < {escalate_threshold:.0%}), "
                        f"not escalating. Exiting."
                    )

        # Out of iterations
        logger.warning(
            f"Vibe loop exhausted {max_iterations} iterations "
            f"(hard cap {hard_cap}), proceeding with remaining issues"
        )
        return final_system

    async def _run_strategic_escalation(
        self,
        final_system: dict,
        structured_brief: dict,
        plan: dict,
    ) -> tuple[dict, dict]:
        """策略层自动升级(v0.29.1, C.2.1)。

        vibe_loop 跑完后,若留下 strategic_warnings(rewriter 改不了的
        策略层错配),就回 secretariat 修订受影响 direction 的策略锚点
        (stop_trigger / reward_type / role_embodiment / ...),然后再
        跑一次 vibe_loop 让 critic + rewriter 用新锚点重新判决。

        返回 (final_system, plan) 二元组 — plan 可能被更新。

        硬上限 STRATEGIC_LOOP_MAX_ITERATIONS(默认 1):只允许一次自动
        升级,避免 direction 来回摆动。超过后 strategic_warnings 会继续
        挂在 final_system 上,由 UI 提示用户人工处理。

        失败模式:secretariat 抛异常 → 保留原 warnings + 原 plan,不阻塞
        后续终审。critic/rewriter 重跑时的异常也同样非致命。
        """
        max_iter = STRATEGIC_LOOP_MAX_ITERATIONS
        iteration = 0

        while iteration < max_iter:
            warnings = final_system.get("strategic_warnings") or []
            if not warnings:
                return final_system, plan

            affected_direction_ids = sorted({
                w.get("direction_id") for w in warnings
                if w.get("direction_id")
            })
            if not affected_direction_ids:
                # Warnings exist but have no direction_id (old critic output
                # or Gemini-flagged). Nothing actionable at strategy layer —
                # leave warnings for user.
                return final_system, plan

            logger.warning(
                "[strategic_escalation] round %d: %d cells flagged strategic "
                "across directions %s — requesting secretariat revision",
                iteration + 1,
                len(warnings),
                affected_direction_ids,
            )

            revision_input = {
                "brief": structured_brief,
                "strategic_revision": {
                    "current_plan": plan,
                    "affected_direction_ids": affected_direction_ids,
                    "strategic_warnings": warnings,
                },
            }

            try:
                revised_plan = await self.secretariat.run(
                    revision_input, self.run_id, self.db,
                )
            except Exception:
                logger.exception(
                    "[strategic_escalation] secretariat revision failed; "
                    "keeping original plan + warnings"
                )
                return final_system, plan

            # Merge revised directions into plan + self._direction_index.
            # Only the affected direction_ids are trusted to have been updated
            # — if secretariat returned other direction_ids with changes,
            # we explicitly ignore those changes to avoid scope creep.
            revised_by_id = {
                d.get("direction_id"): d
                for d in revised_plan.get("tactical_directions", []) or []
                if d.get("direction_id")
            }
            merged_directions = []
            change_log: list[str] = []
            for d in plan.get("tactical_directions", []) or []:
                did = d.get("direction_id")
                if did in affected_direction_ids and did in revised_by_id:
                    new_d = revised_by_id[did]
                    merged_directions.append(new_d)
                    note = new_d.get("_revision_note", "")
                    change_log.append(f"{did}: {note}" if note else did)
                else:
                    merged_directions.append(d)

            plan = {**plan, "tactical_directions": merged_directions}
            self._direction_index = {
                d.get("direction_id"): d
                for d in merged_directions
                if d.get("direction_id")
            }

            # Snapshot pre-escalation warnings so they're not lost; clear
            # the active list so the next vibe_loop can repopulate it fresh.
            prior_warnings = final_system.pop("strategic_warnings", [])
            final_system.setdefault("_strategic_escalation_history", []).append({
                "iteration": iteration + 1,
                "affected_direction_ids": affected_direction_ids,
                "prior_warnings": prior_warnings,
                "change_log": change_log,
            })

            logger.info(
                "[strategic_escalation] round %d applied — re-running vibe_loop "
                "with updated direction anchors (%s)",
                iteration + 1,
                change_log or affected_direction_ids,
            )

            # Re-run vibe_loop. It reads prompt_matrix from final_system +
            # self._direction_index for ground-truth anchors, so simply
            # invoking it again picks up the new stop_trigger / reward_type.
            # Cells outside affected directions will mostly pass on round 1
            # (their anchors didn't change); affected cells will get a fresh
            # classification and the right rewriter.
            try:
                final_system = await self._run_vibe_loop(
                    final_system, structured_brief,
                )
            except Exception:
                logger.exception(
                    "[strategic_escalation] re-run vibe_loop failed; "
                    "leaving current state in place"
                )
                return final_system, plan

            iteration += 1

        # Hit iteration cap. Any warnings still present stay on
        # final_system for the UI to show to the user.
        remaining = final_system.get("strategic_warnings") or []
        if remaining:
            logger.warning(
                "[strategic_escalation] cap reached (%d iterations), "
                "%d strategic warnings remain for user review",
                max_iter,
                len(remaining),
            )
        return final_system, plan

    async def _run_consumer_simulation(
        self,
        final_system: dict,
        structured_brief: dict,
        plan: dict,
    ) -> None:
        """消费者模拟二级校验(v0.29.1, C.2.2)。

        在 vibe_loop 收敛后、终审前,用 persona_simulator 的
        `consumer_simulation` mode 针对每个 cell 以 direction.stop_trigger
        描述的目标用户身份做 stop / scroll 二元判决。这是 interest_align
        的第二层校验——critic 的 interest_align 是"专家判断",这一步是
        "用户端判断"。

        被目标用户 scroll 的 cell 会追加进 strategic_warnings 让用户审查。
        (不自动回策略升级——此时 vibe + strategic 已经跑完,再递归会过复杂。)

        Advisory-only:persona_simulator 失败时仅 log 警告,不阻塞终审。
        结果存到 final_system._consumer_simulation,UI 层渲染。
        """
        prompt_cells = final_system.get("prompt_matrix") or []
        if not prompt_cells:
            return

        direction_index = getattr(self, "_direction_index", {}) or {}

        # Build per-cell consumer context. Cells whose direction has no
        # stop_trigger fall back to target_audience from brief.
        fallback_audience = structured_brief.get("target_audience", "") or ""
        slim_cells = []
        for c in prompt_cells:
            did = c.get("direction_id")
            d = direction_index.get(did) or {}
            stop_trigger = (d.get("stop_trigger") or "").strip()
            slim_cells.append({
                "cell_id": c.get("cell_id"),
                "direction_id": did,
                "direction_name": c.get("direction_name"),
                "stop_trigger": stop_trigger or fallback_audience,
                "demo_output": (c.get("demo_output") or "")[:600],
            })

        try:
            result = await self.persona_simulator.run(
                {
                    "mode": "consumer_simulation",
                    "target_audience": fallback_audience,
                    "platform": (
                        structured_brief.get("target_platforms", [DEFAULT_PLATFORM])[0]
                        if structured_brief.get("target_platforms")
                        else DEFAULT_PLATFORM
                    ),
                    "cells": slim_cells,
                },
                self.run_id,
                self.db,
            )
        except Exception:
            logger.exception(
                "[consumer_sim] persona_simulator failed; skipping 2nd-level check"
            )
            return

        final_system["_consumer_simulation"] = result

        # Cells where the modeled consumer would scroll = interest_align
        # second-layer fail. Append to strategic_warnings so UI surfaces them.
        scrolled = [
            j for j in (result.get("judgments") or [])
            if (j.get("action") or "").lower() == "scroll"
        ]
        if scrolled:
            logger.warning(
                "[consumer_sim] %d cells would be scrolled past by modeled "
                "target consumer: %s",
                len(scrolled),
                [j.get("cell_id") for j in scrolled],
            )
            for j in scrolled:
                final_system.setdefault("strategic_warnings", []).append({
                    "cell_id": j.get("cell_id"),
                    "direction_id": j.get("direction_id"),
                    "platform": j.get("platform", ""),
                    "root_cause_explanation": (
                        "consumer_simulation: 目标用户(stop_trigger 描述者)"
                        f"判决 scroll — {j.get('reason', '')}"
                    ),
                    "multiplier_gate": {"interest_align": "fail"},
                    "iteration": "consumer_sim",
                    "source": "consumer_simulation",
                })
        else:
            logger.info(
                "[consumer_sim] all %d cells passed 2nd-level consumer check",
                len(prompt_cells),
            )


class PipelineAlreadyRunningError(RuntimeError):
    """Raised when an entry point tries to start a pipeline for a project that
    already has a thread running. Prevents double-clicks, two browser tabs,
    or multiple users from racing on the same run_id.
    """


def _assert_project_not_running(
    project_id: str,
    db: SupabaseClient,
    required_status: str | None = None,
) -> dict:
    """Re-read project status from DB (bypassing any stale in-UI cache) and
    refuse to proceed if it's already running, or if required_status is set
    and the project isn't in that state. Returns the fresh project dict on
    success.

    The race window between this check and the caller's own "status=running"
    write is small (ms) but not zero. For full correctness we'd need an
    atomic compare-and-swap in Supabase, which the Python client doesn't
    expose cleanly. Combined with the UI-level session_state lock, this
    closes the practical failure modes (user double-clicks, two tabs).
    """
    project = db.get_project(project_id) or {}
    current = (project.get("status") or "").strip()
    if current == "running":
        raise PipelineAlreadyRunningError(
            f"项目 {project_id[:8]} 当前状态为 running，已有一条流水线正在执行。"
            f"等它跑完或失败后再重试。"
        )
    if required_status and current != required_status:
        raise PipelineAlreadyRunningError(
            f"项目 {project_id[:8]} 当前状态是 {current!r}，不满足触发条件 "
            f"{required_status!r}。请刷新页面查看最新状态。"
        )
    return project


def force_cancel_pipeline(project_id: str, db: SupabaseClient) -> dict:
    """Force-reset a stuck project. Marks ALL pipeline_runs currently
    `running` for this project as `failed`, sets the project itself to
    `failed`, and writes a marker stage_log so the UI has something to
    render explaining what happened.

    Needed because Python can't cleanly kill a hung daemon thread from
    the outside — if a Gemini/Claude call is wedged in the networking
    layer, the thread will block until the TCP timeout trips (could be
    hours). The user still needs to be able to restart the pipeline.
    Marking state = failed releases the PipelineAlreadyRunningError
    guard so the "重跑流水线" / "继续执行" buttons become clickable.

    Any zombie thread that eventually wakes up will find a pipeline_run
    row in a terminal state; its later update_stage_log writes will
    succeed but land on a run nobody looks at anymore. Since "重跑流水线"
    creates a NEW run_id, there's no write collision with the new attempt.

    Returns a summary dict:
      {"cancelled_runs": int, "marker_stage_log_id": str | None}
    """
    summary: dict = {"cancelled_runs": 0, "marker_stage_log_id": None}
    now_iso = datetime.now(timezone.utc).isoformat()

    # 1. Find all running pipeline_runs for this project and mark failed
    try:
        runs = db.get_runs_for_project(project_id) or []
    except Exception as e:
        logger.warning("[force_cancel] get_runs_for_project failed: %r", e)
        runs = []

    for run in runs:
        if run.get("status") == "running":
            rid = run.get("id")
            if not rid:
                continue
            try:
                db.update_pipeline_run(
                    rid,
                    status="failed",
                    completed_at=now_iso,
                )
                summary["cancelled_runs"] += 1
                # Write a marker so the UI can render "why did this
                # stop?" without the user having to read logs.
                try:
                    log = db.create_stage_log(
                        rid,
                        "_force_cancelled",
                        {"reason": "user_force_cancel", "cancelled_at": now_iso},
                    )
                    db.update_stage_log(
                        log["id"],
                        status="failed",
                        error_message=(
                            "⛔ 用户强制终止了这条流水线。"
                            "原因通常是线程卡在某个外部调用（中转站挂起/网络超时/"
                            "Gemini 响应死循环等）。项目状态已重置为 failed，"
                            "可以点「重跑流水线」创建新 run 继续。"
                        ),
                        output_data={
                            "reason": "user_force_cancel",
                            "cancelled_at": now_iso,
                        },
                    )
                    summary["marker_stage_log_id"] = log["id"]
                except Exception as e:
                    logger.warning(
                        "[force_cancel] marker stage_log write failed: %r", e
                    )
            except Exception as e:
                logger.exception(
                    "[force_cancel] failed to mark run %s as failed: %r",
                    rid,
                    e,
                )

    # 2. Reset project status so the guards release
    try:
        db.update_project(project_id, status="failed")
    except Exception as e:
        logger.exception(
            "[force_cancel] failed to reset project status: %r", e
        )

    logger.warning(
        "[force_cancel] reset project=%s, cancelled %d running runs",
        project_id[:8] if project_id else "?",
        summary["cancelled_runs"],
    )
    return summary


def start_pipeline_in_background(project_id: str, run_id: str, db: SupabaseClient):
    """Launch pipeline in a background thread (for Streamlit compatibility)."""
    _assert_project_not_running(project_id, db)

    def _thread_target():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            orchestrator = PipelineOrchestrator(project_id, run_id, db)
            loop.run_until_complete(orchestrator.run())
        except Exception as e:
            # Two failure classes land here:
            #   (a) Exception inside orchestrator.run() — its own try/except
            #       already updated pipeline_run.status to "failed" and
            #       re-raised, so we just log.
            #   (b) Exception BEFORE orchestrator.run() got to run its try
            #       block — e.g. PipelineOrchestrator.__init__ crashed,
            #       event-loop setup failed, the thread was killed early.
            #       In that case pipeline_run.status is still whatever the
            #       caller set it to ("running") and the UI will auto-refresh
            #       forever waiting for a completion that will never come.
            # We can't always tell (a) from (b), so redundantly mark the run
            # failed here: it's idempotent if (a) already did it, and the
            # safety net for (b). Swallow DB errors so we don't crash the
            # thread cleanup.
            logger.exception("Background pipeline thread failed")
            try:
                err_str = f"{type(e).__name__}: {e}"
                db.update_pipeline_run(
                    run_id,
                    status="failed",
                    completed_at=datetime.now(timezone.utc).isoformat(),
                )
                db.update_project(project_id, status="failed")
                # Also surface the failure in stage_logs so UI has something
                # to render instead of just an empty "running" spinner.
                db.create_stage_log(
                    run_id,
                    stage_name="_thread_target",
                    input_data={"phase": "thread_init_or_teardown"},
                )
                logger.info(
                    f"[thread_target] marked run={run_id} failed with: {err_str}"
                )
            except Exception:
                logger.exception(
                    "[thread_target] also failed to mark run as failed — "
                    "run will stay in 'running' state; manual DB cleanup needed"
                )
        finally:
            loop.close()

    thread = threading.Thread(target=_thread_target, daemon=True)
    thread.start()
    return thread


_ROUTE_PRIORITY = (
    "strategic",
    "structural_identity",
    "structural_gap",
    "template",
    "surface",
)


def _classify_failed_cells(failed_cells: list[dict]) -> dict[str, list[dict]]:
    """Split failed cells by root_cause_kind for critic-rewriter routing.

    Priority (high→low): strategic > structural_identity > structural_gap
    > template > surface. If a cell has no `root_cause_kind` (Gemini-flagged
    cells or legacy critic output), default to "surface" so it still gets
    treated by vibe_rewriter. Multiplier gate is inspected as a fallback
    when root_cause_kind is missing — lets us route correctly even with an
    older critic prompt that hasn't been updated yet.
    """
    buckets: dict[str, list[dict]] = {k: [] for k in _ROUTE_PRIORITY}

    for fc in failed_cells:
        kind = (fc.get("root_cause_kind") or "").strip()

        if kind not in _ROUTE_PRIORITY:
            # Fallback classification from multiplier_gate if critic didn't
            # label root_cause_kind (e.g. Gemini arbitration entries).
            gate = fc.get("multiplier_gate") or {}
            if gate.get("interest_align") == "fail":
                kind = "strategic"
            elif gate.get("reward_signal") == "fail":
                kind = "strategic"
            elif gate.get("identity_consistency") == "fail":
                kind = "structural_identity"
            elif gate.get("gap_tension") == "fail":
                kind = "structural_gap"
            else:
                # Unknown origin (pure Gemini flag) — treat as surface.
                kind = "surface"

        buckets[kind].append(fc)

    return buckets


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


def _find_cross_cell_duplicates(cells: list[dict]) -> list[str]:
    """Find cells that share identical content with another cell in the same
    matrix. `_validate_prompt_cell` checks each cell in isolation; this adds
    the cross-cell dimension.

    Two different cells producing IDENTICAL demo_output text or IDENTICAL
    first-sentence openings is almost always a rewriter convergence bug
    (see vibe_rewriter's shared reference-sample pool — without explicit
    diversification instructions, multiple cells rewritten in the same
    batch tend to anchor to the same sample). Catching it here lets the
    orchestrator raise instead of shipping a matrix where D1 and D5 read
    the same.

    Returns: list of human-readable issue strings (empty if no duplicates).
    Matrix-level decision (hard-fail vs warn) is the caller's to make.
    """
    issues: list[str] = []
    if len(cells) < 2:
        return issues

    # 1. Identical full demo_output across cells
    demo_to_ids: dict[str, list[str]] = {}
    for c in cells:
        demo = (c.get("demo_output") or "").strip()
        if len(demo) < 30:  # ignore blanks/headers
            continue
        demo_to_ids.setdefault(demo, []).append(c.get("cell_id", "?"))
    for demo, ids in demo_to_ids.items():
        if len(ids) > 1:
            issues.append(
                f"matrix: demo_output 完全重复：{sorted(ids)} 共用同一段内容"
                f"（前 60 字：{demo[:60]!r}）"
            )

    # 2. Identical first sentence (up to first 。！？\n) across cells.
    # Tolerates different bodies but flags same opening line.
    opening_to_ids: dict[str, list[str]] = {}
    for c in cells:
        demo = (c.get("demo_output") or "").strip()
        if not demo:
            continue
        first = re.split(r"[。！？!?\n]", demo, maxsplit=1)[0].strip()
        if len(first) < 8:  # too short to count as a meaningful opening
            continue
        opening_to_ids.setdefault(first, []).append(c.get("cell_id", "?"))
    for opening, ids in opening_to_ids.items():
        if len(ids) > 1:
            # Don't double-report if already flagged as identical-full-demo
            if any(opening in msg for msg in issues):
                continue
            issues.append(
                f"matrix: 第一句完全相同：{sorted(ids)} 都以 {opening!r} 开头"
            )

    return issues


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
    """Resume a failed pipeline run — reuses existing run_id and skips completed stages.

    The double-click guard lives inside start_pipeline_in_background; we
    don't duplicate it here.
    """
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
    # 0. Guard: only allow this from needs_revision state. The check happens
    # BEFORE any destructive action (stage_logs deletion, _revision_context
    # write) because if two clicks race here, the loser would destroy the
    # winner's still-valid stage_logs. Raising early keeps the DB consistent.
    _assert_project_not_running(project_id, db, required_status="needs_revision")

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

    # 4. Reset pipeline_run status so the UI shows it as running again;
    # project.status gets flipped by orchestrator.run() as its first action,
    # so don't pre-empt it here (doing so would break the guard in
    # start_pipeline_in_background).
    db.update_pipeline_run(run_id, status="running", completed_at=None)
    return start_pipeline_in_background(project_id, run_id, db)
