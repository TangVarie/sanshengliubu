"""Pipeline orchestrator — sequences all agents, handles clarification pauses."""

from __future__ import annotations

import asyncio
import json
from typing import Any
import logging
import re
import threading
import time
from datetime import datetime, timezone
from itertools import groupby

from db.supabase_client import SupabaseClient
from pipeline.config import (
    BATCH_SAMPLE_N,
    CELL_PLANNER_BATCH_SIZE,
    CELL_PLANNER_CONCURRENCY,
    ENABLE_BATCH_SAMPLING,
    CLARIFICATION_POLL_SECONDS,
    CLARIFICATION_TIMEOUT_SECONDS,
    DEFAULT_PLATFORM,
    ENABLE_SOCIALDATAX_TREND_SCOUT_POST,
    ENABLE_SOCIALDATAX_TREND_SCOUT_PRE,
    ENABLE_STRUCTURAL_REWRITER,
    ENABLE_STRATEGIC_ESCALATION,
    ENABLE_CONSUMER_SIMULATION,
    CONSUMER_SIM_ONLY_WEAK_ALIGN,
    NARRATIVE_DIRECTOR_MAX_REBUILDS,
    PERSONA_WEAK_REQUIRES_BOTH_BACKENDS,
    STRATEGIC_LOOP_MAX_ITERATIONS,
    PIPELINE_HEARTBEAT_INTERVAL_SECONDS,
    SOCIALDATAX_TREND_SCOUT_PRE_REQUIRED,
    SOCIALDATAX_TREND_SCOUT_TARGET_COUNT,
    MAX_CHANCELLERY_REJECTIONS,
    MAX_CLARIFICATION_PER_AGENT,
    MAX_DEBATE_TURNS,
    MAX_FINAL_REJECTIONS,
    MATRIX_BATCH_CONCURRENCY,
    MATRIX_CELLS_PER_BATCH,
    PATH_LIBRARY,
    PATH_ROTATION_OFFSETS,
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
from pipeline.agents.kimi_critic import run_kimi_critic
from pipeline.agents.kimi_structure_reviewer import (
    format_revision_hints as _format_structure_hints,
    run_kimi_structure_review,
)
from pipeline.agents.socialdatax_reference_analyzer import run_reference_analyzer
from pipeline.retrieve_samples import (
    retrieve_reference_packs,
    summarize_packs_by_platform,
)
from pipeline.logger_utils import mask_secrets
from pipeline.batch_sampler import run_batch_sampling
from pipeline.prose_gate import (
    format_hard_hits_for_rewriter,
    run_prose_gate,
    scan_text as prose_scan_text,
)
from pipeline.quality_metrics import (
    check_and_flag_regression,
    deterministic_structure_audit,
    persist_quality_score,
    score_matrix,
)
from pipeline.agents.socialdatax_trend_scout import (
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
from pipeline.agents.red_blue_refiner import (
    RedBlueRefiner,  # legacy
    RedBlueRed,
    RedBlueBlue,
)
from pipeline.agents.persona_simulator import PersonaSimulator, PersonaSimulatorAlt

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
        self.red_blue_refiner = RedBlueRefiner()  # legacy(v0.30.9 起不再调用)
        # v0.30.9: 真对抗 — 红队 / 蓝队 用不同模型独立跑
        self.red_blue_red = RedBlueRed()
        self.red_blue_blue = RedBlueBlue()
        self.persona_simulator = PersonaSimulator()
        # v0.30.8: 异厂家画像副本(DeepSeek),和主 PersonaSimulator 并行跑,
        # 提供不同 distribution 的画像反应。如果 DEEPSEEK_API_KEY 没配,
        # 这个 agent 调用会报 RuntimeError,_run_persona_simulation 会软降级
        # 只用 Claude 系结果。
        self.persona_simulator_alt = PersonaSimulatorAlt()

        # vibe_loop 跨轮次累积的 cell_reviews(cell_id -> 最后一次评审)。
        # 质量评分的高分层数据源。刻意不挂在 final_system 上 —— 那个 dict
        # 会被整体透传给 chancellery_final(kimi-k3),挂上去就是白烧输入。
        # 详见 _run_vibe_loop 里的累积点注释。
        self._vibe_cell_reviews: dict[str, dict] = {}

    # ── 取消检查点 (v0.33.8) ──────────────────────────────────────────

    def _check_cancelled(self, where: str) -> None:
        """在 stage 边界查一次 run 状态,被取消就抛 PipelineCancelled。

        `force_cancel_pipeline` 把 run 标成 failed 来解开 UI 的锁,但它杀不掉
        本线程(Python 没法安全地 kill 守护线程)。没有这道检查的话,取消之后
        线程会继续把剩下几十次调用跑完 —— 按钮解锁了,钱还在烧。

        查询失败一律**当作没被取消**继续跑:一次 DB 抖动不该把一条正常的 run
        判死。宁可漏一次取消,不要误杀。
        """
        try:
            run = self.db.get_pipeline_run(self.run_id) or {}
        except Exception:
            logger.debug("[cancel-check] 查询失败,按未取消继续(%s)", where)
            return
        status = (run.get("status") or "").strip().lower()
        # paused_for_review 是请旨等待,是正常态,不算取消。
        if status and status not in ("running", "paused_for_review"):
            raise PipelineCancelled(
                f"run 状态已变为 {status!r}(多半是用户点了强制取消),"
                f"在 {where} 处停止。已完成的阶段都留着,点「继续执行」可以接着跑。"
            )

    # ── 精炼链的断点重续 (v0.33.8) ────────────────────────────────────
    #
    # 此前 resume 只覆盖了前半条链(太子/中书省/尚书省/六部/工部架构,外加格子
    # 规划和工部构建的 cell 级恢复)。**工部构建之后的整条精炼链没有任何 resume**:
    # 叙事导演、红蓝、画像 ×2、结构审、网感循环、消费者模拟、批量采样、终审 ——
    # 12 格子约 80-100 次调用,在终审挂掉后点「继续执行」会**全部重跑一遍**。
    #
    # 保护做在了错误的一半:单次最贵的工部构建有 cell 级恢复,而调用量最大的
    # 后半条链完全裸奔。体感就是"明明只差最后一步,重续却跑了十几分钟"。
    #
    # 两类阶段要分开处理:
    #   - **不改 prompt_matrix 的**(画像/结构审/消费者模拟/采样)—— 它们只往
    #     final_system 挂诊断数据,查一下 done 就能跳过,把数据恢复回去即可
    #   - **改 prompt_matrix 的**(叙事导演/红蓝/网感循环)—— 跳过它们必须同时
    #     恢复被改写过的矩阵,否则会拿未精炼的正文去出货。这类用矩阵快照

    def _restore_advisory_stage(
        self, stage: str, final_system: dict, done: dict, field: str
    ) -> bool:
        """恢复一个**不改 prompt_matrix** 的 advisory 阶段。返回 True = 已跳过。

        这类阶段只往 final_system 挂一个诊断字段,恢复它就等于恢复了全部效果。
        """
        payload = done.get(stage)
        if not payload:
            return False
        final_system[field] = payload
        logger.info("[resume] 跳过 %s(复用上次结果)", stage)
        return True

    def _checkpoint_matrix(
        self, tag: str, final_system: dict, plan: dict | None = None
    ) -> None:
        """给**改过 prompt_matrix** 的阶段打一个矩阵快照。

        存整个 prompt_matrix 而不是 diff:diff 要处理 cell 增删和字段级合并,
        复杂度和出错面都远大于直接存一份。12 格子约 50KB,一条 run 最多两次快照
        —— 用 100KB 的写入换掉 80 次 LLM 调用的重跑,这笔账很划算。
        """
        try:
            log = self.db.create_stage_log(
                self.run_id, f"_matrix_ckpt_{tag}",
                {"cells": len(final_system.get("prompt_matrix") or [])},
            )
            self.db.update_stage_log(
                log["id"], status="completed",
                output_data={
                    "prompt_matrix": final_system.get("prompt_matrix") or [],
                    # 这几个诊断字段是被快照阶段产出的,一起存,避免恢复后
                    # UI 上"跑过但看不到结果"。
                    "_narrative_director_result": final_system.get(
                        "_narrative_director_result"
                    ),
                    "_red_blue_stats": final_system.get("_red_blue_stats"),
                    "vibe_critic_result": final_system.get("vibe_critic_result"),
                    # ⚠️ 累积的逐 cell 评审必须一起存。它挂在 orchestrator
                    # **实例**上(有意的,见 _run_vibe_loop 的累积点注释:挂
                    # final_system 会被整体透传给终审白烧输入),而 resume 会
                    # new 一个新实例 —— 不存快照的话,任何走 refined_b 恢复的
                    # run,score_matrix 拿到的都是空 map,高分层覆盖恒为 0、
                    # 这条 run 对回归追踪直接作废。
                    "_vibe_cell_reviews": getattr(
                        self, "_vibe_cell_reviews", None
                    ) or {},
                    # 策略升级会改 direction 并重跑 vibe_loop,这两样是它的产物,
                    # 不存的话 refined_c 恢复后升级效果就丢了。
                    "strategic_warnings": final_system.get("strategic_warnings") or [],
                    # 策略升级会改 direction,恢复时 plan 必须跟矩阵同代,
                    # 否则下游拿旧锚点判新内容。
                    "_plan": plan,
                    # ⚠️ 策略升级会**清掉受影响 cell 的画像反应**(那些判决针对的
                    # 是升级前的 prompt,留着就是过期诊断)。但 _persona_merged
                    # 标记存的是**清理前**的完整包,恢复顺序又是"先 persona 后
                    # refined_c" —— 不把清理后的包一起存进快照的话,陈旧反应会
                    # 在恢复后复活,出现在产出和 UI 上,而对应 cell 早被重写了。
                    # 这正是本轮要治的「恢复 ≠ 重跑」在我自己的修复里又犯了一次。
                    "_persona_reactions": final_system.get("_persona_reactions"),
                },
            )
            logger.info("[checkpoint] 已存矩阵快照 %s", tag)
        except Exception:
            # 快照失败只是丢了重续能力,不该影响这一轮的产出。
            logger.warning("[checkpoint] 存 %s 快照失败(不影响本轮)", tag)

    def _restore_matrix_checkpoint(
        self, tag: str, final_system: dict, done: dict
    ) -> bool:
        """从快照恢复 prompt_matrix。返回 True = 恢复成功、对应阶段可跳过。"""
        snap = done.get(f"_matrix_ckpt_{tag}")
        if not snap or not snap.get("prompt_matrix"):
            return False
        final_system["prompt_matrix"] = snap["prompt_matrix"]
        for _k in ("_narrative_director_result", "_red_blue_stats",
                   "vibe_critic_result", "strategic_warnings",
                   "_persona_reactions"):
            if snap.get(_k) is not None:
                final_system[_k] = snap[_k]
        # 把累积评审恢复到**实例**上,不是 final_system 上 —— 恢复的位置必须
        # 和产出的位置一致,否则 score_matrix 还是读不到(它读的是 self)。
        # 快照里带了 plan(refined_c 会带)就交给调用方换回去 —— 矩阵和 plan
        # 必须同代,否则下游拿旧锚点判新内容。
        self._restored_plan = snap.get("_plan")
        _rv = snap.get("_vibe_cell_reviews")
        if isinstance(_rv, dict) and _rv:
            self._vibe_cell_reviews = dict(_rv)
            logger.info(
                "[resume] 一并恢复 %d 条逐 cell 评审(否则高分层覆盖恒为 0)",
                len(_rv),
            )
        logger.info(
            "[resume] 从快照 %s 恢复 %d 个 cell,跳过对应精炼阶段",
            tag, len(snap["prompt_matrix"]),
        )
        return True

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
            # ⚠️ 取消检查必须在**接受回复之前**。放在后面的话有竞态:两个标签页
            # 之间,如果"用户回复了请旨"和"用户点了强制取消"发生在同一个轮询间隔里,
            # 下一轮会先看到回复,然后无条件把已经 failed 的 run 和 project 改回
            # running —— 取消被静默吃掉,付费的工作继续跑。
            _cancelled_now = False
            try:
                _run_now = self.db.get_pipeline_run(self.run_id) or {}
                _st = (_run_now.get("status") or "").strip().lower()
                # 这里**必须**把 paused_for_review 当正常态 —— 本函数自己刚把
                # 状态设成它。只有既不是 running 也不是 paused 才算被取消。
                if _st and _st not in ("running", "paused_for_review"):
                    _cancelled_now = True
            except Exception:
                # 查询抖动不该中断请旨等待 —— 和 _check_cancelled 同一个取舍
                pass
            if _cancelled_now:
                raise PipelineCancelled(
                    f"请旨等待期间 run 状态变为 {_st!r}(多半是用户点了强制取消),"
                    f"停止等待。"
                )

            log = self.db.get_stage_log_by_id(log_id)
            if log and log.get("human_intervention"):
                # User has responded — resume.
                # 上面那次状态读和这里之间**还有**一个 TOCTOU 窗口:强制取消如果正好
                # 落在窗口里,无条件的 update 会把已经 failed 的 run 改回 running。
                # 所以恢复走条件 UPDATE —— 只有当前确实还停在 paused_for_review
                # 才切,判断交给数据库做,窗口就不存在了。
                if not self.db.try_resume_from_paused(self.run_id, self.project_id):
                    _st_now = ""
                    try:
                        _st_now = (
                            (self.db.get_pipeline_run(self.run_id) or {}).get("status") or ""
                        )
                    except Exception:
                        pass
                    raise PipelineCancelled(
                        f"请旨收到回复,但 run 状态已不是 paused_for_review"
                        f"(现在是 {_st_now or '未知'},多半是用户点了强制取消),"
                        f"不恢复执行。"
                    )
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
            _rc_raw = _project_brief.get("_revision_context")
            # Only honor revision context created FOR THIS run. A fresh "重跑"
            # mints a NEW run_id; a context left behind by a previous run must
            # not silently drag this run into revision mode — that injects stale
            # _revision_directives into 工部 and makes 终审 do a *delta* review
            # against a prior_review describing a prompt_matrix that no longer
            # exists, so it keeps rejecting and never converges ("从来没通过 +
            # 变得非常奇怪"). Legacy contexts written before this guard carry no
            # run_id; those are also cleared at the fresh-start trigger sites.
            if (
                _rc_raw
                and _rc_raw.get("run_id")
                and _rc_raw.get("run_id") != self.run_id
            ):
                logger.info(
                    "[revise] ignoring stale _revision_context (belongs to run "
                    "%s, this run is %s)",
                    str(_rc_raw.get("run_id"))[:8],
                    str(self.run_id)[:8],
                )
                _rc_raw = None
            self._revision_context: dict | None = _rc_raw
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
                _free_text = project.get("free_text", "") or ""
                _brief_dict = project.get("brief") or {}

                # v0.30.3 fix #1: 把截图分析文本拼进 free_text 的 [参考文件]
                # 包装,让它受 crown_prince 的 60% 硬留存规则保护。之前
                # _screenshot_analysis_text 只挂在 brief 字段上,crown_prince
                # 把它当成普通 brief 字段自由概括,几百字的 Gemini 视觉转写
                # 被压成一句话总结。
                _shot_txt = (_brief_dict.get("_screenshot_analysis_text") or "").strip()
                if _shot_txt and "[参考文件: gemini_screenshot_analysis]" not in _free_text:
                    _free_text = (
                        _free_text
                        + "\n\n[参考文件: gemini_screenshot_analysis]\n"
                        + _shot_txt
                        + "\n[/参考文件]"
                    )
                    logger.info(
                        "[crown_prince] 注入 _screenshot_analysis_text "
                        "(%d 字)进 free_text 的 [参考文件] 包装",
                        len(_shot_txt),
                    )

                # v0.30.3 fix #2: strip 流水线内部状态(如 _revision_context、
                # strategic_warnings、_strategic_escalation_history)避免重跑时
                # 这些上一轮的修订意见污染 crown_prince 的输入,让它误以为是
                # 用户原始 brief 的一部分写进 raw_materials/reference_summary。
                # 保留以下用户原始输入信号:_screenshot_analysis* /
                # _reference_post_urls / _library_sample_analyses 等。
                _internal_state_keys = (
                    "_revision_context",
                    "strategic_warnings",
                    "_strategic_escalation_history",
                    "_prior_strategic_warnings",
                    # 机器抓取的当前爆款样本 — 上一轮的 _trend_intel 若不剥离,
                    # 会以陈旧"当前爆款"身份混进 crown_prince 输入并扩散到下游。
                    # 趋势取样(step 1b)会用当前值重填,这里剥离保证不吃旧数据。
                    "_trend_intel",
                )
                _clean_brief = {
                    k: v for k, v in _brief_dict.items()
                    if k not in _internal_state_keys
                }
                if len(_clean_brief) < len(_brief_dict):
                    _stripped = sorted(
                        set(_brief_dict.keys()) - set(_clean_brief.keys())
                    )
                    logger.info(
                        "[crown_prince] stripped 内部 state 字段 %s 防止"
                        "污染上游输入",
                        _stripped,
                    )

                raw_input = {
                    "free_text": _free_text,
                    "brief": _clean_brief,
                }
                structured_brief = await self._run_with_clarification(self.crown_prince, raw_input)
                # v0.30.4 升级: 把用户原始 free_text(含 [参考文件:] 包装、
                # 截图分析等)透传给所有下游 agent,作为太子整理结果的"原始档案"。
                # foundation_common.md 的"用户原始输入访问协议"统一指引下游
                # 何时翻原文。两个字段:
                #   _user_raw_input  — 主字段(本版起)
                #   _raw_input_text  — 老字段名,保留兼容(老 prompt 可能引用)
                # 注意:_free_text 是已经包了 _screenshot_analysis_text 的版本,
                # 比 project.free_text 更全。
                if _free_text:
                    structured_brief["_user_raw_input"] = _free_text
                    structured_brief["_raw_input_text"] = _free_text  # 兼容
                # crown_prince 的输出会整体覆写 project.brief。它的结构化 schema
                # 不回显这些"用户原始高信号输入"字段,若不并回,update_project 会把
                # 它们静默丢弃 —— 尤其 _reference_post_urls(用户手贴的对标爆文
                # URL),丢了之后参考分析器(step 1a)永远拿不到 URL。setdefault:
                # 只在 crown_prince 没自己产出同名字段时补,不覆盖它的输出。
                for _k in (
                    "_reference_post_urls",
                    "_library_sample_analyses",
                    "_screenshot_analysis_text",
                ):
                    if _k in _brief_dict:
                        structured_brief.setdefault(_k, _brief_dict[_k])
                self.db.update_project(self.project_id, brief=structured_brief)

            self._check_cancelled("太子之后")

            # 1a. User-pasted reference posts (B) — if page 2 recorded
            # any xiaohongshu.com URLs onto project.brief._reference_post_urls,
            # ask Gemini to fetch each via url_context and attach the
            # results to structured_brief._reference_posts for downstream
            # strategy use. Runs before trend scout because user-specified
            # references are higher-signal than generic keyword search.
            # Advisory-only.
            await self._run_gemini_reference_analyzer(structured_brief)

            # 1b. 趋势取样（SocialDataX, A1）— 在策略规划之前拉一批当前
            # 平台的真实爆款（原文 + 互动量），注入到 structured_brief
            # 的 _trend_intel 字段。这些是**原文样本**不是趋势分析。
            # ⚠️ REQUIRED 默认开:取样失败且无可复用数据时抛
            # TrendScoutRequiredError 提前终止 run(fail-fast,见
            # SOCIALDATAX_TREND_SCOUT_PRE_REQUIRED)。
            await self._run_gemini_trend_scout_pre(structured_brief)

            # 2. 中书省 ↔ 门下省 — strategy loop (step 1: skeleton + review)
            # v0.30.10: 修复"strategy_loop 每次 resume 都强制重跑"的老 bug。
            # _strategy_loop 实际写的是 strategy_debate_N 那种 stage_log,
            # 根本没有 "secretariat" 这个 key,所以 done["secretariat"] 永远
            # 拿不到 → 每次 resume / 应用修订意见后都重跑整个策略辩论 →
            # secretariat 看到 _revision_context 可能新增 direction(D6/D7),
            # 下游 cell_planner 给新 D 生成新 cell,用户感觉"一直在跑新 D"。
            #
            # 修复:从 strategy_debate_* 反推最后一个完整 plan(取最后一个
            # 偶数 turn = secretariat 发言的轮次,plan 在 current_plan 字段)。
            if "secretariat" not in done:
                _debate_turns = sorted(
                    [
                        (int(k.rsplit("_", 1)[-1]), k)
                        for k in done.keys()
                        if k.startswith("strategy_debate_")
                        and k.rsplit("_", 1)[-1].isdigit()
                    ],
                    reverse=True,
                )
                # 只重建**已批准**的策略方案。辩论结构:偶数轮=中书省提案、
                # 奇数轮=门下省审议(verdict=approved 才收敛,见 _strategy_loop)。
                # 之前直接取最后一个偶数轮 = 拿一份门下省还没批(甚至正要驳回)的
                # 中途方案当最终稿,跳过剩余对抗轮 → 交付未批准方案。改为:找到
                # verdict==approved 的门下省(奇数)轮,取它前一个偶数轮的中书省
                # 方案(那正是被批准的那份;强制通过也写 approved,同样成立)。
                # 找不到已批准方案 → 不放进 done,让 _strategy_loop 重跑辩论
                # (重跑安全,只是多花 token,远好过交付未经审议的方案)。
                _approved_plan = None
                for _turn_num, _turn_key in _debate_turns:
                    if _turn_num % 2 == 0:
                        continue  # 偶数轮是中书省提案,这里要找门下省的批准判词
                    _verdict = (
                        (done.get(_turn_key) or {}).get("verdict") or ""
                    ).strip().lower()
                    if _verdict != "approved":
                        continue
                    # 被批准的方案 = 该门下省轮**前一轮**(偶数)的中书省提案
                    _sec_key = f"strategy_debate_{_turn_num - 1}"
                    _sec_out = done.get(_sec_key) or {}
                    _candidate_plan = _sec_out.get("current_plan") or _sec_out
                    if not isinstance(_candidate_plan, dict):
                        continue
                    if not _candidate_plan.get("tactical_directions"):
                        continue
                    _approved_plan = _candidate_plan
                    logger.info(
                        "[resume] strategy_loop reconstructed APPROVED plan from "
                        "%s(被 %s 批准)— directions=%s",
                        _sec_key, _turn_key,
                        [
                            d.get("direction_id")
                            for d in _candidate_plan.get("tactical_directions", [])
                        ],
                    )
                    break
                if _approved_plan is not None:
                    done["secretariat"] = _approved_plan
                else:
                    logger.info(
                        "[resume] 未找到已批准的 strategy_debate 方案 —— "
                        "重跑策略辩论,而不是复活一份未经审议的中途方案"
                    )

            if "secretariat" in done:
                logger.info("Resuming: skipping strategy loop (already completed)")
                plan = done["secretariat"]
            else:
                plan = await self._strategy_loop(structured_brief)

            self._check_cancelled("策略辩论之后")

            # 3. 尚书省 — dispatch
            if "dispatcher" in done:
                logger.info("Resuming: skipping dispatcher (already completed)")
                tasks = done["dispatcher"]
            else:
                dispatch_input = {"plan": plan, "brief": structured_brief}
                tasks = await self._run_with_clarification(self.dispatcher, dispatch_input)

            # 4. 六部（前五部并行，跳过已完成的）
            ministry_outputs = await self._run_ministries(tasks, structured_brief, plan, done)

            self._check_cancelled("六部之后")

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
                    f"target_platforms: {plan.get('target_platforms', [])}, "
                    f"brief.target_platforms: {(structured_brief or {}).get('target_platforms', 'MISSING')}. "
                    f"常见根因：secretariat 输出被 max_tokens 截断(plan JSON 不完整，"
                    f"target_platforms/matrix_skeleton 丢失)。检查 strategy_debate "
                    f"stage_log 是否触发了 truncation repair。"
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

            self._check_cancelled("格子规划之后")

            # 5c. 工部·构建 — generate per-cell prompts (Sonnet, batched)
            final_system = await self._run_works_builders(works_plan, structured_brief)

            if not final_system.get("prompt_matrix"):
                raise RuntimeError(
                    f"工部·构建产出为空。cell_plans count: {len(cell_plans)}, "
                    f"works_plan keys: {list(works_plan.keys())}"
                )

            self._check_cancelled("工部构建之后")

            # ── 精炼层 A:叙事导演 + 红蓝(都会改写 prompt_matrix)──────
            # 这两个跑完打一次矩阵快照。resume 时优先看有没有更靠后的快照
            # (refined_b),没有再看这个 —— 恢复了就把这两步整个跳过。
            if not self._restore_matrix_checkpoint("refined_b", final_system, done) \
                    and not self._restore_matrix_checkpoint(
                        "refined_a", final_system, done):
                # 5c+. 叙事导演（跨 cell 一致性+差异化诊断）
                # 看完整矩阵后判断：钩子类型有没有重复？人设有没有立住？
                # 正反面叙事比例对不对？如果有问题，只重跑有问题的 cell。
                await self._run_narrative_director(
                    final_system, works_plan, structured_brief
                )

                # 5c+++. 红蓝对抗精炼 — Red Team 找 AI 腔，Blue Team 做最小修复
                # 每个 cell 独立跑一次（并行，受 RPM 限速）。精修后的 demo
                # 直接替换回 prompt_matrix，所以后续 vibe/终审看到的是
                # 已经被精炼过的版本。
                await self._run_red_blue_refinement(final_system)

                # 这里是重续价值最高的一个点:上面两步 12 格子约 13-25 次调用,
                # 而它们之后还有终审等好几个可能失败的环节。
                self._checkpoint_matrix("refined_a", final_system)

            # 5c++++. 画像模拟审稿 — 3 个模拟读者对每个 cell 的 demo
            # 给出 0.5 秒反应（click/skip/save）。结果存到
            # final_system._persona_reactions 让 UI 展示，也作为
            # vibe_critic 的补充输入。
            # ⚠️ 画像模拟**不能**用通用 advisory 恢复。`done["persona_simulator"]`
            # 只是主 agent 的原始 stage 输出,而 _run_persona_simulation 真正的
            # 产物是:合并两个 backend 的 personas[]、打 _source 标记、补
            # status="ok"、再把弱 cell 扇出成 strategic_warnings。直接把原始
            # 输出塞给 _persona_reactions,这些效果全丢 —— 而且 vibe_loop 要求
            # status == "ok" 才注入画像反应,于是恢复出来的 run 会**静默地**
            # 没有画像信号。所以单独存合并后的包。
            _pm_prev = done.get("_persona_merged")
            if _pm_prev:
                final_system["_persona_reactions"] = _pm_prev.get("reactions") or {}
                for _w in (_pm_prev.get("warnings") or []):
                    final_system.setdefault("strategic_warnings", []).append(_w)
                logger.info(
                    "[resume] 跳过画像模拟(复用合并后的包 + %d 条弱 cell 告警)",
                    len(_pm_prev.get("warnings") or []),
                )
            else:
                _warn_before = len(final_system.get("strategic_warnings") or [])
                await self._run_persona_simulation(final_system, structured_brief)
                # ⚠️ 只有真跑通了才写 resume 标记。两个 backend 都失败时
                # _run_persona_simulation 是**正常返回**的(把 status 写成
                # "failed" 而不是抛异常),照写 completed 标记的话,resume 会
                # 永久跳过画像 —— 哪怕原始失败只是一次限流或短暂故障。
                # 老的"查 stage_log"逻辑在这种情况下是会重试的,不能比它更差。
                _pm_ok = (
                    (final_system.get("_persona_reactions") or {}).get("status")
                    == "ok"
                )
                try:
                    if not _pm_ok:
                        raise RuntimeError("skip-marker")
                    _pmlog = self.db.create_stage_log(
                        self.run_id, "_persona_merged",
                        {"cells": len(final_system.get("prompt_matrix") or [])},
                    )
                    self.db.update_stage_log(
                        _pmlog["id"], status="completed",
                        output_data={
                            "reactions": final_system.get("_persona_reactions") or {},
                            # 弱 cell 扇出的告警是这一步的副产物,恢复时要一起补回,
                            # 否则 resume 后的 run 少了这批策略告警。
                            "warnings": (
                                final_system.get("strategic_warnings") or []
                            )[_warn_before:],
                        },
                    )
                except Exception:
                    if _pm_ok:
                        logger.warning("[persona_sim] resume 标记落库失败")
                    else:
                        logger.warning(
                            "[persona_sim] 双 backend 都失败,**不写** resume 标记 "
                            "—— 下次继续执行时会重试(可能只是限流/短暂故障)"
                        )

            # 5c+++++. Gemini 结构审（advisory）— audits each prompt_cell for
            # structural completeness (5 pools / persona / compliance /
            # keywords / banlist / platform voice). Fires ONCE, between
            # builder and vibe. Hints get merged into the rewriter's
            # directives so the vibe stage also catches structural gaps,
            # not just taste issues. Advisory-only: any Gemini failure →
            # log warn, proceed with unchanged final_system.
            # ⚠️ 结构审也**不是**纯 advisory:它除了写 _structure_review,还会
            # 往每个 cell 上挂 `_structure_hint` —— 而那正是下游 vibe_loop 用来
            # 强制补漏、织进 rewrite_directives 的东西。只恢复 _structure_review
            # 的话,resume 出来的 run 会漏掉全部结构修复指令(且无声)。
            # 所以单独存逐 cell 的 hints,恢复时重新贴回矩阵。
            _sh_prev = done.get("_structure_hints")
            if _sh_prev:
                final_system["_structure_review"] = _sh_prev.get("review") or {}
                _apply_structure_hints(
                    final_system.get("prompt_matrix") or [],
                    _sh_prev.get("hints") or {},
                )
                logger.info(
                    "[resume] 跳过结构审,并把 %d 条逐 cell hint 重贴回矩阵",
                    len(_sh_prev.get("hints") or {}),
                )
            else:
                _sh_ok = await self._run_gemini_structure_review_stage(final_system)
                # ⚠️ 标记只在**模型那半边真跑成功**时写。模型挂了(未配置 /
                # 限流 / 解析失败)时确定性前置审照样出 hint,矩阵上看起来"有
                # 结构审结果",但模型能抓的那类问题(合规写成空话)一条没查。
                # 这时候写标记 = 下次续跑直接跳过整个结构审,那半边永久漏掉。
                # 不写标记,续跑会重试 —— 和上面 5c++++ 那些瞬时失败同一个取舍。
                if not _sh_ok:
                    logger.warning(
                        "[structure_review] 模型半边未跑成(仅确定性 hint 生效),"
                        "不写 resume 标记 —— 下次继续执行时会重试"
                    )
                else:
                    try:
                        _shlog = self.db.create_stage_log(
                            self.run_id, "_structure_hints",
                            {"cells": len(final_system.get("prompt_matrix") or [])},
                        )
                        self.db.update_stage_log(
                            _shlog["id"], status="completed",
                            output_data={
                                "review": final_system.get("_structure_review") or {},
                                "hints": {
                                    c["cell_id"]: c["_structure_hint"]
                                    for c in (final_system.get("prompt_matrix") or [])
                                    if c.get("cell_id") and c.get("_structure_hint")
                                },
                            },
                        )
                    except Exception:
                        logger.warning("[structure_review] resume 标记落库失败")

            self._check_cancelled("精炼层之后")

            # ── 精炼层 B:网感循环(会改写 prompt_matrix)────────────────
            # 有 refined_b 快照就整个跳过 —— 这一步含 critic + rewriter 最多
            # 3 轮,是精炼链里调用量第二大的。
            if not self._restore_matrix_checkpoint("refined_b", final_system, done):
                # 5d. 网感复检循环 — critic checks demo, rewriter fixes failed cells
                final_system = await self._run_vibe_loop(
                    final_system, structured_brief
                )
                self._checkpoint_matrix("refined_b", final_system)

            # 5d+. 策略层自动升级(v0.29.1, C.2.1)— 如果 vibe_loop 留下了
            # strategic_warnings(interest_align / reward_signal 级错配,
            # rewriter 改不了),回 secretariat 修订受影响 direction 的策略
            # 锚点(stop_trigger / reward_type / role_embodiment),然后再
            # 跑一次 vibe_loop 让 critic + rewriter 用新锚点重新判决。
            # 上限 STRATEGIC_LOOP_MAX_ITERATIONS(默认 1)防止死循环。
            # Feature-flagged:ENABLE_STRATEGIC_ESCALATION=False 时跳过。
            if ENABLE_STRATEGIC_ESCALATION:
                if self._restore_matrix_checkpoint("refined_c", final_system, done):
                    _rp = getattr(self, "_restored_plan", None)
                    if isinstance(_rp, dict) and _rp.get("tactical_directions"):
                        plan = _rp
                        # ⚠️ 派生索引也要一起重建。只换 plan 的话
                        # self._direction_index 还是 run() 开头按**升级前**的
                        # direction 建的,而 _run_consumer_simulation 读的正是
                        # 这个索引 —— 会拿旧的 stop_trigger 去评判升级后的 cell,
                        # 产出错误的告警送进终审。
                        # 活路径(_run_strategic_escalation)里是同时更新两者的,
                        # 恢复路径必须对齐。
                        self._direction_index = {
                            d.get("direction_id"): d
                            for d in _rp.get("tactical_directions", [])
                            if d.get("direction_id")
                        }
                        logger.info(
                            "[resume] 跳过策略升级,换回升级后的 plan 并重建"
                            "direction 索引(%d 个 direction)",
                            len(self._direction_index),
                        )
                    else:
                        logger.warning(
                            "[resume] refined_c 快照里没有 plan —— 矩阵是升级后的"
                            "但 plan 是升级前的,下游可能按旧锚点判决。"
                            "这条 run 的策略层结论请人工复核。"
                        )
                else:
                    _plan_before = plan
                    final_system, plan, _esc_ok = await self._run_strategic_escalation(
                        final_system, structured_brief, plan,
                    )
                    # ⚠️ 升级成功后必须再打一次快照。它会改 direction 的策略锚点
                    # **并重跑一整轮 vibe_loop**,而在此之前最新的快照是 refined_b
                    # (升级**前**的状态)。不补这一刀的话:后面任何阶段失败,resume
                    # 恢复的是升级前的矩阵,而 _run_strategic_escalation 因为
                    # strategic_warnings 已被消费会直接早退 —— 升级的结果就这么没了,
                    # 而且没有任何报错。
                    #
                    # 快照里带 plan:升级改的是 direction,不存的话恢复出来的矩阵
                    # 和 plan 对不上,下游按旧锚点判决。
                    #
                    # ⚠️ "plan 变了"**不等于**"升级跑完了"。secretariat 改完锚点、
                    # 但重跑 vibe_loop 抛异常时,plan 已经是新的(所以身份比较为真),
                    # 可矩阵还是按**旧**锚点改写的那份 —— 这时候打快照 = 续跑跳过
                    # 升级,新锚点下的改写永远不发生,且无声。所以要拿升级自己
                    # 报的 _esc_ok 一起判。
                    if _esc_ok and plan is not _plan_before:
                        self._checkpoint_matrix("refined_c", final_system, plan=plan)
                    elif not _esc_ok:
                        logger.warning(
                            "[strategic_escalation] 升级未整套跑完(修订或重跑 vibe "
                            "中途失败),不打 refined_c 快照 —— 下次继续执行会从"
                            "refined_b 重来一遍升级"
                        )

            # 5d++. 消费者模拟(v0.29.1, C.2.2)— 在终审前用 persona_simulator
            # 以 direction.stop_trigger 描述的具体目标用户扮演者对每个 cell
            # 做 stop / scroll 二元判决,作为 interest_align 的第二层校验。
            # 被目标用户 scroll 的 cell 会追加进 strategic_warnings 让用户审查。
            if ENABLE_CONSUMER_SIMULATION:
                # 消费者模拟复用的是 persona_simulator 这个 agent,所以它写的
                # stage_log 名字和画像模拟**撞车**,没法靠 stage 名判断跑没跑过。
                # 单独写一条 `_consumer_sim_done` 作为 resume 标记。
                _cs_prev = done.get("_consumer_sim_done")
                if _cs_prev:
                    final_system["_consumer_simulation"] = _cs_prev
                    logger.info("[resume] 跳过消费者模拟(复用上次结果)")
                else:
                    await self._run_consumer_simulation(
                        final_system, structured_brief, plan,
                    )
                    try:
                        _cslog = self.db.create_stage_log(
                            self.run_id, "_consumer_sim_done",
                            {"scope": (final_system.get("_consumer_simulation")
                                       or {}).get("_scope", "?")},
                        )
                        self.db.update_stage_log(
                            _cslog["id"], status="completed",
                            output_data=final_system.get("_consumer_simulation")
                            or {"status": "skipped"},
                        )
                    except Exception:
                        logger.warning("[consumer_sim] resume 标记落库失败")

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

            self._check_cancelled("网感循环之后")

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

            # 审计 COR-028:终审之后到落库之间原本没有任何取消检查点,
            # trend post / prose / 批量采样(约 60 次调用)/评分/保存全程
            # 不可取消。这里和下面两处补上 stage 边界检查。
            self._check_cancelled("终审之后")

            # 6.9. Gemini 网感对标（advisory, A2）— 为每个 direction 拉一批
            # 当前小红书真实帖子，存到 final_system._per_direction_references
            # 让用户在产出页做"我的 demo vs 真实爆款"的肉眼对比。不改变
            # prompt_matrix 的内容——纯观察。跑在 save_output 之前好让
            # references 一起持久化。
            await self._run_gemini_trend_scout_post(final_system, plan)

            # 6.95 (#12) 同步 demo_outputs。demo_outputs 是 works 阶段建的**并列
            # 列表**,但 vibe_loop / red_blue / 叙事导演精炼改的是
            # prompt_matrix[i].demo_output —— 不同步这个列表会让产出中心/导出显示
            # 精炼**前**的陈旧示例(且修订/重试会重复追加)。save_output 前直接从
            # (已精炼的)prompt_matrix 重建它:一 cell 一条(天然去重),output_content
            # 取精炼后的 demo_output,persona_used 从旧列表按 cell_id/方向保留。
            _old_demos = final_system.get("demo_outputs") or []
            def _demo_key(_o):
                return _o.get("cell_id") or f"{_o.get('direction_id')}|{_o.get('platform')}"
            _persona_by_key = {
                _demo_key(_d): _d.get("persona_used", "") for _d in _old_demos
            }
            _rebuilt_demos = []
            for _c in (final_system.get("prompt_matrix") or []):
                _demo_txt = (_c.get("demo_output") or "").strip()
                if not _demo_txt:
                    continue
                _rebuilt_demos.append({
                    "cell_id": _c.get("cell_id", ""),
                    "direction_id": _c.get("direction_id", ""),
                    "direction_name": _c.get("direction_name", ""),
                    "platform": _c.get("platform", ""),
                    "persona_used": _persona_by_key.get(_demo_key(_c), ""),
                    "output_content": _demo_txt,
                })
            final_system["demo_outputs"] = _rebuilt_demos

            # 6.955 机审闸 · 跨篇指纹(v0.33.5)。零 LLM 成本。
            #
            # 这一层抓的是**结构重合**,和已有的跨 cell 查重是两回事:
            #   - _find_cross_cell_duplicates / trigram Jaccard 抓【词面重合】
            #     —— 换汤不换药
            #   - 这里抓【结构重合】—— 每篇开头都时空锚定、每篇都插一句"说真的"、
            #     每篇结尾落在同一句式。词面重合度可以很低,读者一眼流水线。
            #     这是【换药不换汤】
            #
            # 蓝词和品牌词走白名单:蓝词必须**原样重复**(轮换同义词直接损伤搜索
            # 权重),它们出现在每一篇里是正确行为,不能被算成批量指纹。
            try:
                _allow_terms = set()
                for _k in ("brand_name", "product_name", "core_keywords",
                           "blue_keywords"):
                    _v = (structured_brief or {}).get(_k)
                    if isinstance(_v, str) and _v.strip():
                        _allow_terms.add(_v.strip())
                    elif isinstance(_v, list):
                        _allow_terms.update(
                            str(x).strip() for x in _v if str(x).strip()
                        )
                _prose_report = run_prose_gate(
                    final_system.get("prompt_matrix") or [],
                    allow=frozenset(_allow_terms),
                )
                final_system["_prose_gate"] = _prose_report
                _pglog = self.db.create_stage_log(
                    self.run_id, "prose_gate",
                    {"total_cells": _prose_report.get("total_cells", 0)},
                )
                _fp_hits = (
                    _prose_report.get("batch_fingerprints", {})
                    .get("fingerprint_hits", 0)
                )
                self.db.update_stage_log(
                    _pglog["id"],
                    status=(
                        "completed_warn"
                        if (_prose_report.get("failed_cells") or _fp_hits)
                        else "completed"
                    ),
                    output_data=_prose_report,
                )
                logger.info(
                    "[prose_gate] 出货前终扫:单篇 %d/%d 过 · 跨篇指纹命中 %d 项",
                    _prose_report.get("total_cells", 0)
                    - len(_prose_report.get("failed_cells") or []),
                    _prose_report.get("total_cells", 0),
                    _fp_hits,
                )
            except Exception:
                logger.exception("[prose_gate] 终扫失败(non-fatal),继续出货")

            # 6.96 批量采样验收(v0.33.3)。这是整条流水线唯一一处用 N>1 的样本
            # 验收产出的地方 —— 前面 11 道质量闸看的都是每个 cell 唯一那篇 demo,
            # 而交付物是给批量生成用的 prompt。决定"第 7 篇会不会和第 3 篇一个味"
            # 的 5 池 + 人设轮换机制,在此之前从来没被真正跑过一遍。
            #
            # 跑在终审之后是有意的:采样的是**最终出货的那版 system_prompt**。
            # 【观测,不拦】—— 不阻塞、不触发重写、不影响 verdict。没有历史分布
            # 就设阈值等于凭猜,先攒数据。详见 batch_sampler 模块 docstring。
            if ENABLE_BATCH_SAMPLING and done.get("batch_sampling"):
                # 采样是整条精炼链里调用量最大的一步(12 格子 × 5 篇 = 60 次),
                # 而它跑在最后、后面还有落库 —— 重续时最不该重跑的就是它。
                final_system["_batch_sampling"] = done["batch_sampling"]
                logger.info("[resume] 跳过批量采样(复用上次的 60 次调用结果)")
            elif ENABLE_BATCH_SAMPLING:
                # 批量采样是全链调用量最大的尾段(12 格 × 5 = 60 次),
                # 进入前再查一次取消(COR-028)。
                self._check_cancelled("批量采样之前")
                try:
                    _sampling = await run_batch_sampling(
                        final_system.get("prompt_matrix") or [],
                        structured_brief or {},
                        n=BATCH_SAMPLE_N,
                    )
                    final_system["_batch_sampling"] = _sampling
                    _slog = self.db.create_stage_log(
                        self.run_id,
                        "batch_sampling",
                        {"n_per_cell": BATCH_SAMPLE_N},
                    )
                    self.db.update_stage_log(
                        _slog["id"],
                        status=(
                            "completed" if _sampling.get("status") == "ok"
                            else "skipped"
                        ),
                        output_data=_sampling,
                    )
                    # 采样成本单独上报,和辅助层其它岗位一样不进 run token 熔断,
                    # 但要让成本回显看得见 —— 悄悄花钱比花钱更糟。
                    _su = _sampling.get("_usage") or {}
                    if _su.get("cost_usd") or _su.get("output_tokens"):
                        accumulate_auxiliary_cost(
                            self.run_id,
                            cost_usd=float(_su.get("cost_usd", 0.0)),
                            input_tokens=int(_su.get("input_tokens", 0)),
                            output_tokens=int(_su.get("output_tokens", 0)),
                            source="batch_sampling",
                        )
                    if _sampling.get("status") == "ok":
                        logger.info(
                            "[batch_sample] %d 个 cell × %d 篇 = %d 样本 · "
                            "红线 %.0f%% · 最差格子首句去重率 %s · "
                            "最差格子两两相似度 %s · $%.4f",
                            _sampling.get("cells_sampled", 0),
                            _sampling.get("n_per_cell", 0),
                            _sampling.get("total_samples", 0),
                            _sampling.get("redline_pass_rate", 0.0) * 100,
                            _sampling.get("worst_cell_unique_opening_ratio"),
                            _sampling.get("worst_cell_max_similarity"),
                            float(_su.get("cost_usd", 0.0)),
                        )
                except Exception:
                    # 采样是 advisory —— 绝不能因为它把一条跑完的 run 判死。
                    logger.exception("[batch_sample] 失败(non-fatal),继续出货")

            # 6.97 质量评分(双层评分体系)。跑在这里是有意的:此时 prompt_matrix
            # 已经过全部精炼阶段(红蓝 / 网感重写 / 结构补漏 / 策略升级),
            # 打的分就是**真正出货的那一版**的分,而不是中间态。
            #
            # 零 LLM 成本:红线层是纯 Python 判定,高分层读 vibe_loop 累积的
            # _vibe_cell_reviews。落库失败不阻塞(见 persist_quality_score)。
            #
            # 这是本仓库第一个**输出侧**的质量度量。在此之前只有输入侧遥测
            # (R-022 飞轮 audit 追"样本有没有被用上"),没有任何东西回答得了
            # "这一版比上一版好吗"。跨 run 对比的 SQL 见 docs/architecture.md 第 5 节。
            try:
                _scorecard = score_matrix(
                    final_system.get("prompt_matrix") or [],
                    getattr(self, "_vibe_cell_reviews", None) or {},
                )
                final_system["_quality_score"] = _scorecard
                persist_quality_score(self.db, self.run_id, _scorecard)
                # 回归哨兵:跟同项目历史比,掉出噪声带就写红字告警。
                # 单条 run 的分数没有意义,只有跟自己的历史比才知道是涨是跌 ——
                # 而没人会去手动比对每条 run 的两个数字。
                _reg = check_and_flag_regression(
                    self.db, self.project_id, self.run_id,
                    _scorecard, final_system,
                )
                if _reg:
                    _scorecard["_regression"] = _reg
            except Exception:
                # 评分器本身抛异常也不能把一条跑完的 run 判死 —— 它是可观测性,
                # 不是硬不变量。和 _persist_audit_findings 同一个原则。
                logger.exception(
                    "[quality_score] 评分计算失败(non-fatal),继续出货"
                )

            # 落库前最后一道取消检查(COR-028/COR-003):force_cancel 之后的
            # 旧线程绝不能再写产出、也不能再覆盖 run/project 终态。
            self._check_cancelled("保存产出之前")

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

        except PipelineCancelled as e:
            # 用户主动取消 —— 不是故障,不写 traceback marker,也不覆盖状态
            # (force_cancel 已经把 run 标成 failed 了)。写一条干净的说明,
            # 让详情页显示"这是你自己取消的"而不是一堆吓人的堆栈。
            logger.info("[cancel] %s", e)
            try:
                _clog = self.db.create_stage_log(
                    self.run_id, "_cancelled",
                    {"cancelled_at": datetime.now(timezone.utc).isoformat()},
                )
                self.db.update_stage_log(
                    _clog["id"], status="failed", error_message=str(e),
                )
            except Exception:
                logger.warning("[cancel] 取消标记落库失败(不影响结果)")
            # ⚠️ **不 re-raise**。往上抛会被 _thread_target 的通用 except 接住,
            # 那里会打一条 "Background pipeline thread failed" 的 traceback、
            # 再写一条 `_thread_target` failed 标记、并重复把 run 标成失败 ——
            # 于是用户自己点的每一次取消,在 UI 上都长得像后台线程内部崩溃,
            # 正好和这个分支想做的事相反。
            # 状态由 force_cancel_pipeline 负责(它已经把 run 标成 failed 了),
            # 这里正常返回即可。
            return
        except Exception as e:
            logger.exception("Pipeline failed")
            # Persist the failure where the UI can actually show it. run()'s
            # own RuntimeErrors (empty cells, count mismatches, aggregated部
            # failures) and any unhandled exception land here; without this
            # the detail page renders "failed but error_message empty, 请查看
            # 服务端日志" — unreachable on Streamlit Cloud. Mirror the
            # _force_cancelled marker pattern so pages/3 can render it.
            try:
                import traceback as _tb
                _detail = mask_secrets(
                    f"{type(e).__name__}: {e}\n--- traceback ---\n"
                    + "".join(_tb.format_exception(type(e), e, e.__traceback__))
                )
                if len(_detail) > 8000:
                    _detail = _detail[:7900] + "\n[... 截断于 8000 字符 ...]"
                _elog = self.db.create_stage_log(
                    self.run_id,
                    "_pipeline_error",
                    {"failed_at": datetime.now(timezone.utc).isoformat()},
                )
                self.db.update_stage_log(
                    _elog["id"],
                    status="failed",
                    error_message=(
                        "流水线在编排层抛出未被单个阶段捕获的错误（常见于策略/"
                        "工部阶段的校验失败：重建 cell 为空、cell 数量对不上、"
                        "六部有部执行失败等）。完整 traceback 见下：\n\n"
                        + _detail
                    ),
                )
            except Exception as _persist_err:
                logger.warning(
                    "[run] failed to persist _pipeline_error marker: %r",
                    _persist_err,
                )
            # 审计 COR-003(轻量守卫):失败回写前确认这条 run 还归本线程管。
            # force_cancel 已把 run 标 failed、同项目可能已有新 run 接管项目,
            # 旧线程此刻再无条件把 project 写 failed 就是跨 run 串写。run 状态
            # 已不是 running/paused_for_review 说明状态所有权已移交,只记日志。
            _still_owned = True
            try:
                _cur = self.db.get_pipeline_run(self.run_id) or {}
                _cur_status = (_cur.get("status") or "").strip().lower()
                _still_owned = _cur_status in ("running", "paused_for_review")
            except Exception:
                pass  # 查询失败按仍归本线程处理,保持旧行为(宁可多写不悬空)
            if _still_owned:
                self.db.update_pipeline_run(
                    self.run_id,
                    status="failed",
                    completed_at=datetime.now(timezone.utc).isoformat(),
                )
                self.db.update_project(self.project_id, status="failed")
            else:
                logger.info(
                    "[run] run %s 已被外部终结(force_cancel/收割),"
                    "跳过失败状态回写以免覆盖新 run 的项目状态",
                    self.run_id,
                )
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
                # v0.30.5 fix H2: 把 Gemini 趋势取样的真实小红书帖子原文
                # (_trend_intel.formatted_block)显式注入,之前是 dead drop。
                # secretariat.md 的"趋势取样校准"段会读 trend_intel_block。
                _trend_block = (
                    (brief.get("_trend_intel") or {}).get("formatted_block") or ""
                )
                if _trend_block:
                    input_data["trend_intel_block"] = _trend_block
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

                # v0.33.6: 最后一轮**照常调用门下省**,只把 verdict 覆写成
                # approved,而不是凭空合成一个批准。
                #
                # 原来的写法是"到最后一轮直接编一个 approved 出来,不调模型"。
                # 配合 MAX_DEBATE_TURNS=4 后果很严重:turn 0 提案 → turn 1 审议
                # (唯一一次真审) → turn 2 中书省按意见修订 → turn 3 命中
                # `turn >= max_turns - 1` 直接橡皮图章。也就是**修订后的方案
                # 从来没有被审过**,而修订恰恰是最需要复核的那一版。
                #
                # 现在:最后一轮仍然真跑一次审议,拿到 challenges 后再强制放行并
                # 把风险写进 overall_assessment。多花一次 deepseek-v4-flash 调用
                # (最便宜的一档),换回"交付的方案一定被对抗性看过至少一遍"。
                _is_last_chancellery_turn = turn >= max_turns - 1
                agent = self.chancellery
                orig_stage = agent.stage_name
                agent.stage_name = stage_name
                try:
                    response = await agent.run(input_data, self.run_id, self.db)
                except Exception as _dbt_err:
                    # 最后一轮审议挂了不能把整条 run 拖死 —— 退回旧的合成批准,
                    # 但把失败原因写进 assessment,不假装审过。
                    if not _is_last_chancellery_turn:
                        raise
                    logger.warning(
                        "[strategy_debate] 末轮审议调用失败,退回合成批准: %r",
                        _dbt_err,
                    )
                    response = {
                        "verdict": "approved",
                        "challenges": [],
                        "overall_assessment": (
                            f"⚠️ 末轮审议调用失败({type(_dbt_err).__name__}),"
                            "本方案**未经最终对抗性复核**即放行,交付前请人工过一遍"
                        ),
                    }
                finally:
                    agent.stage_name = orig_stage

                if _is_last_chancellery_turn and (
                    (response.get("verdict") or "").strip().lower() != "approved"
                ):
                    _held = response.get("challenges") or []
                    logger.warning(
                        "[strategy_debate] 末轮门下省仍有 %d 条质疑,"
                        "但已达轮次上限 —— 强制放行并把质疑写进 assessment",
                        len(_held),
                    )
                    response = {
                        **response,
                        "verdict": "approved",
                        "_forced_pass": True,
                        "overall_assessment": (
                            f"⚠️ 辩论达最大轮次({max_turns})强制通过。"
                            f"门下省末轮仍提出 {len(_held)} 条未解决的质疑,"
                            f"已保留在 challenges 字段,交付前请人工复核:"
                            + "；".join(
                                str(c.get("point") or c)[:60] for c in _held[:3]
                            )
                            + (f"（原评语）{response.get('overall_assessment', '')}"
                               if response.get("overall_assessment") else "")
                        ),
                    }

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

                # v0.33.8: 无质疑即收敛。门下省这一轮一条 challenge 都没提,
                # 说明对抗已经跑到头了 —— 再走一个「中书省修订 → 门下省再审」
                # 的完整往返,中书省要重吐一次完整大 plan(跑 k3 的 $15/1M 输出
                # 档,这条链上最贵的部分),换回来的多半是同一份方案。
                #
                # 只在**非末轮**做这个判断:末轮本来就要放行,不需要提前。
                _challenges = response.get("challenges") or []
                if (
                    verdict != "approved"
                    and not _challenges
                    and not _is_last_chancellery_turn
                ):
                    logger.info(
                        "[strategy_debate] turn %d 门下省未提出任何质疑,"
                        "提前收敛(省下一个完整往返)", turn,
                    )
                    verdict = "approved"
                    # ⚠️ 必须把合成的批准**写回 stage_log**。resume 时重建已批准
                    # 方案的逻辑只认落库的 `verdict == "approved"`(见 run() 里
                    # 那段反推);只改内存里的 verdict 的话,任何下游失败后 resume
                    # 都找不到已批准方案,于是整个策略辩论重跑一遍 —— 本来是为了
                    # 省钱做的提前收敛,反而变成了重复付费。
                    response = {
                        **response,
                        "verdict": "approved",
                        "_early_converged": True,
                        "overall_assessment": (
                            "门下省本轮未提出任何质疑,提前收敛。"
                            + (response.get("overall_assessment") or "")
                        ),
                    }
                    # 这一轮的 stage_log 是 BaseAgent.run() 内部建的,拿不到
                    # log_id,只能按 stage 名回查最后一条再更新。
                    try:
                        _turn_logs = self.db.get_stage_logs(
                            self.run_id, stage_name
                        ) or []
                        if _turn_logs:
                            # ⚠️ **同时**要把 status 改成 completed。这一轮如果
                            # 触发过请旨且超了 MAX_CLARIFICATION_PER_AGENT,
                            # _run_with_clarification 会带着部分产出返回,而那条
                            # stage_log 还停在 needs_input ——
                            # _load_completed_stages 只收 status=="completed" 的行,
                            # 于是"已落库的批准"其实读不出来,下游一失败还是重跑
                            # 整个策略辩论。只改 output_data 是个不完整的修复。
                            self.db.update_stage_log(
                                _turn_logs[-1]["id"],
                                status="completed",
                                output_data=response,
                            )
                        else:
                            logger.warning(
                                "[strategy_debate] 找不到 %s 的 stage_log,"
                                "合成批准没落库", stage_name,
                            )
                    except Exception:
                        logger.warning(
                            "[strategy_debate] 合成批准回写失败 —— resume 时"
                            "可能重跑辩论(不影响本轮结果)"
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

        # return_exceptions=True so ALL 六部 settle before we decide —
        # plain gather would propagate the first failure while the other
        # ministries keep running in the background (leaking tokens + rate
        # slots) and their real error is masked by whoever raised first.
        results = await asyncio.gather(
            *[run_one(name) for name in self.ministries],
            return_exceptions=True,
        )
        pairs: list[tuple[str, Any]] = []
        errors: list[str] = []
        for name, res in zip(self.ministries, results):
            if isinstance(res, BaseException):
                errors.append(f"{name}: {res}")
            else:
                pairs.append(res)
        if errors:
            # Required stage — surface an aggregated error naming every部
            # that failed (not just the first) so the failed run is diagnosable.
            raise RuntimeError("六部并行执行失败：" + "；".join(errors))

        return {name: output for name, output in pairs}

    def _reconstruct_active_cells(self, plan: dict, brief: dict | None = None) -> list[dict]:
        """Compute the active cell list from the plan.

        Reconciles secretariat's active_cells with the D×P (directions ×
        platforms − excluded) reconstruction.  Historical bug: model
        sometimes only emits D1 cells and omits the rest.  Fix: splice
        in missing (direction_id, platform) pairs without dropping what
        secretariat intentionally kept.

        Returns the final active_cells list (may be empty).
        """
        # `or {}` guards a JSON-null matrix_skeleton: plan.get(key, {}) returns
        # None (not the default) when the key exists with value null, so a bare
        # .get() would AttributeError and hard-fail the whole run instead of
        # falling through to the D×P rebuild below. Mirrors line 949.
        active_cells = (plan.get("matrix_skeleton", {}) or {}).get("active_cells", []) or []

        directions = plan.get("tactical_directions", []) or []
        platforms = plan.get("target_platforms", []) or []
        excluded = (plan.get("matrix_skeleton", {}) or {}).get("excluded_cells", []) or []

        # v0.30.13 防御:secretariat 偶发输出截断会丢掉 target_platforms,
        # 让 D×P 重建得 0 cell、整条流水线直接崩(见 _use_thinking 注释里的
        # debate-thinking 截断 case)。target_platforms 为空时退回 brief 里
        # crown_prince 提取的原始平台列表兜底,并 warning 暴露 plan 残缺 ——
        # 比直接 RuntimeError 好,至少能用 brief 平台把 cell 撑起来。
        if not platforms and brief:
            _brief_platforms = (brief.get("target_platforms") or [])
            if _brief_platforms:
                logger.warning(
                    "plan.target_platforms 为空(secretariat 输出疑似被截断),"
                    "回退到 brief.target_platforms=%s 重建 cells",
                    _brief_platforms,
                )
                platforms = _brief_platforms

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
            # ⚠️ 必须同时按 cell_id 去重,不能只比 (direction, platform_key)。
            #
            # 踩过的坑:中书省把 platform 写成「小红书」,而 brief 的
            # target_platforms 里是 "xiaohongshu"(或反过来)。两边
            # _platform_key 出来不相等 → 所有 pair 都被判成"缺失" → 9 个方向
            # 原样再 splice 一遍 → active_cells 变成 18 条,而且【cell_id 完全
            # 重复】(D1_xiaohongshu 出现两次)。下游 builder 老老实实把每个
            # 格子建两遍,token 直接翻倍。
            #
            # 日志上的指纹很好认:missing dirs 是空的(方向一个不缺),却说
            # "missing N pairs" —— 方向都在,只是平台名对不上。
            existing_ids = {
                str(c.get("cell_id"))
                for c in active_cells
                if isinstance(c, dict) and c.get("cell_id")
            }
            splice_in = [
                cell for cell in expected_cells
                if (cell["direction_id"], _platform_key(cell["platform"]))
                not in existing_pairs
                and cell["cell_id"] not in existing_ids
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
            elif expected_cells and not (
                {(c["direction_id"], _platform_key(c["platform"]))
                 for c in expected_cells} & existing_pairs
            ):
                # pair 一个都对不上但 cell_id 全对得上 = 平台命名不一致。
                # 上面的 cell_id 去重已经挡住了重复建格子,这里只是把根因
                # 喊出来,免得下次又要靠翻日志反推。
                logger.warning(
                    "active_cells 与 D×P 重建的 platform 命名不一致"
                    "(中书省: %s / target_platforms: %s),已按 cell_id 去重避免"
                    "重复建格。建议统一 brief.target_platforms 和 secretariat "
                    "输出里的平台写法。",
                    sorted({str(c.get("platform")) for c in active_cells
                            if isinstance(c, dict)})[:5],
                    sorted({str(c["platform"]) for c in expected_cells})[:5],
                )

        # 最后再按 cell_id 去一次重(保序)。上面的 splice 已经防住了主要来源,
        # 但中书省自己也可能在 active_cells 里写出重复 cell_id —— 重复一条就
        # 意味着整格子多建一次,是最贵的一类脏数据,兜住成本极低。
        _seen_ids: set[str] = set()
        _deduped: list[dict] = []
        _dupes: list[str] = []
        for c in active_cells:
            if not isinstance(c, dict):
                continue
            _cid = str(c.get("cell_id") or "")
            if _cid and _cid in _seen_ids:
                _dupes.append(_cid)
                continue
            if _cid:
                _seen_ids.add(_cid)
            _deduped.append(c)
        if _dupes:
            logger.warning(
                "active_cells 里有 %d 个重复 cell_id,已去重(重复的: %s)。"
                "每个重复项都会让工部把同一个格子多建一遍。",
                len(_dupes), sorted(set(_dupes))[:10],
            )
        active_cells = _deduped

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
            f"active_cells: {(plan.get('matrix_skeleton', {}) or {}).get('active_cells', 'MISSING')!r:.200s}, "
            f"tactical_directions count: {len(plan.get('tactical_directions', []))}, "
            f"target_platforms: {plan.get('target_platforms', [])}"
        )

        active_cells = self._reconstruct_active_cells(plan, brief)
        if not active_cells:
            logger.error(
                "No active_cells found and could not reconstruct. "
                f"plan keys: {list(plan.keys())}, "
                f"matrix_skeleton: {plan.get('matrix_skeleton', 'MISSING')}, "
                f"brief.target_platforms: {(brief or {}).get('target_platforms', 'MISSING')}"
            )
            return []

        shared_skeleton = works_arch.get("shared_skeleton", {})
        semaphore = asyncio.Semaphore(CELL_PLANNER_CONCURRENCY)

        # ── 8 条突破路径的跨批次预分配(原 M2 遗留项)────────────────────
        # cell_planner 分批并行跑,批次之间看不到对方选了什么路径 —— 而路径
        # 组合正是"几个方向的打法不重样"的核心机制,撞车既必然又不可见。
        # 这里在分批**之前**按 direction_id 确定性分配好,塞进每个批次的输入。
        # 零 LLM 成本,跨批次一致性由代码保证而不是靠模型自觉。
        # 按 direction 而非按 cell 分配的理由见 config.PATH_LIBRARY 的注释。
        _path_allocation = _allocate_paths_by_direction(active_cells)
        if _path_allocation:
            logger.info(
                "[cell_planner] 路径预分配(%d 个方向): %s",
                len(_path_allocation),
                {k: "/".join(v) for k, v in list(_path_allocation.items())[:8]},
            )

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
                    # 单 cell 重试也要带全量分配 —— 否则这个 cell 会在没有任何
                    # 跨方向视野的情况下重挑路径,重试出来的格子反而最容易撞车。
                    "_path_allocation": _path_allocation,
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
            except RunBudgetExceededError:
                raise  # 预算熔断必须冒泡到顶层硬停,不能被下面的通用 except
                       # 吞成"这个 cell 没返回"——那会把真实死因(token 爆了)
                       # 换成一句误导的"模型三轮都没返回",还白烧后续批次的重试。
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
                        # 跨批次路径预分配。传【全量】(所有方向,不只本批次的)
                        # 是有意的:让本批次能看到别的批次分到了什么,自己就不会
                        # 无意中往同一组路径上收敛。多传几十字节换掉一个必然的
                        # 撞车,是划算的。
                        "_path_allocation": _path_allocation,
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
                    except RunBudgetExceededError:
                        raise  # 同上:预算熔断不能被吞成"批次重试失败"
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
            *[plan_batch(b, i) for i, b in enumerate(batches)],
            return_exceptions=True,
        )
        _batch_errs = [str(r) for r in results if isinstance(r, BaseException)]
        if _batch_errs:
            # Let every batch settle before failing (no leaked in-flight
            # calls) and aggregate all batch failures, not just the first.
            raise RuntimeError(
                "cell_planner 批次执行失败：" + "；".join(_batch_errs)
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

    async def _run_works_builders(
        self, works_plan: dict, brief: dict | None = None
    ) -> dict:
        """Run WorksBuilder in batches with concurrency control.

        Smart batching: same-platform cells are grouped together for context reuse.
        Builder receives shared_skeleton + cell_plans (with ministry_digest) +
        a slim brief context(v0.30.5 fix H1):核心字段 target_audience /
        competitive_context / constraints / core_claim,以及关键的
        `_user_raw_input` 让 builder 在写 demo 时能直接翻用户原文找具体
        细节(数字 / 用户原话 / 截图分析等),不至于因为 cell_plan 里
        ministry_digest 是三层总结(太子→部→cell_planner)就只能写"效果显著"。
        """
        cell_plans = works_plan.get("cell_plans", [])
        shared_skeleton = works_plan.get("shared_skeleton", {})
        semaphore = asyncio.Semaphore(MATRIX_BATCH_CONCURRENCY)

        # v0.30.5 fix H1: 给 builder 一个 slim brief 让它能写细节。
        # 不传整个 brief(里面 trend_intel / reference_posts / 内部 state 一堆
        # 无关字段),只传 builder 实际能用上的:核心策略 + 用户原文。
        # _user_raw_input 是关键——builder 写 demo 时如果发现 cell_plan
        # 里没有具体数字/原话/产品参数,可以直接 grep 原文。
        _brief = brief or {}
        _slim_brief_for_builder = {
            "product_name": _brief.get("product_name", ""),
            "product_category": _brief.get("product_category", ""),
            "core_claim": _brief.get("core_claim", ""),
            "target_audience": _brief.get("target_audience", ""),
            "campaign_objective": _brief.get("campaign_objective", []),
            "competitive_context": _brief.get("competitive_context", ""),
            "constraints": _brief.get("constraints", ""),
            "_user_raw_input": _brief.get("_user_raw_input", "")
                or _brief.get("_raw_input_text", ""),
        }

        logger.info(
            f"Works builder: {len(cell_plans)} cell_plans, "
            f"shared_skeleton keys: {list(shared_skeleton.keys()) if shared_skeleton else 'EMPTY'},"
            f" slim_brief: raw_input={len(_slim_brief_for_builder['_user_raw_input']):,} 字"
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
                    "brief": _slim_brief_for_builder,  # v0.30.5 H1
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
            except RunBudgetExceededError:
                raise  # 预算熔断必须冒泡到顶层硬停,不能被下面的通用 except
                       # 吞成"这个 cell 没返回"——那会把真实死因(token 爆了)
                       # 换成一句误导的"模型三轮都没返回",还白烧后续批次的重试。
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
                        "brief": _slim_brief_for_builder,  # v0.30.5 H1
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
                    except RunBudgetExceededError:
                        raise  # 同上:预算熔断不能被吞成"批次重试失败"
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
            *[build_batch(b, i) for i, b in enumerate(batches)],
            return_exceptions=True,
        )
        _batch_errs = [str(r) for r in results if isinstance(r, BaseException)]
        if _batch_errs:
            raise RuntimeError(
                "works_builder 批次执行失败：" + "；".join(_batch_errs)
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
        self, final_system: dict, works_plan: dict, brief: dict | None = None
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

        # v0.33.2: 重建设上限。诊断很便宜(2101 字符提示词、只看 demo 前 500 字),
        # 贵的全在重建 —— 每个 flagged cell 都要重跑一次 works_builder,
        # 而那是全流水线 token 消耗第一的 stage。诊断出 8 个问题就等于把
        # 最贵的 stage 又跑 8 次。
        #
        # 它管的"钩子重复/跨 cell 撞车"另有两处覆盖:`_find_cross_cell_duplicates`
        # (确定性、零成本、跑两遍)和 vibe_critic 第 3 步的跨 cell 一致性检查。
        # 三重覆盖里只有这一路会触发最贵的重建,所以这里限流。
        #
        # 按 severity 排序后取前 N 个:模型没给 severity 时按原顺序(它通常
        # 已经把最严重的排在前面)。被截掉的写进 review 让终审和 UI 看得见,
        # 不静默丢弃 —— 无声截断会被读成"这些问题不存在"。
        _cap = NARRATIVE_DIRECTOR_MAX_REBUILDS
        _deferred: list[dict] = []
        if _cap is not None and len(cells_to_revise) > _cap:
            # ⚠️ 字段名是 `priority` 不是 `severity` —— narrative_director.md
            # 的输出契约里写的是 priority(high/medium)。读错字段会让所有条目
            # 落到同一个兜底档,排序变成 no-op,cap 就成了"按模型返回顺序砍",
            # 后面的 high 可能被前面的 medium 挤掉。两个名字都认,priority 优先。
            _sev_rank = {"high": 0, "critical": 0, "medium": 1, "low": 2}
            _sorted = sorted(
                cells_to_revise,
                key=lambda r: _sev_rank.get(
                    ((r.get("priority") or r.get("severity") or "")
                     .strip().lower()),
                    1,
                ),
            )
            _deferred = _sorted[_cap:]
            cells_to_revise = _sorted[:_cap]
            logger.warning(
                "[narrative_director] 诊断出 %d 个待修 cell,按上限只重建前 %d 个"
                "(重建=重跑 works_builder,最贵的 stage);其余 %d 个转 "
                "strategic_warnings 交人工: %s",
                len(_deferred) + _cap, _cap, len(_deferred),
                [c.get("cell_id") for c in _deferred],
            )
            # ⚠️ 必须同步裁剪 review 自己的 cells_to_revise。终审的
            # narrative_director_summary.cells_rebuilt 是从这个列表推出来的
            # (见 agents/chancellery.py::run_final_review),不裁剪的话被推迟的
            # cell 会以"已重建"的身份呈给终审 —— 而 chancellery.md 明写
            # 「cells_rebuilt 非空 → 优先抽查它们的 demo 看问题是否真解决」,
            # 于是终审会去抽查一个根本没重建过的 cell,那道检查就废了。
            review["cells_to_revise"] = list(cells_to_revise)
            review["_deferred_rebuilds"] = [
                {
                    "cell_id": c.get("cell_id"),
                    "issue": c.get("issue", ""),
                    "fix_instruction": c.get("fix_instruction", ""),
                }
                for c in _deferred
            ]
            for c in _deferred:
                final_system.setdefault("strategic_warnings", []).append({
                    "cell_id": c.get("cell_id"),
                    "type": "narrative_rebuild_deferred",
                    "root_cause_explanation": (
                        "叙事导演诊断出跨 cell 问题,但本轮重建已达上限 "
                        f"({_cap} 个) 未修复 —— {c.get('issue', '')}"
                    ),
                    "source": "narrative_director",
                })

        # Selective rebuild: for each flagged cell, inject the director's
        # fix_instruction into the builder's input as a _narrative_directive
        # and re-run just that cell. Reuse the existing single-cell builder
        # flow from _run_works_builders (build_single_cell).
        cell_plans_by_id = {
            c.get("cell_id"): c
            for c in works_plan.get("cell_plans", [])
        }
        shared_skeleton = works_plan.get("shared_skeleton", {})

        # Rebuild flagged cells in PARALLEL (bounded) — each targets a
        # distinct cell_id and is independent, so the old serial `for` loop
        # (the only cell stage that wasn't parallelized) just added
        # per-flagged-cell latency to the critical path. Collect results,
        # then splice back by cell_id (order-independent).
        _sem = asyncio.Semaphore(MATRIX_BATCH_CONCURRENCY)

        async def _rebuild_one(revision: dict):
            cid = revision.get("cell_id")
            fix = revision.get("fix_instruction", "")
            if not cid or not fix:
                return None
            cell_plan = cell_plans_by_id.get(cid)
            if not cell_plan:
                return None
            async with _sem:
                logger.info(
                    "[narrative_director] rebuilding %s: %s", cid, fix[:100]
                )
                try:
                    # v0.30.5 H1: rebuild 时也要给 brief,否则修订后写出来的
                    # demo 仍然只能用 cell_plan 三层总结,看不到原文细节。
                    _brief_for_rebuild = {
                        "product_name": (brief or {}).get("product_name", ""),
                        "core_claim": (brief or {}).get("core_claim", ""),
                        "target_audience": (brief or {}).get("target_audience", ""),
                        "competitive_context": (brief or {}).get("competitive_context", ""),
                        "_user_raw_input": (
                            (brief or {}).get("_user_raw_input")
                            or (brief or {}).get("_raw_input_text", "")
                        ),
                    } if brief else {}
                    rebuild_input = {
                        # KEY 必须是 cell_plans —— works_builder 的硬契约(见
                        # works_builder.md)数的是 cell_plans 数组长度做 1:1 自检。
                        # 之前误写 active_cells(那是 D×P skeleton 的术语),builder
                        # 收到 0 个 cell_plans → 输出空 prompt_cells → splice-back
                        # 匹配不到 cell_id → 修订被静默丢弃。
                        "cell_plans": [cell_plan],
                        "shared_skeleton": shared_skeleton,
                        "brief": _brief_for_rebuild,
                        "_batch_info": {
                            "label": f"叙事导演修复 {cid}",
                            "round": "narrative_fix",
                            "cell_ids": [cid],
                        },
                        "_narrative_directive": fix,
                        "_strict_contract": (
                            f"叙事导演要求修改 cell {cid}：{fix}\n"
                            f"你的 prompt_cells 必须有且只有 1 个 cell,其 cell_id "
                            f"必须严格等于 {cid}。在保留原有合规/关键词/人设的前提下,"
                            f"按指令调整 demo_output 的钩子结构或叙事方式。"
                        ),
                    }
                    rebuilt = await self.works_builder.run(
                        rebuild_input, self.run_id, self.db
                    )
                    for nc in (rebuilt.get("prompt_cells") or []):
                        if nc.get("cell_id") == cid:
                            return (cid, nc)
                    return None
                except Exception as e:
                    logger.warning(
                        "[narrative_director] rebuild %s failed: %r", cid, e
                    )
                    return None

        _results = await asyncio.gather(
            *[_rebuild_one(r) for r in cells_to_revise],
            return_exceptions=True,
        )
        _idx_by_id = {c.get("cell_id"): i for i, c in enumerate(prompt_cells)}
        for _r in _results:
            if isinstance(_r, BaseException) or not _r:
                continue
            _cid, _nc = _r
            _i = _idx_by_id.get(_cid)
            if _i is not None:
                # 校验后才覆盖:截断/缺字段的重建 cell 不能盖掉一个已验证的原 cell
                # (否则修订反而把好 cell 换成残缺 cell)。无效则保留原 cell + warn。
                _valid, _reasons = _validate_prompt_cell(_nc)
                if not _valid:
                    logger.warning(
                        "[narrative_director] rebuilt %s 未过校验 %s,保留原 cell 不覆盖",
                        _cid, _reasons,
                    )
                    continue
                prompt_cells[_i] = _nc
                logger.info("[narrative_director] rebuilt %s OK", _cid)

        final_system["prompt_matrix"] = prompt_cells

    async def _run_red_blue_refinement(self, final_system: dict) -> None:
        """Red-Blue adversarial refinement: for each cell, Red Team
        attacks AI-tone issues in demo_output, Blue Team fixes them
        with minimal changes. Combined in a single agent call per cell.

        Runs cells in parallel (bounded by rate limiter). Refined
        demo_output replaces the original in prompt_matrix in place.

        v0.29.2: 即使某个 cell 没改动/失败/跳过,也会在 cell 上写
        `_red_blue_summary` + `_red_blue_status`,并在 final_system
        上写 `_red_blue_stats` 总计,让 UI 能区分"没跑"/"跑了没改"/
        "跑了失败"——此前这三种状态在前端完全一样(啥都没有)。
        """
        prompt_cells = final_system.get("prompt_matrix") or []
        if not prompt_cells:
            final_system["_red_blue_stats"] = {
                "attempted": 0, "refined": 0, "unchanged": 0, "failed": 0,
                "note": "prompt_matrix 为空,跳过红蓝精炼",
            }
            return

        semaphore = asyncio.Semaphore(RED_BLUE_CONCURRENCY)

        async def refine_one(cell: dict) -> tuple[dict | None, str | None]:
            """v0.30.9: 真异模型对抗(Red=Opus 4.6 → Blue=Sonnet 3.7)。

            Two-step:
              1. 红队跑(找 attacks)
              2. attacks 非空才调蓝队(给 fixes + 修复 demo);为空跳过省 token

            Return (result_dict_or_None, error_str_or_None)。result 字段保持
            和老版兼容: attacks / fixes / refined_demo_output /
            refined_system_prompt / changes_summary。
            """
            cid = cell.get("cell_id", "?")
            async with semaphore:
                _common_input = {
                    "cell_id": cid,
                    "direction_name": cell.get("direction_name", ""),
                    "platform": cell.get("platform", ""),
                    "system_prompt": cell.get("system_prompt", ""),
                    "demo_output": cell.get("demo_output", ""),
                    "paradigm": cell.get("paradigm", "A_emotional_hook"),
                }

                # Step 1: 红队找 attacks
                try:
                    red_result = await self.red_blue_red.run(
                        _common_input, self.run_id, self.db,
                    )
                except RunBudgetExceededError:
                    raise  # 预算熔断必须冒泡到顶层硬停,不能被下面的通用 except 吞成单 cell 错误
                except Exception as e:
                    logger.warning("[red_blue] cell %s 红队失败: %r", cid, e)
                    return None, f"red:{type(e).__name__}: {e}"

                attacks = red_result.get("attacks") or []
                red_summary = red_result.get("_red_summary", "")

                # Red Team 找不到问题 → 直接 unchanged 退出,省蓝队那次 API 调用
                if not attacks:
                    return ({
                        "cell_id": cid,
                        "attacks": [],
                        "fixes": [],
                        "refined_demo_output": "",
                        "refined_system_prompt": "",
                        "changes_summary": (
                            f"Red Team 检查通过,无需精修。{red_summary}"
                        ),
                        "_red_summary": red_summary,
                    }, None)

                # Step 2: 蓝队接 attacks 给 fixes
                try:
                    blue_input = {
                        **_common_input,
                        "attacks": attacks,
                        "_red_summary": red_summary,
                    }
                    blue_result = await self.red_blue_blue.run(
                        blue_input, self.run_id, self.db,
                    )
                except RunBudgetExceededError:
                    raise  # 同上:预算熔断冒泡到顶层
                except Exception as e:
                    logger.warning("[red_blue] cell %s 蓝队失败: %r", cid, e)
                    # 红队找到了 attacks 但蓝队没修好 —— 这 cell 的问题**未被解决**,
                    # 绝不能当成 unchanged/通过。返回 err(非 None)让聚合循环计入
                    # stats['failed'],cell 保留原 demo 并标 _red_blue_status=failed,
                    # 避免 pages/4 误显示"全部通过 Red Team 检查"。
                    return None, (
                        f"blue:{type(e).__name__}: {e} "
                        f"(红队找到 {len(attacks)} 个问题但蓝队修复失败)"
                    )

                # 合并红蓝输出,保持和老 RedBlueRefiner 同样的字段结构
                merged = {
                    "cell_id": cid,
                    "attacks": attacks,
                    "fixes": blue_result.get("fixes") or [],
                    "refined_demo_output": blue_result.get("refined_demo_output", ""),
                    "refined_system_prompt": blue_result.get("refined_system_prompt", ""),
                    "changes_summary": blue_result.get("changes_summary", ""),
                    "_red_summary": red_summary,
                }
                return merged, None

        results = await asyncio.gather(
            *[refine_one(c) for c in prompt_cells]
        )

        stats = {"attempted": len(prompt_cells), "refined": 0,
                 "unchanged": 0, "failed": 0}
        details: list[dict] = []

        for i, (cell, (result, err)) in enumerate(zip(prompt_cells, results)):
            cid = cell.get("cell_id", "?")
            if err is not None or result is None:
                # Agent crashed. Leave a visible marker on the cell AND
                # in stats so the UI can surface "ran but failed" instead
                # of showing a blank.
                prompt_cells[i]["_red_blue_status"] = "failed"
                prompt_cells[i]["_red_blue_summary"] = f"(红蓝精炼失败) {err or '未知错误'}"
                stats["failed"] += 1
                details.append({
                    "cell_id": cid, "status": "failed",
                    "summary": err or "未知错误", "attacks": [], "fixes": [],
                })
                continue

            attacks = result.get("attacks") or []
            fixes = result.get("fixes") or []
            new_demo = (result.get("refined_demo_output") or "").strip()
            new_sp = (result.get("refined_system_prompt") or "").strip()
            changes_summary = (result.get("changes_summary") or "").strip()

            if new_demo:
                prompt_cells[i]["demo_output"] = new_demo
                stats["refined"] += 1
                prompt_cells[i]["_red_blue_status"] = "refined"
                prompt_cells[i]["_red_blue_summary"] = (
                    changes_summary or f"精修了 {len(fixes)} 处"
                )
            else:
                # No refined_demo returned — either Red Team found nothing
                # or model returned empty string. Previously this was
                # silent; now we record it so the UI can show "检查过,
                # 无需修改" instead of "没跑".
                stats["unchanged"] += 1
                prompt_cells[i]["_red_blue_status"] = "unchanged"
                prompt_cells[i]["_red_blue_summary"] = (
                    changes_summary
                    or (f"Red Team 找到 {len(attacks)} 个点但未产出精修文本"
                        if attacks else "Red Team 检查通过,无需修改")
                )

            if new_sp:
                prompt_cells[i]["system_prompt"] = new_sp

            # Snapshot per-cell detail for UI expansion.
            details.append({
                "cell_id": cid,
                "status": prompt_cells[i].get("_red_blue_status", "?"),
                "summary": prompt_cells[i].get("_red_blue_summary", ""),
                "attacks": attacks,
                "fixes": fixes,
                "refined_demo_output": new_demo,
                "refined_system_prompt_changed": bool(new_sp),
            })

        stats["details"] = details
        final_system["_red_blue_stats"] = stats

        logger.info(
            "[red_blue] attempted=%d refined=%d unchanged=%d failed=%d",
            stats["attempted"], stats["refined"],
            stats["unchanged"], stats["failed"],
        )

    async def _run_persona_simulation(
        self, final_system: dict, brief: dict
    ) -> None:
        """Persona simulation: 3 target-audience personas react to each
        cell's demo. Results stored on final_system for UI display and
        as supplementary input to vibe_critic.

        v0.29.2: 之前失败路径(prompt_matrix 空 / 调用抛异常)完全不写
        `_persona_reactions`,UI 只能显示"没产出"。现在所有路径都写一个
        `{"status": ..., "error"/"result": ...}` 结构,让 UI 区分
        "没跑"/"跑了失败"/"跑了成功"。
        """
        prompt_cells = final_system.get("prompt_matrix") or []
        if not prompt_cells:
            final_system["_persona_reactions"] = {
                "status": "skipped",
                "reason": "prompt_matrix 为空,未调用 persona_simulator",
            }
            return

        target_audience = brief.get("target_audience", "")
        if isinstance(target_audience, list):
            target_audience = ", ".join(str(t) for t in target_audience)

        # v0.30.6 fix M4: 每个 cell 自己带 platform,持出去给画像模拟,
        # 避免多平台 brief 里 douyin cell 用 xhs 画像评判。
        slim_cells = [
            {
                "cell_id": c.get("cell_id"),
                "direction_name": c.get("direction_name"),
                "platform": c.get("platform", ""),
                "demo_output": (c.get("demo_output") or "")[:500],
            }
            for c in prompt_cells
        ]

        # 顶层 platform 仍传一个(向后兼容旧 prompt),但优先级:cell.platform。
        _platforms = brief.get("target_platforms") or []
        _top_platform = _platforms[0] if _platforms else DEFAULT_PLATFORM

        # v0.30.8: 双模型并跑画像 — Claude 系(Sonnet 3.7)+ DeepSeek 系
        # (v4-pro)。两个 agent 用同一份 persona_simulator.md prompt,但不同
        # 模型 distribution → 产出的 6 个画像 cover 更广(Claude 偏目标用户
        # 细腻反应,DeepSeek 偏草根/破圈视角)。任一失败不阻塞另一个,只
        # 缺谁的标记缺失,不会 fail 整个流水线。
        _common_input = {
            "target_audience": target_audience or "未指定（请根据 brief 推断）",
            "platform": _top_platform,
            "all_platforms": list(_platforms) or [DEFAULT_PLATFORM],
            "cells": slim_cells,
        }

        async def _run_one(agent, label: str):
            try:
                _res = await agent.run(_common_input, self.run_id, self.db)
                logger.info("[persona_sim] %s 跑通", label)
                return _res, None
            except RunBudgetExceededError:
                raise  # 预算熔断冒泡到顶层硬停,不被吞成单 backend 失败
            except Exception as _e:
                logger.exception("[persona_sim] %s 失败", label)
                return None, f"{type(_e).__name__}: {_e}"

        # 并行启动两个 agent。如果 DeepSeek 没配 secret,alt 那条会立刻
        # RuntimeError,但 Claude 那条不受影响。
        _claude_pair, _ds_pair = await asyncio.gather(
            _run_one(self.persona_simulator, "Claude(Sonnet 3.7)"),
            _run_one(self.persona_simulator_alt, "DeepSeek(v4-pro)"),
        )
        _claude_result, _claude_err = _claude_pair
        _ds_result, _ds_err = _ds_pair

        # 至少一个成功才能继续
        if not _claude_result and not _ds_result:
            final_system["_persona_reactions"] = {
                "status": "failed",
                "error": (
                    f"Claude: {_claude_err or 'unknown'} | "
                    f"DeepSeek: {_ds_err or 'unknown'}"
                ),
                "reason": (
                    "双 backend 都失败。常见原因: 配置问题(secrets.toml)、"
                    "rate limit / 5xx、模型返回为空。stage_log 里有完整 traceback。"
                ),
            }
            return

        # 合并 personas[],按 _source 标注来源。同 id 不去重(Claude 的 P_core
        # 和 DeepSeek 的 P_core 是不同 distribution 下的扮演,UI 都该看到)。
        merged_personas: list[dict] = []
        for _src_label, _res in [("claude", _claude_result), ("deepseek", _ds_result)]:
            if not _res:
                continue
            for _p in (_res.get("personas") or []):
                _p_tagged = {**_p, "_source": _src_label}
                # 如果两边 id 相同(都是 P_core),给 DeepSeek 那个改成 P_core_ds
                # 避免下游 cell_id 计数时混淆
                if _src_label == "deepseek":
                    _orig_pid = _p_tagged.get("id", "")
                    _p_tagged["id"] = f"{_orig_pid}_ds" if _orig_pid else "P_unknown_ds"
                merged_personas.append(_p_tagged)

        # 用 Claude 的 summary 当主(它通常更结构化),DeepSeek 的留作 _ds_summary
        # 给 UI 展示。如果 Claude 失败就 fallback DeepSeek。
        _primary = _claude_result or _ds_result or {}
        _summary = _primary.get("summary", {}) or {}
        if _ds_result and _claude_result:
            _summary = {**_summary, "_ds_summary": (_ds_result.get("summary") or {})}

        result = {
            "status": "ok",
            "mode": _primary.get("mode", "persona_spectrum"),
            "personas": merged_personas,
            "summary": _summary,
            "_dual_backend": {
                "claude_ok": bool(_claude_result),
                "claude_error": _claude_err,
                "deepseek_ok": bool(_ds_result),
                "deepseek_error": _ds_err,
            },
        }
        tagged = result
        final_system["_persona_reactions"] = tagged

        summary = (result or {}).get("summary") or {}
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

        # v0.29.11(方案 A):把"3 个画像全部 skip"的 cell 追加进
        # strategic_warnings,和 consumer_simulation 对齐,走现有的
        # UI 告警通道 + 可选的 strategic_escalation 自动升级链。
        # 不信任 summary.weak_cells(模型有时给的是 direction_id 而非
        # cell_id),直接从 personas[*].reactions 计算:每个 cell_id 统计
        # 3 个画像的 action,全 skip = weak。
        try:
            _personas = (result or {}).get("personas") or []
            # 按 (cell_id, _source) 分桶,不把 Claude 与 DeepSeek 两个 backend 的
            # 票**池化**。池化有两个错:(a) DeepSeek 一票非-skip 就能吞掉 Claude
            # 3 画像的一致否决(fail-open,漏掉真弱 cell);(b) "全 skip" 阈值随是否
            # 配置 DEEPSEEK_API_KEY 在 3 票与 6 票间漂移。改为:**任一** backend 对
            # 某 cell 的画像全部 skip 即判 weak(并集),阈值恒为该 backend 的画像数。
            _actions_by_cell_src: dict[tuple, list[str]] = {}
            _reactions_per_cell: dict[str, list[str]] = {}
            for _p in _personas:
                _pid = _p.get("id", "?")
                _src = _p.get("_source", "?")
                for _r in (_p.get("reactions") or []):
                    _cid = _r.get("cell_id")
                    if not _cid:
                        continue
                    _actions_by_cell_src.setdefault((_cid, _src), []).append(
                        (_r.get("action") or "").lower()
                    )
                    _reactions_per_cell.setdefault(_cid, []).append(
                        f"{_pid}: {(_r.get('reaction') or '')[:60]}"
                    )

            # 每个 backend 独立投票:该 backend 至少给了 3 个画像判决且全 skip
            # → 这个 backend 一致否决。
            _vetoed_by: dict[str, set[str]] = {}
            _backends_seen: set[str] = set()
            for (_cid, _src), _acts in _actions_by_cell_src.items():
                _backends_seen.add(_src)
                if len(_acts) >= 3 and all(_a == "skip" for _a in _acts):
                    _vetoed_by.setdefault(_cid, set()).add(_src)

            # v0.33.2: 判据从【任一 backend 否决】改成【所有跑通的 backend 都否决】。
            #
            # 改的是**成本不对称**不是准确率。并集规则下多一个 backend 主要是提高
            # 误报率,而误报的代价极不对称:弱 cell → strategic_warnings →
            # 触发 strategic_escalation → 回中书省(kimi-k3)改方向 → 再跑一整轮
            # vibe_loop。一次几分钱的 DeepSeek 调用能触发全流水线最贵的重入。
            #
            # 交集要求两个不同厂家、不同 distribution 的画像都一致否决 —— 这才是
            # 当初做双 backend 的本意:跨厂家一致=强信号,单边否决=噪声。
            # 只有一个 backend 跑通时(另一个没配 key / 挂了)交集自动退化成
            # 它自己,不会因为少一票就永远判不出弱 cell。
            if PERSONA_WEAK_REQUIRES_BOTH_BACKENDS and len(_backends_seen) > 1:
                _weak_cell_ids = sorted(
                    cid for cid, srcs in _vetoed_by.items()
                    if srcs >= _backends_seen
                )
                _single_sided = sorted(
                    cid for cid, srcs in _vetoed_by.items()
                    if not (srcs >= _backends_seen)
                )
                if _single_sided:
                    logger.info(
                        "[persona_sim] %d 个 cell 只被单边 backend 否决,按交集"
                        "规则不判弱(避免廉价误报触发 strategic_escalation): %s",
                        len(_single_sided), _single_sided,
                    )
            else:
                _weak_cell_ids = sorted(_vetoed_by.keys())

            if _weak_cell_ids:
                _matrix = final_system.get("prompt_matrix") or []
                _cell_idx = {c.get("cell_id"): c for c in _matrix}

                for _cid in _weak_cell_ids:
                    _cell = _cell_idx.get(_cid, {})
                    _sample = " | ".join(_reactions_per_cell.get(_cid, [])[:3])
                    final_system.setdefault("strategic_warnings", []).append({
                        "cell_id": _cid,
                        "direction_id": _cell.get("direction_id", ""),
                        "platform": _cell.get("platform", ""),
                        "root_cause_explanation": (
                            f"persona_simulator: 3 个画像全部 skip — {_sample}"
                        ),
                        # 和 consumer_simulation 对齐用 interest_align fail
                        # 触发策略升级链(如果 ENABLE_STRATEGIC_ESCALATION)。
                        "multiplier_gate": {"interest_align": "fail"},
                        "iteration": "persona_sim",
                        "source": "persona_simulator",
                    })

                logger.warning(
                    "[persona_sim] %d weak cells flagged into "
                    "strategic_warnings: %s",
                    len(_weak_cell_ids),
                    _weak_cell_ids,
                )
        except Exception:
            # 反馈链失败不阻塞主流程,persona 结果仍然在 UI 上可见。
            logger.exception(
                "[persona_sim] weak-cell fan-out into strategic_warnings "
                "failed (non-fatal)"
            )

    async def _run_gemini_reference_analyzer(self, brief: dict) -> None:
        """B: fetch user-pasted post URLs via SocialDataX detail tools.

        v0.32.0: 换厂前这一步走 Gemini 的 url_context(模型自己去取 URL);
        Kimi 没有等价能力,改走 SocialDataX 的第一方 detail 接口,顺带把
        LLM 环节整个去掉(返回的已经是结构化数据,再转述只会引入幻觉)。
        stage 名保留 "gemini_reference_analyzer" 是为了 DB 兼容。

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

        _rp = {
            "posts": result.get("posts", []),
            "summary_stats": result.get("_summary_stats", {}),
            "verdict": verdict,
        }
        brief["_reference_posts"] = _rp  # in-memory,供本 run 下游读取
        # 持久化用 read-modify-merge,不整列覆写。resume/revise 时 brief 是
        # 不含 _revision_context / _reference_post_urls 的 crown_prince 快照,
        # 整列覆写会把库里这些持久字段抹掉 → 终审 round 计数丢失、force-pass
        # 永不触发(无限往返)+ 用户手贴对标 URL 永久丢失。镜像 699-703 写法。
        _fresh = (self.db.get_project(self.project_id) or {}).get("brief") or {}
        _fresh["_reference_posts"] = _rp
        self.db.update_project(self.project_id, brief=_fresh)

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
        """A1: pre-secretariat trend scout. Pulls real current 小红书 爆款
        via SocialDataX (first-party XHS access) to calibrate downstream
        copy writing against current platform voice.

        NOT advisory by default: under SOCIALDATAX_TREND_SCOUT_PRE_REQUIRED
        (=True), a scout failure with no reusable _trend_intel raises
        TrendScoutRequiredError and fails the run — fail-fast at the cost
        of only the 太子 stage beats silently producing an uncalibrated
        run. Deterministic non-fixable cases (unsupported platform,
        keyword-less brief with no hot-list fallback) and reuse-eligible
        resumes stay non-blocking.

        Search-keyword note: unlike the old Gemini/Google path — which
        avoided product terms because Google returned 软广 + 分析 — we now
        search the product *category* and rank by real 互动量, so topical
        keywords return relevant AND currently-viral samples. We pass
        product_category / product_name / target_audience as ordered
        search-keyword candidates and the scout tries them in order.
        (Stage-log names keep the historical ``gemini_trend_scout_*`` keys
        for UI compatibility.)
        """
        if not ENABLE_SOCIALDATAX_TREND_SCOUT_PRE:
            return

        # Ordered SEARCH-KEYWORD candidates. product_category is the best
        # topical anchor (relevant + rankable by real engagement);
        # product_name and target_audience are fallbacks. campaign_objective
        # is a goal, not a searchable topic, so it is excluded.
        vibe_hints: list[str] = []
        for k in ("product_category", "product_name", "target_audience"):
            v = brief.get(k)
            if v and isinstance(v, str) and v.strip():
                vibe_hints.append(v.strip())
            elif isinstance(v, list):
                for item in v:
                    if isinstance(item, str) and item.strip():
                        vibe_hints.append(item.strip())
        # Empty candidates is fine — the scout falls back to the platform's
        # real hot list (xhs) for generic "what's hot now" samples.

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
                target_count=SOCIALDATAX_TREND_SCOUT_TARGET_COUNT,
            )
        except Exception as e:
            # Fold unexpected exceptions into the normal skipped-result
            # path so the REQUIRED policy below sees them too.
            logger.warning(
                "[trend_scout pre] unexpected exception: %r", e
            )
            result = {
                "verdict": "skipped",
                "posts": [],
                "queries_used": [],
                "_skip_reason": f"unexpected_exception: {e}",
            }

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
        if verdict != "all_pass":
            reason = str(
                result.get("_skip_reason")
                or result.get("_not_found_reason")
                or verdict
            )
            tokens = int(usage.get("input_tokens", 0)) + int(
                usage.get("output_tokens", 0)
            )

            # Reuse guard — a resume/revision may already have usable
            # calibration posts from a previous successful attempt. NOTE:
            # on resume `brief` is done["crown_prince"] (the stage-log
            # output), which never carries _trend_intel — the persisted
            # copy lives on project.brief (written below on success), so
            # read it from there. A fresh fetch failing must not discard
            # existing data or kill the run.
            existing_intel = (brief.get("_trend_intel") or {})
            if not existing_intel.get("posts"):
                _project = self.db.get_project(self.project_id) or {}
                existing_intel = (
                    (_project.get("brief") or {}).get("_trend_intel") or {}
                )
            existing_posts = existing_intel.get("posts") or []
            reuse = bool(existing_posts)
            if reuse:
                # Re-attach so secretariat sees the previous posts even
                # though this brief instance (stage-log copy) lacked them.
                brief["_trend_intel"] = existing_intel
                reason = f"reused_existing_trend_intel ({reason})"

            # REQUIRED / fail-fast — missing calibration data skews the
            # whole downstream strategy; failing at A1 costs only the 太子
            # stage instead of a full uncalibrated run + rerun. Platforms
            # SocialDataX can't serve at all, and keyword-less briefs on
            # platforms without a hot-list fallback, stay advisory —
            # retrying can never fix those.
            deterministic = reason.startswith(
                ("unsupported_platform", "no_search_keywords")
            )
            required_fail = (
                SOCIALDATAX_TREND_SCOUT_PRE_REQUIRED
                and not reuse
                and not deterministic
            )

            error_message = None
            if required_fail:
                error_message = (
                    f"趋势取样(SocialDataX)未能拿到校准样本：{reason}。"
                    f"该阶段是策略校准的关键输入,缺失会导致全程产出跑偏,"
                    f"故按 SOCIALDATAX_TREND_SCOUT_PRE_REQUIRED=True 提前终止"
                    f"(此时只消耗了太子阶段)。修复方式:"
                    f"① 配置 .streamlit/secrets.toml 顶层 SOCIALDATAX_API_KEY"
                    f"(https://socialdatax.com/?from=npm);"
                    f"② 网络/配额问题请重试;"
                    f"③ 若想跳过取样直接跑,把 pipeline/config.py 的 "
                    f"SOCIALDATAX_TREND_SCOUT_PRE_REQUIRED 设为 False。"
                )

            self.db.update_stage_log(
                log_id,
                status="failed" if required_fail else "skipped",
                output_data={
                    "_skip_reason": reason,
                    "queries_used": result.get("queries_used", []),
                    "_partial_failures": result.get(
                        "_partial_failures", []
                    ),
                    "_raw_keys_sample": result.get("_raw_keys_sample", []),
                    "_gemini_usage": usage,
                },
                tokens_used=tokens,
                model_used=usage.get("model"),
                # error_message is the channel pages/3's
                # render_stage_error actually displays — without it a
                # failed stage renders as "error_message 为空,请查看
                # 服务端日志", unreachable on Streamlit Cloud.
                error_message=error_message,
            )
            if required_fail:
                raise TrendScoutRequiredError(error_message)
            logger.info("[trend_scout pre] %s: %s",
                        "reusing previous posts" if reuse else "skipped",
                        reason)
            return

        # Inject raw posts into brief so secretariat sees them. Store
        # both the structured list (for UI + downstream programmatic
        # access) and a formatted block (for direct prompt injection).
        _ti = {
            "posts": result.get("posts", []),
            "queries_used": result.get("queries_used", []),
            "grounding_urls": result.get("grounding_urls", []),
            "formatted_block": format_trend_intel_for_prompt(result),
        }
        brief["_trend_intel"] = _ti  # in-memory,供本 run 下游读取
        # 持久化用 read-modify-merge,不整列覆写(同 _reference_posts:resume 时
        # brief 是 crown_prince 快照,整列覆写会抹掉 _revision_context /
        # _reference_post_urls 等持久字段)。镜像 699-703 写法。
        _fresh = (self.db.get_project(self.project_id) or {}).get("brief") or {}
        _fresh["_trend_intel"] = _ti
        self.db.update_project(self.project_id, brief=_fresh)

        self.db.update_stage_log(
            log_id,
            status="completed",
            output_data={
                "verdict": verdict,
                "posts": result.get("posts", []),
                "queries_used": result.get("queries_used", []),
                "grounding_urls": result.get("grounding_urls", []),
                "_partial_failures": result.get("_partial_failures", []),
                # Observability for the tolerant field mapping: whether
                # engagement counts resolved (False → ranking degraded to
                # API order) and the first raw note's keys so the mapping
                # can be pinned from the UI without server-log access.
                "_engagement_resolved": result.get(
                    "_engagement_resolved", True
                ),
                "_raw_keys_sample": result.get("_raw_keys_sample", []),
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
        each direction is an independent SocialDataX call.

        Skipped wholesale if ENABLE_SOCIALDATAX_TREND_SCOUT_POST=False or
        SocialDataX is unavailable. One stage_log per direction so the UI
        can display status independently. (Stage-log names keep the
        historical ``gemini_trend_scout_post_*`` keys for UI compatibility.)
        """
        if not ENABLE_SOCIALDATAX_TREND_SCOUT_POST:
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
        per_direction_count = min(SOCIALDATAX_TREND_SCOUT_TARGET_COUNT, 5)

        semaphore = asyncio.Semaphore(TREND_SCOUT_POST_CONCURRENCY)

        async def _scout_one(direction: dict) -> tuple[str, dict]:
            d_id = str(direction.get("direction_id", "")).strip()
            d_name = str(direction.get("direction_name", "")).strip()
            if not d_id:
                return "", {}
            # SEARCH-KEYWORD candidates for this direction. direction_name
            # is the topical theme; the paradigm label adds a flavor term.
            # The scout searches them in order and ranks by real engagement.
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
    ) -> bool:
        """Gemini 结构审（advisory）— 在工部构建之后、网感复检之前跑一次。

        审查每条 prompt_cell 的 5 池 / 人设 / 合规 / 关键词 / AI 禁用清单 /
        平台调性是否结构完整。结果写成一条 stage_log（名为
        ministry_works_structure_review）给 UI 看，并把每 cell 的结构缺失
        hint 附到对应 cell 的 _structure_hint 字段，让下游 rewriter 和
        chancellery_final 都能读到。

        Advisory-only：Gemini 未配置 / 调用失败 / 解析失败全部 → 跳过，
        不阻塞流水线。Gemini 的 token + 成本通过 accumulate_auxiliary_cost
        累到 pipeline_runs.total_cost_usd 里，不占 MAX_TOKENS_PER_RUN 预算。

        返回值 = **模型那半边是否真跑成功**（不是"这个阶段有没有出错"）。
        确定性前置审是纯 Python，永远算跑过；模型半边失败时返回 False，
        调用方据此决定要不要写 resume 标记 —— 写了的话下次续跑会跳过整个
        结构审，把模型能抓到的那类问题（"合规写成空话"）永久漏掉。
        """
        prompt_cells = final_system.get("prompt_matrix", []) or []
        if not prompt_cells:
            # 没有矩阵可审 —— 重跑也审不出东西，算"已完成"，别让续跑空转。
            return True

        stage_name = "ministry_works_structure_review"
        log = self.db.create_stage_log(
            self.run_id,
            stage_name,
            {"cell_count": len(prompt_cells)},
        )
        log_id = log["id"]

        # ── 确定性前置审(零 LLM 成本)────────────────────────────────────
        # 结构审原本查 6 类结构件,其中 5 类(五池 / 人设 / 合规存在性 /
        # 关键词存在性 / 禁用清单存在性)`_validate_prompt_cell` 在构建阶段
        # 就已经用别名表确定性地查过了 —— 花一次 LLM 调用重查是纯重复。
        #
        # 平台调性张冠李戴这一类原本也交给模型,但它同样是确定性的
        # (平台调性词表是固定的),所以下沉到 Python。剩给模型的只有
        # 「具体 vs 空话」这一类真需要判断的(见 kimi_structure_reviewer.md)。
        #
        # 先跑确定性的,结果和模型的 hint 合并 —— 两边写的是同一个
        # `_structure_hint` 形状,下游 vibe_loop 的强制补漏逻辑不用动。
        _det_hints = deterministic_structure_audit(prompt_cells)
        if _det_hints:
            logger.info(
                "[structure review] 确定性前置审命中 %d 个 cell(零成本): %s",
                len(_det_hints), sorted(_det_hints.keys()),
            )

        try:
            result = await run_kimi_structure_review(prompt_cells)
        except Exception as e:
            # run_kimi_structure_review swallows its own errors; if
            # something still leaks treat as non-fatal.
            logger.warning(
                "[structure review] unexpected exception, skipping: %r", e
            )
            # 模型挂了不影响确定性那半边 —— 照样把 hint 挂上去,让重写器修。
            # 这正是下沉的价值之一:结构审的一部分不再依赖模型可用性。
            _apply_structure_hints(prompt_cells, _det_hints)
            self.db.update_stage_log(
                log_id,
                status="skipped",
                output_data={
                    "_skip_reason": f"unexpected_exception: {e}",
                    "deterministic_hints": _det_hints,
                },
            )
            return False

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
        _model_hints = {
            cid: {
                "missing_items": item.get("missing_items", []),
                "revision_hint": item.get("revision_hint", ""),
                "_source": "model",
            }
            for cid, item in incomplete_by_id.items()
        }
        # 合并确定性 hint 和模型 hint —— 同一个 cell 两边都命中时取并集,
        # 不是二选一:平台调性写串(确定性抓到)和合规写成空话(模型抓到)
        # 是两个独立问题,rewriter 一次都要修。
        _merged_hints = _merge_structure_hints(_det_hints, _model_hints)
        _apply_structure_hints(prompt_cells, _merged_hints)

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
            return False

        self.db.update_stage_log(
            log_id,
            status="completed",
            output_data={
                "verdict": verdict,
                "summary": result.get("summary", ""),
                "cells_incomplete": incomplete,
                "cell_reviews": result.get("cell_reviews", []),
                # 确定性那半边单独记一份,便于在 UI / SQL 里区分
                # "模型抓到的" 和 "Python 抓到的"。
                "deterministic_hints": _det_hints,
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
        return True

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
        # R-022: 不管命中与否都算 summary,送给 rewriter/critic 让它们知道
        # 哪些平台是 PRIMARY(DB hit)、哪些必须 FALLBACK(静态兜底)。
        # 同时 telemetry: 把 hit/miss + TV-synced 占比写日志,运营才能看出
        # "飞轮飞起来了吗,还是 0 packs 在悄悄地用静态兜底"。
        reference_packs_summary = summarize_packs_by_platform(
            reference_packs_by_platform, _unique_platforms
        )
        if reference_packs_by_platform:
            logger.info(
                "[vibe_loop] injecting reference_packs: %s "
                "(tv_synced=%d, manual=%d, platforms_missed=%s)",
                {k: len(v) for k, v in reference_packs_by_platform.items()},
                reference_packs_summary["tv_synced_total"],
                reference_packs_summary["manual_total"],
                reference_packs_summary["platforms_missed"],
            )
        else:
            # 全部 miss — vibe_rewriter 会全部走静态兜底。这是飞轮失效信号。
            logger.warning(
                "[vibe_loop] NO reference_packs for any platform %s "
                "(category=%r); vibe_rewriter will fall back to static "
                "samples only. Flywheel value is not flowing back — check "
                "reference_samples table or truth-vault sync.",
                _unique_platforms,
                _brief_category,
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
                critic_input["reference_packs_summary"] = reference_packs_summary

            # v0.30.5 fix H3: 把 persona_simulator 的 per-cell 反应注入 critic,
            # 让"3 个画像里有 2 个划走"这种信号真的影响判决,而不是只在
            # 全 skip 的 weak_cells 边缘场景才触发 strategic_warnings。
            # 压缩成 {cell_id: [{persona, action, reason}, ...]} 紧凑形式。
            #
            # 仅 iteration==0 注入:persona 反应是在 vibe_loop 之前对**原始 demo**
            # 跑的(见 run() 5d 顺序)。round2+ 只复检被改写过的 cell,那些 cell 的
            # demo 已变,旧反应变陈旧——继续按 cell_id 注入会拿"对旧文案的划走"去
            # 判新文案,污染质量闸、阻碍收敛。round2+ 一律不注入。
            _persona_pkg = final_system.get("_persona_reactions") or {}
            if iteration == 0 and _persona_pkg.get("status") == "ok":
                _per_cell_reactions: dict = {}
                for _p in (_persona_pkg.get("personas") or []):
                    _pid = _p.get("id", "?")
                    for _r in (_p.get("reactions") or []):
                        _cid_r = _r.get("cell_id")
                        if not _cid_r:
                            continue
                        _per_cell_reactions.setdefault(_cid_r, []).append({
                            "persona": _pid,
                            "action": _r.get("action", "?"),
                            "reason": (_r.get("reason") or "")[:80],
                        })
                if _per_cell_reactions:
                    critic_input["persona_reactions_by_cell"] = _per_cell_reactions

            try:
                critic_result = await self.vibe_critic.run(
                    critic_input, self.run_id, self.db
                )
            except RunBudgetExceededError:
                raise  # 预算熔断必须冒泡到顶层硬停,不能被 advisory 降级吞掉
            except Exception as _critic_err:
                # Full traceback so a vibe-critic failure isn't a mystery
                # ("why did this run skip the vibe loop?"). Still non-fatal:
                # we proceed with whatever the builder produced — BUT record a
                # strategic_warning so the run doesn't silently report success
                # as if the 网感 gate had passed. #14: a skipped quality gate
                # must be visible, not swallowed into a green run.
                logger.exception(
                    "Vibe critic failed, proceeding without critique"
                )
                final_system.setdefault("strategic_warnings", []).append({
                    "type": "quality_gate_skipped",
                    "stage": "vibe_critic",
                    "message": (
                        "⚠️ 网感闸门（vibe_critic）执行失败，本轮产出**未经网感"
                        "校验**——可能含 AI 腔 / 不够真实。请排查该阶段错误后重跑再交付。"
                        f"（{type(_critic_err).__name__}: {_critic_err}）"
                    ),
                })
                break

            # 跨轮次累积 cell_reviews。round 2+ 只复检被改写过的 cell(见上面
            # cells_to_critique 的分支),所以最后一轮的 critic_result 里只有
            # 那几个 cell —— round 1 就通过、之后再没被评过的 cell 完全不在
            # 里面。质量评分要的是**每个 cell 最后一次评审**的全集,这里按
            # cell_id 覆盖累积:后一轮的评审自然盖掉前一轮的。
            # 纯读已有输出,零额外 LLM 成本。
            #
            # ⚠️ 存在 self 上而不是 final_system 上,这是有意的:
            # chancellery_final 的输入是 `{"prompt_system": final_system}`
            # 整体透传(见 agents/chancellery.py::run_final_review),挂到
            # final_system 等于给全流水线最贵的那个 stage(kimi-k3 $3/$15)
            # 白加十几 KB 输入换零收益。评分在终审之后才跑,存 self 完全够用。
            # 策略升级会二次调用 _run_vibe_loop,共用同一个 self,累积正确。
            for _rv in (critic_result.get("cell_reviews") or []):
                _rv_cid = _rv.get("cell_id")
                if _rv_cid:
                    self._vibe_cell_reviews[_rv_cid] = _rv

            failed = critic_result.get("failed_cells", []) or []
            # Fail-CLOSED cross-check. vibe_critic.md 的契约:failed_cells 必须
            # 包含每个 severity != pass 的 cell,且任一 cell 失败时 verdict 必为
            # "some_failed"。但中转/模型截断可能只写了尾部空的 failed_cells(而
            # cell_reviews 里明明有 borderline/fail),此时只信 failed_cells 就是
            # fail-open —— AI 腔 cell 被当"全过"静默发货。这里交叉校验:凡
            # cell_reviews 中 severity∈{borderline,fail} 但没进 failed_cells 的,
            # 补回;并在 verdict=some_failed 却一个都补不出时留告警(闸门不可信)。
            _declared_ids = {f.get("cell_id") for f in failed if f.get("cell_id")}
            _cr = critic_result.get("cell_reviews") or []
            _recovered = []
            for _rv in _cr:
                _sev = (_rv.get("severity") or "").strip().lower()
                _cid = _rv.get("cell_id")
                if _cid and _sev in ("borderline", "fail") and _cid not in _declared_ids:
                    _recovered.append({
                        "cell_id": _cid,
                        "platform": _rv.get("platform", ""),
                        "severity": _sev,
                        "root_cause_kind": _rv.get("root_cause_kind"),
                        "root_cause_explanation": _rv.get("root_cause_explanation", ""),
                        "rewrite_directives": _rv.get("rewrite_directives", ""),
                        "_flagged_by": "gate_reconcile",
                    })
            if _recovered:
                logger.warning(
                    "[vibe_loop] fail-closed 补回:%d 个 cell 在 cell_reviews 里 "
                    "severity!=pass 但没进 failed_cells(critic 输出疑似截断)——"
                    "补回复检:%s",
                    len(_recovered),
                    sorted(r["cell_id"] for r in _recovered),
                )
                failed = failed + _recovered
            _verdict = (critic_result.get("verdict") or "").strip().lower()
            if _verdict == "some_failed" and not failed:
                # 判了 some_failed 却连一个可定位的失败 cell 都没有(两个数组都被
                # 截断)——不能当通过。留告警让交付前可见,不静默放行。
                final_system.setdefault("strategic_warnings", []).append({
                    "type": "quality_gate_untrusted",
                    "stage": "vibe_critic",
                    "message": (
                        "⚠️ 网感闸门判定 some_failed 但未返回任何可定位的失败 cell"
                        "(critic 输出疑似被截断)。本轮无法据此修复,产出**未获可信"
                        "网感校验**,交付前请人工复核或重跑。"
                    ),
                })
                logger.warning(
                    "[vibe_loop] verdict=some_failed 但 failed/cell_reviews 均为空"
                    " — 闸门不可信,已记 strategic_warning"
                )
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
                    gemini_result = await run_kimi_critic(claude_passed)
                except Exception as e:
                    # run_kimi_critic is supposed to swallow everything.
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

            # ── 机审闸(v0.33.5)───────────────────────────────────────
            # 纯 Python 扫本轮 cell 的机械指纹(翻案句 / 商业黑话 / 伪精确行为量 /
            # AI 空话 / 寒暄开场 / 列表体)。命中的强制加进 failed 列表走定点改写。
            #
            # 放在 critic **之后**是有意的:critic 是批处理(整批一次调用),
            # 先跑机审并不能少掉那次调用,反而要多一轮编排。放这里则是纯加法 ——
            # 借 critic 已经产出的 failed 列表做合并,机械命中的 cell 顺路进
            # rewriter,不额外发起任何调用。
            #
            # 真正的省是在**下一轮**:round 2+ 只复检被改写过的 cell,机审在那时
            # 免费复扫一遍,能抓到 rewriter「把 A 指纹改成 B 指纹」的情况 ——
            # 那是机扫过了、读者没过的最隐蔽劣化路径。
            _pg_forced: set[str] = set()
            for _c in cells_to_critique:
                _cid_pg = _c.get("cell_id")
                if not _cid_pg or _cid_pg in {
                    f.get("cell_id") for f in failed if f.get("cell_id")
                }:
                    continue
                _pg = prose_scan_text(_c.get("demo_output") or "")
                _c["_prose_soft_flags"] = _pg["soft"]
                if not _pg["hard"]:
                    continue
                failed.append({
                    "cell_id": _cid_pg,
                    "platform": _c.get("platform", ""),
                    "severity": "fail",
                    # 机械命中属于 surface 的机械子类 —— 明确走 vibe_rewriter
                    # 而不是 structural_rewriter:这不是叙事身份或缺口方向的
                    # 问题,是句子层面的定点修。
                    "root_cause_kind": "surface",
                    "root_cause_explanation": "prose_gate 机审硬命中",
                    "rewrite_directives": format_hard_hits_for_rewriter(
                        {"hard_hits": _pg["hard"]}
                    ),
                    "_flagged_by": "prose_gate",
                })
                _pg_forced.add(_cid_pg)
            if _pg_forced:
                logger.info(
                    "[prose_gate] 机审硬命中 %d 个 critic 判过的 cell,"
                    "强制进重写(零 LLM 成本): %s",
                    len(_pg_forced), sorted(_pg_forced),
                )

            # v0.30.6 fix M1: Gemini 结构审标了 _structure_hint(missing_items
            # 非空)但 critic 让该 cell 过了 → 之前 hint 永远到不了 rewriter,
            # 漂亮但结构不全的 cell 直接发货。这里把这些 cell 强制加进
            # failed 列表(severity=borderline,带合成的 rewrite_directives)。
            existing_failed_ids = {f.get("cell_id") for f in failed if f.get("cell_id")}
            structurally_incomplete_ids: set[str] = set()
            for _c in cells_to_critique:
                _hint = _c.get("_structure_hint") or {}
                _missing = _hint.get("missing_items") or []
                _hint_rev = _hint.get("revision_hint") or ""
                if not _missing and not _hint_rev:
                    continue
                _cid = _c.get("cell_id")
                if not _cid or _cid in existing_failed_ids:
                    continue
                # critic 让它过了,但结构审说缺。force fail 让 rewriter 补结构。
                _synthesized = (
                    f"【结构审强制补漏】critic 判 pass 但 Gemini 结构审检测到本 "
                    f"cell 缺失关键模块: {', '.join(str(m) for m in _missing) or '(未列)'}。"
                    f"建议: {_hint_rev or '补齐缺失模块,保持原 demo 网感和钩子不变'}。"
                )
                failed.append({
                    "cell_id": _cid,
                    "platform": _c.get("platform", ""),
                    "severity": "borderline",
                    "rewrite_directives": _synthesized,
                    "root_cause_kind": "surface",
                    "root_cause_explanation": "structure_review 强制补漏",
                    "_flagged_by": "structure_review",
                })
                structurally_incomplete_ids.add(_cid)
            if structurally_incomplete_ids:
                logger.info(
                    "[vibe_loop] structure_review 强制补漏 %d 个 critic-pass "
                    "但缺结构的 cell: %s",
                    len(structurally_incomplete_ids),
                    sorted(structurally_incomplete_ids),
                )

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

            # #14: 若本轮 vibe_rewriter 抛异常,critic 判不合格的 surface/template
            # cell 会原样保留(未修复)。用这个 flag 在应用完(可能部分成功的)结构
            # 重写结果后跳出循环,并记 strategic_warning,不让失败静默混成 completed。
            _vibe_rewriter_broke = False
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
                        structural_input["reference_packs_summary"] = (
                            reference_packs_summary
                        )
                    try:
                        structural_result = await self.structural_rewriter.run(
                            structural_input, self.run_id, self.db,
                        )
                        structural_new_by_id = {
                            c["cell_id"]: c
                            for c in structural_result.get("prompt_cells", [])
                        }
                    except RunBudgetExceededError:
                        raise  # 预算熔断必须冒泡到顶层硬停,不能被 advisory 降级吞掉
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
                        rewriter_input["reference_packs_summary"] = (
                            reference_packs_summary
                        )
                    try:
                        rewritten = await self.vibe_rewriter.run(
                            rewriter_input, self.run_id, self.db,
                        )
                        vibe_new_by_id = {
                            c["cell_id"]: c
                            for c in rewritten.get("prompt_cells", [])
                        }
                    except RunBudgetExceededError:
                        raise  # 预算熔断必须冒泡到顶层硬停,不能被 advisory 降级吞掉
                    except Exception as _rw_err:
                        logger.exception(
                            "Vibe rewriter failed, keeping original cells for this batch"
                        )
                        # #14: 镜像 legacy 路径 —— rewriter 失败 → 这批 cell
                        # (含回落到 surface 桶的 structural cell)未经修复直接保留。
                        # 记 strategic_warning 让它在 UI 红色 surface,并置 flag
                        # 在应用完已成功的 structural 结果后跳出循环(rewriter 坏了
                        # 再转下一轮 critic 也没意义)。
                        final_system.setdefault("strategic_warnings", []).append({
                            "type": "quality_gate_failed",
                            "stage": "vibe_rewriter",
                            "message": (
                                "⚠️ 网感修复（vibe_rewriter）执行失败,被 critic 判"
                                "不合格的 cell **未经修复直接保留**。交付前请人工复核"
                                "这些 cell 或排查错误后重跑。"
                                f"（{type(_rw_err).__name__}: {_rw_err}）"
                            ),
                        })
                        _vibe_rewriter_broke = True

                # Merge both rewriter outputs. If both emitted the same cell_id
                # (shouldn't happen — we bucket disjointly — but defend anyway),
                # structural wins because it's the more targeted surgery.
                new_cells_by_id = {**vibe_new_by_id, **structural_new_by_id}
                # Audit-scope: only vibe_rewriter cells should carry "源:*"
                # tags. structural_rewriter.md doesn't require them.
                vibe_only_cells_by_id = dict(vibe_new_by_id)
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
                    rewriter_input["reference_packs_summary"] = (
                        reference_packs_summary
                    )
                try:
                    rewritten = await self.vibe_rewriter.run(
                        rewriter_input, self.run_id, self.db,
                    )
                except Exception as _rw_err:
                    logger.exception("Vibe rewriter failed, keeping original cells")
                    # #14: rewriter failed → critic-flagged cells ship UNFIXED.
                    # Don't let that pass as a clean success — surface it.
                    final_system.setdefault("strategic_warnings", []).append({
                        "type": "quality_gate_failed",
                        "stage": "vibe_rewriter",
                        "message": (
                            "⚠️ 网感修复（vibe_rewriter）执行失败，被 critic 判"
                            "不合格的 cell **未经修复直接保留**。交付前请人工复核"
                            "这些 cell 或排查错误后重跑。"
                            f"（{type(_rw_err).__name__}: {_rw_err}）"
                        ),
                    })
                    break
                new_cells_by_id = {
                    c["cell_id"]: c for c in rewritten.get("prompt_cells", [])
                }
                # Legacy path: every rewritten cell came from vibe_rewriter.
                vibe_only_cells_by_id = dict(new_cells_by_id)

            rewritten_ids = set(new_cells_by_id.keys())
            logger.info(
                f"Rewriters produced {len(rewritten_ids)} updated cells: "
                f"{sorted(rewritten_ids)}"
            )

            # 数量对账:critic 判 fail 但 rewriter 没返回的 cell(LLM 漏返/截断)。
            # 若不管:round2+ 只复检 rewritten_ids(见循环头 2909-2915),这些被
            # 漏掉的 fail cell 会**掉出复检集**、保留原失败内容静默发货。把它们并回
            # rewritten_ids,保证下一轮继续盯着它们(再给一次重写机会);循环耗尽时
            # 由下面的 exhaustion 告警兜底,不静默放行。
            _missing_rewrites = failed_ids - rewritten_ids
            if _missing_rewrites:
                logger.warning(
                    "[vibe_loop] rewriter 漏返 %d 个已判 fail 的 cell:%s —— "
                    "保留在复检集里,下一轮再试",
                    len(_missing_rewrites),
                    sorted(_missing_rewrites),
                )
                rewritten_ids = rewritten_ids | _missing_rewrites

            # R-022 follow-up: 运行时审计 rewrite_summary 的"源:..."标记。
            # 这是飞轮 ROI 唯一的可观测信号 — LLM 没按 prompt 写标记时,
            # 运营会以为飞轮没在工作。logger.warning 不阻断流水线(LLM
            # 漏写 ≠ 重写失败),只把异常 cell_id 显式 surface 出来。
            # 只审 vibe_rewriter 输出 — structural_rewriter 的 prompt
            # 不要求"源:..."标记,把它们混进来会出现系统性 false positive。
            # findings 同时持久化到 stage_logs(stage_name='r022_flywheel_audit')
            # 一行,这样运营/TV 不必盯 stderr,直接 SQL 就能日报和告警。
            _audit_findings = _audit_rewrite_source_tags(
                vibe_only_cells_by_id, reference_packs_by_platform
            )
            if _audit_findings.get("total_vibe_cells", 0) > 0:
                _persist_audit_findings(
                    self.db,
                    self.run_id,
                    iteration=iteration,
                    findings=_audit_findings,
                    reference_packs_summary=reference_packs_summary,
                )

            # 字段 MERGE,不是整体替换。rewriter 的输出 schema 只覆盖
            # system_prompt/demo_output 等网感字段,不含 builder 产出的
            # media_brief / comment_seeds。整体替换(旧写法 .get(id, c))会让被重写
            # 的 cell 丢掉这两个字段 → 交付物残缺(下游 4240-4243 校验会报缺失)。
            # 把重写结果叠加到 builder cell 之上:网感修复落地,builder-only 字段存活。
            _merged_cells = []
            for _c in prompt_cells:
                _cid = _c.get("cell_id")
                if _cid in new_cells_by_id:
                    _merged_cells.append({**_c, **new_cells_by_id[_cid]})
                else:
                    _merged_cells.append(_c)
            prompt_cells = _merged_cells
            final_system["prompt_matrix"] = prompt_cells

            # #14: 结构重写结果已应用完(见上),此刻若 vibe_rewriter 本轮抛过异常,
            # 停止循环 —— rewriter 坏了继续转下一轮 critic 无意义,且 warning 已记。
            if _vibe_rewriter_broke:
                break

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

        # ⚠️ 走到这里 = 撞上轮次上限退出,而**上一步刚做完重写**。
        # 被重写过的 cell 正文已经换了,但 _vibe_cell_reviews 里存的还是改写
        # **之前**那版的判词 —— 拿旧的 multiplier_gate / template_test 去配新的
        # demo,评分器算出来的两个来源不同代:实验里会把改进读成退步、把退步读成
        # 改进。这正是 P1 建评分体系时要避免的那类失真,不修就等于评分器自己
        # 在制造噪声。
        #
        # 处理是**作废**而不是补评:删掉这些条目后 check_high_score 对应维度
        # 返回 None(=没测),这些 cell 不进高分篇计数,同时
        # high_score_coverage_cells 会掉下来 —— UI 上已经有「覆盖不满 →
        # 高分层数字不可比」的提示,读数的人能看见。
        # 补评要多花一次 critic 调用,而这一轮的结论本来就是"没收敛",
        # 花钱买一个已知不合格样本的精确分数不划算。
        _stale = rewritten_ids & set(self._vibe_cell_reviews.keys())
        if _stale:
            for _sid in _stale:
                self._vibe_cell_reviews.pop(_sid, None)
            logger.warning(
                "[vibe_loop] 达轮次上限后仍有 %d 个 cell 是刚改写未复检的,"
                "作废其陈旧评审以免评分口径串代: %s",
                len(_stale), sorted(_stale),
            )
        return final_system

    async def _run_strategic_escalation(
        self,
        final_system: dict,
        structured_brief: dict,
        plan: dict,
    ) -> tuple[dict, dict, bool]:
        """策略层自动升级(v0.29.1, C.2.1)。

        vibe_loop 跑完后,若留下 strategic_warnings(rewriter 改不了的
        策略层错配),就回 secretariat 修订受影响 direction 的策略锚点
        (stop_trigger / reward_type / role_embodiment / ...),然后再
        跑一次 vibe_loop 让 critic + rewriter 用新锚点重新判决。

        返回 (final_system, plan, completed) 三元组 — plan 可能被更新。

        `completed` = **这一轮升级是否整套跑完**(修订锚点 + 重跑 vibe_loop)。
        只有它为 True 时调用方才该打 refined_c 快照:secretariat 改完锚点、
        但重跑 vibe_loop 抛异常时,plan 已经换成新的了(所以"plan 变了没"
        判断不出来),可矩阵还是**按旧锚点改写过的**那份。这时候打快照 =
        续跑时直接跳过升级,新锚点下的改写永远不会发生,而且没有任何报错。
        不打快照的话,续跑从 refined_b 起,warnings 还在,升级会重来一遍。

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
                return final_system, plan, True

            affected_direction_ids = sorted({
                w.get("direction_id") for w in warnings
                if w.get("direction_id")
            })
            if not affected_direction_ids:
                # Warnings exist but have no direction_id (old critic output
                # or Gemini-flagged). Nothing actionable at strategy layer —
                # leave warnings for user.
                return final_system, plan, True

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
            except RunBudgetExceededError:
                raise  # 预算熔断必须冒泡到顶层硬停,不能被 advisory 降级吞掉
            except Exception:
                logger.exception(
                    "[strategic_escalation] secretariat revision failed; "
                    "keeping original plan + warnings"
                )
                return final_system, plan, False

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

            # v0.30.6 fix H5: 失效掉受影响 direction 的 advisory 数据。
            # secretariat 改了这些 direction 的 stop_trigger / reward_type 等
            # 锚点,vibe_loop 会重写对应 cell 的 system_prompt + demo,所以
            # _persona_reactions / _narrative_director_result / _red_blue_stats
            # 里关于这些 cell 的旧记录已经过期。chancellery 和 UI 会读这些字段
            # 当作 ground truth,留着会误导。这里只清"受影响 cell 的部分",
            # 保留其他 cell 的有效数据。
            _affected_cell_ids: set[str] = set()
            for _c in (final_system.get("prompt_matrix") or []):
                if _c.get("direction_id") in affected_direction_ids:
                    _cid = _c.get("cell_id")
                    if _cid:
                        _affected_cell_ids.add(_cid)

            # 1. _persona_reactions: 清掉受影响 cell 的反应
            _pr = final_system.get("_persona_reactions") or {}
            if _pr.get("status") == "ok" and _pr.get("personas"):
                for _persona in _pr.get("personas", []):
                    _persona["reactions"] = [
                        r for r in (_persona.get("reactions") or [])
                        if r.get("cell_id") not in _affected_cell_ids
                    ]
                _pr["_stale_cell_ids_purged"] = sorted(_affected_cell_ids)

            # 2. _narrative_director_result: 清掉 cells_to_revise 里的受影响项
            _nd = final_system.get("_narrative_director_result") or {}
            if _nd:
                _nd["cells_to_revise"] = [
                    c for c in (_nd.get("cells_to_revise") or [])
                    if c.get("cell_id") not in _affected_cell_ids
                ]
                _nd["_stale_cell_ids_purged"] = sorted(_affected_cell_ids)

            # 3. _red_blue_stats.details: 清掉受影响 cell 的诊断
            _rb = final_system.get("_red_blue_stats") or {}
            if _rb.get("details"):
                _rb["details"] = [
                    d for d in _rb["details"]
                    if d.get("cell_id") not in _affected_cell_ids
                ]
                # 总计也减一下(失效的部分不再计入"已修复"统计)
                _rb["_stale_purged_count"] = len(_affected_cell_ids)

            # 4. _consumer_simulation.judgments: 清掉受影响 cell 的判决
            _cs = final_system.get("_consumer_simulation") or {}
            if _cs.get("status") == "ok" and _cs.get("judgments"):
                _cs["judgments"] = [
                    j for j in _cs["judgments"]
                    if j.get("cell_id") not in _affected_cell_ids
                ]
                _cs["_stale_cell_ids_purged"] = sorted(_affected_cell_ids)

            if _affected_cell_ids:
                logger.info(
                    "[strategic_escalation] purged stale advisory data for "
                    "%d cells across %d directions: persona / narrative_director / "
                    "red_blue / consumer_simulation 里关于这些 cell 的旧记录全部清掉,"
                    "下游 chancellery_final 不会读到过期诊断。",
                    len(_affected_cell_ids),
                    len(affected_direction_ids),
                )

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
            except RunBudgetExceededError:
                raise  # 预算熔断必须冒泡到顶层硬停,不能被 advisory 降级吞掉
            except Exception:
                logger.exception(
                    "[strategic_escalation] re-run vibe_loop failed; "
                    "leaving current state in place"
                )
                return final_system, plan, False

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
        return final_system, plan, True

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
            final_system["_consumer_simulation"] = {
                "status": "skipped",
                "reason": "prompt_matrix 为空,未调用 consumer_simulation",
            }
            return

        direction_index = getattr(self, "_direction_index", {}) or {}

        # Build per-cell consumer context. Cells whose direction has no
        # stop_trigger fall back to target_audience from brief.
        # v0.30.6 fix M4: 每个 cell 带自己的 platform,避免多平台 brief
        # 里 douyin cell 被用 xhs 标准评判。
        fallback_audience = structured_brief.get("target_audience", "") or ""

        # v0.33.2: 只对 critic 判 interest_align=weak 的 cell 做第二层校验。
        #
        # 到这一步同一批 demo 已经被画像类 agent 判过两遍(主 + alt 双 backend),
        # 而本函数调的**就是同一个 persona_simulator**,只是换了 mode 和判据 ——
        # 同一个 agent 对同一批内容判第三遍。
        #
        # 而它的定位是"interest_align 的第二层校验",可 vibe_critic 的四乘数
        # 硬门槛已经逐 cell 判过 interest_align 了:判 pass 的再判一遍多半同答案,
        # 判 fail 的早就进了 rewriter 或 strategic_warnings。真正值得花第二票的
        # 只有 **weak** 那一档 —— critic 自己都拿不准的。
        #
        # 数据来自 vibe_loop 累积的 _vibe_cell_reviews(零额外成本)。拿不到时
        # 退回全量 —— critic 挂了的情况下恰恰最需要这层校验。
        _target_cells = prompt_cells
        _reviews = getattr(self, "_vibe_cell_reviews", None) or {}
        # ⚠️ 只有 critic **评全了**才敢按 weak 过滤。截断/畸形的 critic 响应
        # 可能只返回一个 cell 的评审,如果那个恰好是 pass,_weak_ids 就是空集,
        # 于是整个矩阵被跳过、还对外宣称"没有 weak 档" —— 这是 fail-open。
        # 覆盖不全时退回全量:那种情况下恰恰最需要第二层校验。
        _all_ids = {c.get("cell_id") for c in prompt_cells if c.get("cell_id")}
        _covered = _all_ids & set(_reviews.keys())
        _full_coverage = bool(_all_ids) and _covered == _all_ids
        if not _full_coverage and _reviews:
            logger.warning(
                "[consumer_sim] critic 只评了 %d/%d 个 cell,覆盖不全 —— "
                "退回全量复判(不按 weak 过滤)",
                len(_covered), len(_all_ids),
            )
        if CONSUMER_SIM_ONLY_WEAK_ALIGN and _full_coverage:
            _weak_ids = {
                cid for cid, rv in _reviews.items()
                if ((rv.get("multiplier_gate") or {}).get("interest_align") or "")
                .strip().lower() == "weak"
            }
            if not _weak_ids:
                logger.info(
                    "[consumer_sim] critic 对全部 %d 个 cell 的 interest_align "
                    "都不是 weak,跳过第二层校验(省一次调用)",
                    len(prompt_cells),
                )
                final_system["_consumer_simulation"] = {
                    "status": "skipped",
                    "reason": (
                        "vibe_critic 的 interest_align 判决里没有 weak 档 —— "
                        "pass 的不需要复判,fail 的已走重写/告警通道。"
                    ),
                    "_scope": "weak_only",
                }
                return
            _target_cells = [
                c for c in prompt_cells if c.get("cell_id") in _weak_ids
            ]
            logger.info(
                "[consumer_sim] 只复判 interest_align=weak 的 %d/%d 个 cell: %s",
                len(_target_cells), len(prompt_cells), sorted(_weak_ids),
            )

        slim_cells = []
        for c in _target_cells:
            did = c.get("direction_id")
            d = direction_index.get(did) or {}
            stop_trigger = (d.get("stop_trigger") or "").strip()
            slim_cells.append({
                "cell_id": c.get("cell_id"),
                "direction_id": did,
                "direction_name": c.get("direction_name"),
                "platform": c.get("platform", ""),
                "stop_trigger": stop_trigger or fallback_audience,
                "demo_output": (c.get("demo_output") or "")[:600],
            })

        _platforms_list = structured_brief.get("target_platforms") or []
        _top_platform = _platforms_list[0] if _platforms_list else DEFAULT_PLATFORM

        try:
            result = await self.persona_simulator.run(
                {
                    "mode": "consumer_simulation",
                    "target_audience": fallback_audience,
                    "platform": _top_platform,
                    "all_platforms": list(_platforms_list) or [DEFAULT_PLATFORM],
                    "cells": slim_cells,
                },
                self.run_id,
                self.db,
            )
        except RunBudgetExceededError:
            raise  # 预算熔断必须冒泡到顶层硬停,不能被 advisory 降级吞掉
        except Exception as e:
            logger.exception(
                "[consumer_sim] persona_simulator failed; skipping 2nd-level check"
            )
            final_system["_consumer_simulation"] = {
                "status": "failed",
                "error": f"{type(e).__name__}: {e}",
                "reason": (
                    "consumer_simulation 调用失败。常见原因: JSON 解析失败 / "
                    "rate limit / 模型返回空。stage_log 里有完整 traceback。"
                ),
            }
            return

        # `_scope` 记录这一轮到底复判了哪些 cell —— 不写的话 UI 上"全部通过"
        # 会被读成"整个矩阵都过了",而实际只判了 weak 那一档。
        final_system["_consumer_simulation"] = {
            "status": "ok",
            "_scope": (
                "weak_only" if len(_target_cells) < len(prompt_cells) else "all"
            ),
            "_cells_checked": [c.get("cell_id") for c in _target_cells],
            **(result or {}),
        }

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
                "[consumer_sim] 复判的 %d 个 cell 全部通过第二层校验"
                "(矩阵共 %d 个)",
                len(_target_cells), len(prompt_cells),
            )


class TrendScoutRequiredError(RuntimeError):
    """Raised by _run_gemini_trend_scout_pre when
    SOCIALDATAX_TREND_SCOUT_PRE_REQUIRED=True and the scout can't deliver
    calibration posts (and the brief has none to reuse). Propagates to
    run()'s top-level handler, which marks the run failed — a deliberate
    fail-fast: at that point only the 太子 stage has been paid for, whereas
    silently proceeding without calibration data skews the whole strategy
    and forces a full rerun.
    """


class PipelineCancelled(RuntimeError):
    """用户在流水线中途点了「强制取消」。

    v0.33.8 新增。此前 `force_cancel_pipeline` 只翻数据库状态来解开 UI 的锁 ——
    它的 docstring 自己写明了 Python 杀不掉挂起的守护线程。后果是:点了取消之后
    **按钮解锁了、钱还在烧**,僵尸线程会一直跑到自己结束或 TCP 超时。

    现在流水线在每个 stage 边界查一次 run 状态,不再是 running 就抛这个异常,
    让线程自己走正常的退出路径。代价是每条 run 多约 20 次极轻量的 DB 读。

    注意这只能在**两次调用之间**生效 —— 已经发出去的那一次 HTTP 请求仍然要等它
    自己返回。真正卡在网络层的调用还是得靠超时兜底,那不是这一层能解决的。
    """


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

    Uses an ATOMIC compare-and-swap (try_claim_project_running) rather than
    a read-then-check: the old read-only guard had a TOCTOU window because
    the status only flips to 'running' much later inside the run thread, so
    two racing starts both read a non-running status and both proceeded —
    two orchestrators clobbering the same run (duplicate output / doubled
    tokens / whoever-writes-last-wins). The claim flips the project to
    'running' as its side effect, so this MUST be immediately followed by
    launching the run (both callers do).
    """
    allowed = [required_status] if required_status else None
    claimed = db.try_claim_project_running(project_id, allowed_from=allowed)
    if claimed is not None:
        return claimed
    # Claim failed — read the current status to produce a precise message.
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
    raise PipelineAlreadyRunningError(
        f"项目 {project_id[:8]} 无法占用为 running（可能有并发启动抢先一步）。"
        f"请刷新页面后重试。"
    )


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


def start_pipeline_in_background(
    project_id: str, run_id: str, db: SupabaseClient, *, claim: bool = True
):
    """Launch pipeline in a background thread (for Streamlit compatibility).

    claim=True (default) atomically claims the project as 'running' first.
    Callers that ALREADY claimed it (resume: revise_and_resume claims it up
    front so its destructive stage_log deletion is race-protected) pass
    claim=False to avoid a double-claim that would raise AlreadyRunning.
    """
    if claim:
        _assert_project_not_running(project_id, db)

    def _thread_target():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        # Liveness heartbeat: an independent daemon renews
        # pipeline_run.heartbeat_at every N seconds while this run executes.
        # If the PROCESS is killed (Cloud recycle/sleep), this thread dies
        # too and the heartbeat stops → the startup reaper collects the
        # zombie. The beat is independent of stage progress, so a healthy
        # long stage (multi-minute thinking call) never looks stalled.
        _hb_stop = threading.Event()

        def _heartbeat():
            while True:
                try:
                    db.update_pipeline_run(
                        run_id,
                        heartbeat_at=datetime.now(timezone.utc).isoformat(),
                    )
                except Exception:
                    pass  # a transient DB blip must not kill the heartbeat
                if _hb_stop.wait(PIPELINE_HEARTBEAT_INTERVAL_SECONDS):
                    return

        _hb_thread = threading.Thread(target=_heartbeat, daemon=True)
        _hb_thread.start()
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
                # to render instead of just an empty "running" spinner. Write
                # status=failed + error_message so the run-level error banner
                # (pages/3) actually renders it — an input-only marker showed
                # nothing.
                _tlog = db.create_stage_log(
                    run_id,
                    stage_name="_thread_target",
                    input_data={"phase": "thread_init_or_teardown"},
                )
                db.update_stage_log(
                    _tlog["id"],
                    status="failed",
                    error_message=mask_secrets(
                        "后台线程在 orchestrator.run() 之外失败（通常是 "
                        "PipelineOrchestrator 初始化 / 事件循环建立失败 / 线程"
                        "被提前杀）。详情：\n" + err_str
                    ),
                )
                logger.info(
                    f"[thread_target] marked run={run_id} failed with: {err_str}"
                )
            except Exception:
                logger.exception(
                    "[thread_target] also failed to mark run as failed — "
                    "run will stay in 'running' state; startup reaper will "
                    "collect it once the heartbeat goes stale"
                )
        finally:
            _hb_stop.set()
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


def _audit_rewrite_source_tags(
    vibe_rewritten_cells_by_id: dict,
    reference_packs_by_platform: dict[str, list],
) -> dict:
    """R-022 follow-up: 扫 vibe_rewriter 输出的 `rewrite_summary` 字段,
    确认它带了 `源:数据库样本 #<id>` 或 `源:静态兜底 #<编号>` 标记。

    审计范围只限 vibe_rewriter 输出。structural_rewriter.md 的 output
    format 不要求 "源:*" 标记,把 structural cells 混进来会出现系统性
    false positive(P1 review issue, 2026-05-22)。

    为什么不抛错: LLM 偶尔不严格遵守 prompt 是已知问题,这里抛错会让单
    cell 漏写一个标记就拆掉整条 vibe_loop。我们只 WARNING 把异常 cell 拎
    出来,让运营能在日志里 grep 出"飞轮信号缺失"的实际比例。**调用方**
    会把返回的 findings 写到 stage_logs 一行(stage_name='r022_flywheel_audit'),
    让"谁去看"的问题靠 SQL/dashboard 解决,而不是靠盯 stderr。

    返回 findings dict 形状:
        {
          "total_vibe_cells": int,
          "db_sourced": int,
          "static_sourced": int,
          "missing_tag_cell_ids": list[str],
          "excess_static_by_platform": dict[platform → quota detail],
          "per_platform_total": dict[platform → int],
        }

    分类:
      - missing_tag        — rewrite_summary 完全没有"源:" 这两个字
      - excess_static_use  — 平台 P 在 DB 里有 K 个包、本批进 vibe_rewriter
                             的 P 平台 cell 有 N 个,vibe_rewriter prompt
                             允许 max(0, N - K) 个 cell 走静态兜底(包用
                             尽场景)。实际静态使用 > 该上限时,超出部分
                             视为 LLM 忽略 PRIMARY 的真实漏洞。
    """
    if not vibe_rewritten_cells_by_id:
        return {
            "total_vibe_cells": 0,
            "db_sourced": 0,
            "static_sourced": 0,
            "missing_tag_cell_ids": [],
            "excess_static_by_platform": {},
            "per_platform_total": {},
        }

    # ── 阶段 1:逐 cell 检查 missing_tag + 累计 per-platform tally ──
    missing_tag: list[str] = []
    per_plat_total: dict[str, int] = {}
    per_plat_db: dict[str, int] = {}
    per_plat_static: dict[str, int] = {}

    # LLM may emit U+003A ASCII colon ":" or U+FF1A full-width Chinese
    # colon ":" after 源. Both spellings are explicitly allowed by the
    # vibe_rewriter prompt; use explicit ： escape so editors/diff
    # tools that fold unicode don't silently break this audit.
    _ASCII = "源:"  # 源:
    _FW    = "源："  # 源:
    def _has(text: str, suffix: str) -> bool:
        return (_ASCII + suffix) in text or (_FW + suffix) in text

    for cid, cell in vibe_rewritten_cells_by_id.items():
        summary = str(cell.get("rewrite_summary", "") or "")
        if not _has(summary, ""):
            missing_tag.append(str(cid))
            continue
        plat = (cell.get("platform") or "").strip()
        per_plat_total[plat] = per_plat_total.get(plat, 0) + 1
        if _has(summary, "数据库样本"):  # 数据库样本
            per_plat_db[plat] = per_plat_db.get(plat, 0) + 1
        if _has(summary, "静态兜底"):  # 静态兜底
            per_plat_static[plat] = per_plat_static.get(plat, 0) + 1

    # ── 阶段 2:per-platform 配额检查 (excess_static_use) ──
    # 规则:平台 P 有 K 个 DB 包、本批有 N 个 vibe cell。vibe_rewriter.md
    # "三态决策表" 允许的静态使用 = max(0, N - K)(包用尽时合法)。
    # 实际静态使用 > 该上限,超出部分是真漏洞。
    excess_static: dict[str, dict] = {}
    for plat, total in per_plat_total.items():
        if not plat:
            continue
        available_packs = len(reference_packs_by_platform.get(plat) or [])
        allowed_static = max(0, total - available_packs)
        actual_static = per_plat_static.get(plat, 0)
        if actual_static > allowed_static:
            excess_static[plat] = {
                "vibe_cells_in_batch": total,
                "available_packs": available_packs,
                "allowed_static_at_most": allowed_static,
                "actual_static": actual_static,
                "excess": actual_static - allowed_static,
            }

    if missing_tag:
        logger.warning(
            "[R-022 audit] %d/%d vibe cells missing 源:* tag in "
            "rewrite_summary: %s. Flywheel ROI signal invisible for "
            "these — check vibe_rewriter prompt compliance.",
            len(missing_tag),
            len(vibe_rewritten_cells_by_id),
            missing_tag,
        )
    if excess_static:
        logger.warning(
            "[R-022 audit] some platforms used 源:静态兜底 more than DB "
            "pack exhaustion would justify: %s. Allocation rule: a platform "
            "with K available packs allows at most max(0, N-K) static "
            "fallbacks where N = # vibe cells on that platform. Excess = "
            "LLM ignored PRIMARY — investigate.",
            excess_static,
        )
    total_db = sum(per_plat_db.values())
    total_static = sum(per_plat_static.values())
    logger.info(
        "[R-022 audit] vibe source tags: db=%d, static=%d, missing=%d "
        "(of %d vibe cells)",
        total_db, total_static, len(missing_tag),
        len(vibe_rewritten_cells_by_id),
    )

    return {
        "total_vibe_cells": len(vibe_rewritten_cells_by_id),
        "db_sourced": total_db,
        "static_sourced": total_static,
        "missing_tag_cell_ids": missing_tag,
        "excess_static_by_platform": excess_static,
        "per_platform_total": per_plat_total,
    }


def _persist_audit_findings(
    db,
    run_id: str,
    *,
    iteration: int,
    findings: dict,
    reference_packs_summary: dict | None,
) -> None:
    """Persist `_audit_rewrite_source_tags` findings as one stage_log row,
    stage_name='r022_flywheel_audit'. Status reflects severity:
      - 'completed'        — no missing_tag AND no excess_static_use
      - 'completed_warn'   — at least one of the two non-empty
                             (custom status string, picked up by future
                             dashboard / dailyTV self-check SQL)

    Why a row per iteration: vibe_loop can run 2-3 rounds; persisting per
    iteration gives a per-round audit trail for debugging "did round 1
    have a bug that round 2 fixed?".

    DB write is best-effort: a failed insert MUST NOT break vibe_loop —
    audit signal is observability, not a hard invariant.
    """
    has_missing = bool(findings.get("missing_tag_cell_ids"))
    has_excess = bool(findings.get("excess_static_by_platform"))
    output_data = {
        "iteration": iteration + 1,    # 1-indexed for human readability
        "total_vibe_cells": findings.get("total_vibe_cells", 0),
        "db_sourced": findings.get("db_sourced", 0),
        "static_sourced": findings.get("static_sourced", 0),
        "missing_tag_cell_ids": findings.get("missing_tag_cell_ids", []),
        "excess_static_by_platform": findings.get(
            "excess_static_by_platform", {}
        ),
        "per_platform_total": findings.get("per_platform_total", {}),
        "reference_packs_summary": reference_packs_summary or {},
        "has_warnings": has_missing or has_excess,
    }
    try:
        log = db.create_stage_log(
            run_id,
            "r022_flywheel_audit",
            input_data={"iteration": iteration + 1},
        )
        db.update_stage_log(
            log["id"],
            status="completed_warn" if (has_missing or has_excess) else "completed",
            output_data=output_data,
        )
    except Exception:
        # Observability layer SHOULD NOT crash the pipeline. Surface the
        # write failure in logs so the operator knows the audit row is
        # missing, but don't propagate.
        logger.exception(
            "[r022_flywheel_audit] failed to persist findings to stage_logs "
            "(iteration=%d, run_id=%s); findings still in stderr",
            iteration + 1,
            run_id,
        )


def _allocate_paths_by_direction(active_cells: list[dict]) -> dict[str, list[str]]:
    """按 direction_id 确定性分配「8 条突破路径」,返回 {direction_id: [路径,...]}。

    为什么需要:cell_planner 分批并行执行(CELL_PLANNER_BATCH_SIZE=5 /
    CONCURRENCY=5),每个批次独立给自己那几个 cell 挑路径,**批次之间互不知情**。
    12 个格子分 3 批同时跑时路径撞车是必然的,而且没有任何地方能看出来 ——
    偏偏路径组合就是"这几个方向的打法不重样"的核心机制。
    (config.py v0.30.6 注记里的 M2 遗留项。)

    为什么按 direction 而不是按 cell:矩阵是 方向 × 平台。要不重样的是**方向
    之间**;同一方向的不同平台(D1_xhs / D1_douyin)是同一打法的平台适配,
    本来就该共用同一组路径 —— 按 cell 分配反而会把一个方向拆成两种打法。

    分配用固定偏移量的轮转(见 config.PATH_ROTATION_OFFSETS),不用随机:
    随机在 resume / 重试时会给同一个方向分到不同路径,导致重跑结果漂移。
    """
    seen: list[str] = []
    for c in active_cells or []:
        did = c.get("direction_id")
        if did and did not in seen:
            seen.append(did)

    # 按方向编号自然排序,让分配不依赖 active_cells 的到达顺序 —— 否则上游
    # 任何一次排序变化都会让同一条 run 重跑时拿到不同的路径,resume 出来的
    # 格子和第一次跑的对不上。纯字典序会把 D10 排到 D2 前面,所以抽数字后缀。
    def _dir_sort_key(d: str) -> tuple[int, str]:
        m = re.search(r"(\d+)", d)
        return (int(m.group(1)) if m else 1 << 30, d)

    seen.sort(key=_dir_sort_key)

    allocation: dict[str, list[str]] = {}
    n = len(PATH_LIBRARY)
    for i, did in enumerate(seen):
        allocation[did] = [
            PATH_LIBRARY[(i + off) % n] for off in PATH_ROTATION_OFFSETS
        ]
    return allocation


def _merge_structure_hints(
    det_hints: dict[str, dict], model_hints: dict[str, dict]
) -> dict[str, dict]:
    """合并确定性结构审和模型结构审的 hint,同 cell 取并集。

    两边查的是不相交的东西 —— 确定性那边查平台调性张冠李戴,模型那边查
    「合规/关键词/禁用清单是具体规则还是空话」。同一个 cell 两边都命中时
    是两个独立问题,rewriter 一次都要修,所以 missing_items 拼起来、
    revision_hint 也拼起来,不是二选一。
    """
    merged: dict[str, dict] = {k: dict(v) for k, v in det_hints.items()}
    for cid, mh in model_hints.items():
        if cid not in merged:
            merged[cid] = dict(mh)
            continue
        base = merged[cid]
        base["missing_items"] = list(base.get("missing_items", [])) + [
            m for m in mh.get("missing_items", [])
            if m not in base.get("missing_items", [])
        ]
        _hints = [h for h in (base.get("revision_hint"), mh.get("revision_hint")) if h]
        base["revision_hint"] = "；".join(_hints)
        base["_source"] = "deterministic+model"
    return merged


def _apply_structure_hints(
    prompt_cells: list[dict], hints: dict[str, dict]
) -> None:
    """把 hint 挂到对应 cell 的 `_structure_hint` 字段(原地修改)。

    下游两个消费点:
      - vibe_loop 的强制补漏(critic 判 pass 但结构缺 → force borderline)
      - `_augment_rewrite_directives` 把它织进 rewrite_directives
    """
    by_id = {c.get("cell_id"): c for c in prompt_cells if c.get("cell_id")}
    for cid, hint in hints.items():
        cell = by_id.get(cid)
        if cell is not None:
            cell["_structure_hint"] = hint


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


# 一段文本"看起来是完整结尾"的判定,给 _validate_prompt_cell 用。
#
# ⚠️ 这里踩过一个把整条流水线拖垮的坑,别改回去:
# 原版直接 `text.endswith(clean_endings)`,而 clean_endings 只有句末标点
# (。！？」…)。小红书笔记【本来就以话题标签结尾】——
#     "...我下个月再来汇报。  #西屋按摩椅GT33 #按摩椅 #中秋送礼 #上岸"
# 这是完全正确的产出,却因为末尾是「内」而不是「。」被判成截断。
#
# 后果不是漏判而是【全中】:一个 run 里每个格子都触发 hard fail → 批次重试 →
# 单 cell 重试,三轮之后拿到的还是同样的合法内容,代码只好 "accepting anyway
# (best effort)" 收下。等于每个格子固定烧 3 倍 token 换回同一个结果,
# 直接把 run 顶到 MAX_TOKENS_PER_RUN 被熔断。
#
# 修法:先把结尾那串话题标签剥掉再判。剥完还剩正文就按正文的结尾判 ——
# 真正的截断(停在半句话上)仍然抓得到。
_TRAILING_HASHTAGS_RE = re.compile(r"(?:[#＃][^\s#＃]+[ 	　]*)+$")


def _ending_looks_complete(text: str, clean_endings: tuple[str, ...]) -> bool:
    """结尾是否完整。剥掉尾部话题标签后再按句末标点判。"""
    t = (text or "").rstrip()
    if not t:
        return True  # 空内容由必填字段检查负责,不在这里重复报
    stripped = _TRAILING_HASHTAGS_RE.sub("", t).rstrip()
    if not stripped:
        # 整段都是标签(极罕见)。长度检查会管它,这里不判截断。
        return True
    return stripped.endswith(clean_endings)


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
        if not _ending_looks_complete(sp, clean_endings):
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
        # ⚠️ 必须用别名表,不能单字面量匹配 —— 这是本函数里唯一一处曾经漏掉
        # 别名的检查,而它是【硬失败】。现场日志实测:模型把禁用清单写成
        # 「不要写…」「避免…」「❌…」而不是「禁止…」时,这条就误判,触发
        # 批次重试 + 单 cell 重试;如果模型的措辞习惯稳定,三轮都过不了 →
        # 整条 run 报 "三轮尝试后仍缺失" 挂掉。
        # 紧邻的 pool_aliases / batch_rule_aliases 早就是别名表了,后者的注释
        # 原话就是"别让 builder 因为措辞差异陷入三轮重试地狱"——这里补齐。
        #
        # 判定目标是"这段 prompt 里到底有没有这个板块",不是"有没有这个词",
        # 所以收同义写法;真的整段缺失时仍然抓得到。
        essential_keyword_aliases = {
            "合规/compliance 规则": [
                "合规", "compliance", "违禁词", "敏感词", "广告法",
                "风险提示", "不可宣称", "禁用词",
            ],
            "关键词植入指令": [
                "关键词", "keyword", "核心词", "搜索词", "蓝词", "埋词",
                "词包",
            ],
            # ⚠️ 这一组只收【明确是禁用清单】的写法。别加 "不得" / "避免" /
            # "不要" 这类裸词 —— 合规段里就有"不得宣称疗效"、五池规则里有
            # "不得与上一篇重复",裸词会命中它们,于是 system_prompt 里压根
            # 没有反 AI 腔清单也能判过(实测踩过)。宁可偶尔误报要求重试,
            # 也不能让这条检查变成永远为真的摆设。
            "反 AI 腔禁用清单": [
                "禁止", "禁用", "严禁", "忌用", "❌",
                "不要写", "不要出现", "不要用", "避免使用", "少用",
                "黑名单", "black list", "blacklist",
            ],
        }
        for description, aliases in essential_keyword_aliases.items():
            if not any(a in sp for a in aliases):
                issues.append(
                    f"{cid}: system_prompt 缺少「{description}」"
                    f"（这些写法一个都没找到: {'/'.join(aliases[:4])}…）"
                )

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

        # v0.33.4: 开头切入池必须是 15 条带编号的(跨批次多样性机制)。
        #
        # 其余四池"至少 5 个"就够 —— 它们管的是批次内差异化。只有开头角度还要
        # 管**跨批次**:运营是每天跑、连着跑几十批,5 种粒度的组合跑 5-10 批就
        # 用光,第 11 批必然撞。编号形式是为了让「上批已用 C03 C07」这种回避
        # 指令能落地。
        #
        # 只数编号不查内容:C01-C15 这种编号在正常中文里几乎不会误命中,是个
        # 高精度信号。判 SOFT 不判硬失败 —— 这是新增机制,老 run / 手工改过的
        # prompt 不该因为它被打回重试(遵循本函数其余检查的一贯尺度)。
        _angle_codes = set(re.findall(r"\bC(?:0[1-9]|1[0-5])\b", sp))
        if len(_angle_codes) < 10:
            issues.append(
                f"{cid}: 开头切入池只有 {len(_angle_codes)} 条编号角度"
                f"（跨批次多样性要求 15 条 C01-C15，少于 10 条时运营跑 5-10 批"
                f"就会撞车），建议补齐但不强制重试"
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
        if len(demo) > 50 and not _ending_looks_complete(demo, clean_endings):
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
        _raw_score = dim_data.get("score", 5)
        try:
            score = float(_raw_score)
        except (TypeError, ValueError):
            # 畸形 score(JSON null / 字符串 / 缺失)绝不能让合成崩溃:本函数在
            # run() 的 6.5 fail-closed 安全网里被调用,一旦抛 TypeError 会冒泡到
            # 顶层 except、跳过 save_output,把整份已组装好的 prompt_matrix 连同
            # 这次 run 一起判 failed(零产出)。畸形分视作满分=不为该维度合成。
            score = 5.0
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


# ── 重跑/修订共用的 stage 失效图(审计 COR-005 的根治) ──────────────────
#
# resume 断点恢复(run() 里的 done.get(...) 分支)、UI 的「追加并重跑」
# (pages/3)和「应用修订意见」(下面的 revise 路径)是同一份 stage_log 的
# 多个读者。历史上删除清单存在两份手工副本,pages/3 那份漏了精炼层快照
# (_matrix_ckpt_refined_a/b/c 等 9 个标记)和 red_blue_red/blue 等新名字,
# 结果「补充并重跑」后旧矩阵快照把新 builder 结果整体盖掉、精炼链被跳过,
# 用户拿回原封不动的旧稿且无任何报错。v0.34.1 的注释已经警告过"每新增一个
# resume 标记都要回来登记"——所以清单从现在起只允许存在这一份,两条路径
# 都必须调用 compute_stages_to_invalidate()。
#
# 主链路顺序(重跑起点的粒度)。动态名(strategy_debate_N / 旧版
# chancellery_N / gemini_trend_scout_post_*)不在这里,由函数统一展开。
PIPELINE_STAGE_ORDER: list[str] = [
    "crown_prince",
    "gemini_reference_analyzer",
    "gemini_trend_scout_pre",
    "secretariat",
    "dispatcher",
    "ministry_personnel", "ministry_revenue", "ministry_rites",
    "ministry_war", "ministry_justice",
    "ministry_works",
    "ministry_works_cell_planner",
    "ministry_works_builder",
    "narrative_director",
    "red_blue_refiner",        # legacy,v0.30.9 起不再写新 log,留着清旧
    "red_blue_red",            # v0.30.9 拆分,会写新 log
    "red_blue_blue",           # v0.30.9 拆分,会写新 log
    "persona_simulator",
    "persona_simulator_alt",   # v0.30.8 双 backend 并跑,会写新 log
    "ministry_works_structure_review",
    "vibe_critic",
    "vibe_rewriter",
    "structural_rewriter",
    "chancellery_final",
]

# 精炼层断点快照 + 终审后的收尾阶段。它们都 evaluate full matrix:只要
# builder 或任何精炼阶段重跑,这些必须全部失效——宁可多删(多花一点重算)
# 也不能少删(旧快照盖掉新结果)。
REFINEMENT_MARKER_STAGES: list[str] = [
    "_matrix_ckpt_refined_a",
    "_matrix_ckpt_refined_b",
    "_matrix_ckpt_refined_c",
    "_consumer_sim_done",
    "_persona_merged",
    "_structure_hints",
    "batch_sampling",
    "prose_gate",
    "quality_score",
]


def compute_stages_to_invalidate(
    from_stage: str, run_id: str, db: SupabaseClient
) -> list[str]:
    """给定重跑起点,返回必须删除的 stage_log 名字全集。

    覆盖:主链路 from_stage 及其下游、全部精炼层快照/收尾标记、
    strategy_debate_N 动态名、legacy chancellery_N 名、以及本 run 已存在的
    gemini_trend_scout_post_* 动态名。所有 rerun/revise 入口共用此函数,
    不允许各自维护删除清单。
    """
    if from_stage in PIPELINE_STAGE_ORDER:
        idx = PIPELINE_STAGE_ORDER.index(from_stage)
    else:
        idx = 0  # 未知起点按最保守处理:全删
    to_delete: set[str] = set(PIPELINE_STAGE_ORDER[idx:])

    # 精炼层快照/收尾标记:任何可选起点(最深为 vibe_critic)都会重算精炼链,
    # 一律清掉。
    to_delete.update(REFINEMENT_MARKER_STAGES)

    # 中书省+门下省实际以 strategy_debate_{turn} 写 log(v0.29.7 起)。
    # MAX_DEBATE_TURNS 当前为 4,20 是防未来上调的 buffer。
    if "secretariat" in to_delete:
        to_delete.update(f"strategy_debate_{i}" for i in range(20))

    # Legacy chancellery_1..9 命名(debate 之前的老版本)。当前代码不再写
    # 这些名字,删除仅为清理历史 run 的 UI 残留,无副作用。
    to_delete.update(f"chancellery_{i}" for i in range(1, 10))

    # 动态 per-direction post scan:final 批准后才跑,final 要重跑它就要清。
    # delete 是 exact-match,所以先扫一遍本 run 的 log 名字。
    if "chancellery_final" in to_delete:
        try:
            _all_logs = db.get_stage_logs(run_id) or []
            to_delete.update(
                l.get("stage_name", "")
                for l in _all_logs
                if (l.get("stage_name") or "").startswith(
                    "gemini_trend_scout_post_"
                )
            )
        except Exception:
            # 非致命:拿不到也只是 post 残留,不影响主流程正确性。
            pass

    return sorted(to_delete)


def resume_pipeline_in_background(project_id: str, run_id: str, db: SupabaseClient):
    """Resume a failed pipeline run — reuses existing run_id and skips completed stages.

    The double-click guard lives inside start_pipeline_in_background; we
    don't duplicate it here.
    """
    # Reset run status so UI shows it as running again
    # Refresh heartbeat_at when reviving a reused run row: its heartbeat is
    # stale from the previous attempt, and the heartbeat daemon only starts
    # ticking once start_pipeline_in_background's thread spins up. Without this,
    # a reaper pass in that startup window would see status=running + a stale
    # heartbeat and kill the healthy run we just resumed.
    db.update_pipeline_run(
        run_id,
        status="running",
        completed_at=None,
        heartbeat_at=datetime.now(timezone.utc).isoformat(),
    )
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
    # 0. Guard: atomically claim needs_revision → running BEFORE any
    # destructive action (stage_logs deletion, _revision_context write). Two
    # racing "apply revision" clicks: only one wins the CAS; the loser raises
    # here instead of destroying the winner's still-valid stage_logs.
    _assert_project_not_running(project_id, db, required_status="needs_revision")

    # 0.5 Flip the RUN to running immediately, BEFORE the destructive
    # deletion below. If this resume crashes/gets killed mid-deletion, a
    # running run with a stale heartbeat is reaper-collectable; a project
    # stuck 'running' with a non-running run would NOT be.
    # Refresh heartbeat_at when reviving a reused run row: its heartbeat is
    # stale from the previous attempt, and the heartbeat daemon only starts
    # ticking once start_pipeline_in_background's thread spins up. Without this,
    # a reaper pass in that startup window would see status=running + a stale
    # heartbeat and kill the healthy run we just resumed.
    db.update_pipeline_run(
        run_id,
        status="running",
        completed_at=None,
        heartbeat_at=datetime.now(timezone.utc).isoformat(),
    )

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
    if not is_global_revision and not affected_direction_ids:
        # 审计 COR-012:终审若用自然语言/中文方向名/小写 d1 描述修订,既没有
        # D+数字标记也没命中全局关键词,旧逻辑会当成"cell 级修订 + 空 affected
        # 集"——builder 日志全保留、一个 cell 都不重建,修订意见被静默丢弃,
        # 终审下一轮多半原样再驳。空 affected 集必须保守视为全局修订:
        # 多重建只是成本,少重建是丢用户的修订意见。
        is_global_revision = True
        logger.warning(
            "[revise] revisions carry no D\\d+ ids and no global keyword — "
            "treating as GLOBAL revision (conservative: rebuild all cells)"
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
    # Only inherit the round counter when the lingering context belongs to THIS
    # run. A stale context from an abandoned run would otherwise let a run that
    # has only been rejected once jump straight to (or past) the force-pass
    # round — or, worse, never advance because plain "重跑" pinned it.
    if prior_rc.get("run_id") == run_id:
        next_round = int(prior_rc.get("round", 1)) + 1
    else:
        next_round = 2  # round 1 was this run's fresh final review
    logger.info(f"[revise] advancing final-review round to {next_round}")

    revision_context = {
        "run_id": run_id,
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

    # 3. Selective deletion — 审计 COR-005:删除清单统一收敛到
    # compute_stages_to_invalidate,本路径不再手工维护副本(v0.34.1 的教训:
    # resume 和 revision 是同一份 stage_log 的两个读者,清单漏一项就会拿旧
    # 快照盖掉新结果)。
    # 全局修订:从工部·架构起全部失效(architect + cell_planner + builder
    # + 精炼链 + final + 全部断点快照)。
    # cell 级修订:保留 works/cell_planner/builder 日志,从 narrative_director
    # 起失效——cell 级 resume 会按 affected_direction_ids 把受影响 cell 排除
    # 出恢复集、强制重建。
    if is_global_revision:
        stages_to_redo = compute_stages_to_invalidate(
            "ministry_works", run_id, db
        )
        logger.info(
            "[revise] global revision → deleting ALL works stages + 中间精炼 + vibe + final"
        )
    else:
        stages_to_redo = compute_stages_to_invalidate(
            "narrative_director", run_id, db
        )
        logger.info(
            f"[revise] cell-specific revision → keeping builder stage_logs, "
            f"only D{'/D'.join(affected_direction_ids)} will be re-built"
        )

    deleted = db.delete_stage_logs_by_names(run_id, stages_to_redo)
    logger.info(
        f"[revise] Deleted {deleted} stage_logs: {stages_to_redo}"
    )

    # 4. Launch. The project was already atomically claimed as 'running' in
    # step 0 and the run set running in step 0.5, so tell
    # start_pipeline_in_background NOT to claim again (that would raise
    # AlreadyRunning against the status we ourselves just set).
    return start_pipeline_in_background(project_id, run_id, db, claim=False)
