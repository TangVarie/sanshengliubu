"""门下省 · Chancellery — Reviews plans and final output. Deliberately adversarial."""

import logging

from pipeline.agents import BaseAgent
from pipeline.config import (
    MAX_CHANCELLERY_REJECTIONS,
    MAX_FINAL_REJECTIONS,
    MODELS,
    STAGE_MAX_TOKENS,
    MAX_TOKENS_DEFAULT,
)
from db.supabase_client import SupabaseClient

logger = logging.getLogger(__name__)


# ── 终审输入瘦身 (v0.33.2) ────────────────────────────────────────────────
#
# 终审跑 kimi-k3($3 in / $15 out),是全流水线单价最高的一档,而它拿到的是
# **整个 final_system**。到终审这一步,final_system 上已经堆了 8 个上游阶段
# 挂的诊断数据 —— 而 chancellery.md 全文只引用两样东西:`prompt_matrix`
# 和单独注入的 `narrative_director_summary`。
#
# 最刺眼的是 demo 正文被送了**三遍**:
#   1. prompt_matrix[i].demo_output          ← 唯一被提示词引用的那份
#   2. demo_outputs[i].output_content        ← 提示词从未提及
#   3. _red_blue_stats.details[i].refined_demo_output  ← 同上
# 12 个格子 × 800 字 × 3 = 约 29K 字符,其中 19K 是纯副本。
#
# 加上 _persona_reactions(6 画像 × 12 反应)、_structure_review、
# _consumer_simulation、vibe_critic_result 这些提示词一个字没提的诊断包,
# 粗估 ~100K 字符进了单价最高的 stage,换零收益。
#
# 这是 v0.30.5 修的那批 "dead drop" 的镜像问题:那次是**数据注入了但提示词
# 不知道去读**,这次是**数据注入了而提示词根本不需要读**。前者漏信号,
# 后者烧钱,同一个根因 —— 没人管 final_system 上到底该有什么。
#
# 用 allowlist 而不是 blocklist:blocklist 挡不住"以后又有人往 final_system
# 上挂新字段"这个复发路径,而这正是问题的来源。代价是将来提示词要用新字段时
# 得记着来这里加一行 —— 所以下面会把丢掉的 key 打日志,drift 是可见的。
_FINAL_REVIEW_KEEP_KEYS: frozenset[str] = frozenset({
    "prompt_matrix",      # 终审的主体:system_prompt + demo_output
    "shared_skeleton",    # 合规块 / 关键词通用规则,判"一致性"要对照
    "system_name",
    "strategic_warnings", # 质量闸留下的告警,体量小,该让终审看见
})

# prompt_matrix 每个 cell 上的 UI 标记 / 上游诊断残留。终审审的是 prompt 本身,
# 这些既不该影响判决,又会挤占输入。
_CELL_STRIP_KEYS: frozenset[str] = frozenset({
    "_red_blue_summary", "_red_blue_status", "_structure_hint",
    "_revision_response", "_uncertainty",
})


def slim_system_for_final_review(final_system: dict) -> tuple[dict, list[str]]:
    """按 allowlist 裁剪 final_system,返回 (瘦身后的 dict, 被丢掉的 key 列表)。

    只保留 `chancellery.md` 实际引用的字段(见 _FINAL_REVIEW_KEEP_KEYS 上方的
    长注释)。叙事导演的诊断不在这里 —— 它由 run_final_review 单独抽成
    `narrative_director_summary` 注入,那条路径保持不变。
    """
    if not isinstance(final_system, dict):
        return final_system, []

    dropped = sorted(k for k in final_system if k not in _FINAL_REVIEW_KEEP_KEYS)
    slim = {
        k: v for k, v in final_system.items() if k in _FINAL_REVIEW_KEEP_KEYS
    }

    matrix = slim.get("prompt_matrix")
    if isinstance(matrix, list):
        slim["prompt_matrix"] = [
            {k: v for k, v in cell.items() if k not in _CELL_STRIP_KEYS}
            if isinstance(cell, dict) else cell
            for cell in matrix
        ]
    return slim, dropped


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

            # v0.33.2: 按 allowlist 瘦身。终审跑最贵的 kimi-k3,而 final_system
            # 上堆的 8 个上游诊断包提示词一个都没引用(demo 正文还被送了三遍)。
            # 详见 slim_system_for_final_review 上方的注释。
            _slim_system, _dropped = slim_system_for_final_review(final_system)
            if _dropped:
                logger.info(
                    "[chancellery_final] 输入瘦身:丢掉 %d 个提示词未引用的字段 %s"
                    "(要让终审看到新字段,去 _FINAL_REVIEW_KEEP_KEYS 加一行)",
                    len(_dropped), _dropped,
                )

            input_data = {
                "review_type": "final_review",
                "prompt_system": _slim_system,
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
