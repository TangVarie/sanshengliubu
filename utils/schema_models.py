"""Pydantic models for all inter-agent data contracts."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict


# ── Enums ──────────────────────────────────────────────────────────────────

class StageStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    NEEDS_INPUT = "needs_input"


class PipelineStatus(str, Enum):
    DRAFT = "draft"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    PAUSED = "paused_for_review"


# ── 太子 · Crown Prince Output ────────────────────────────────────────────

class ProductBrief(BaseModel):
    model_config = ConfigDict(extra="allow")

    product_name: str
    product_category: str
    core_claim: str
    target_platforms: list[str] = []
    target_audience: str = ""
    campaign_objective: list[str] = []
    competitive_context: str = ""
    constraints: str = ""
    task_type: str = "new_system"  # new_system | iteration | extension
    iteration_context: str = ""
    raw_materials: str = ""
    reference_summary: str = ""
    existing_prompt_analysis: str = ""


# ── 中书省 · Secretariat Output ───────────────────────────────────────────

class TacticalDirection(BaseModel):
    model_config = ConfigDict(extra="allow")

    direction_id: str
    direction_name: str
    rationale: str = ""
    target_scenario: str = ""
    content_angle: str = ""
    expected_output_type: str = ""
    # v0.27.0 "B 版思考顺序"三个必填字段 — secretariat 标注,下游 cell_planner
    # + vibe_critic 消费。extra="allow" 保留兼容性,未填时下游按默认值走。
    paradigm: str = ""  # "A_emotional_hook" | "B_meta_response"
    reward_type: str = ""  # 6 选 1: 实用信息/社交信息/情绪共鸣/认知挑战/感官新奇/身份共谋
    role_embodiment: str = ""  # 6 选 1: 旁观者/学习者/窥探者/受害者/共谋者/评审者
    gap_direction: str = ""  # 2 选 1: 事件本身 | 复现方法


class ModulePlan(BaseModel):
    model_config = ConfigDict(extra="allow")

    persona_needed: bool = True
    keyword_strategy_needed: bool = True
    controversy_design_needed: bool = False
    batch_management_needed: bool = True
    authenticity_mechanism_needed: bool = True


class PlatformDirection(BaseModel):
    """方向 × 平台矩阵中的一个格子"""
    model_config = ConfigDict(extra="allow")

    cell_id: str                     # "D1_xiaohongshu"
    direction_id: str                # "D1"
    platform: str                    # "小红书"
    platform_content_logic: str = "" # 这个方向在这个平台上的内容逻辑
    persona_strategy_notes: str = "" # 人设选择如何影响该格子的内容策略


class StrategicPlan(BaseModel):
    model_config = ConfigDict(extra="allow")

    system_name: str = ""
    strategic_insight: str
    tactical_directions: list[TacticalDirection] = []
    module_plan: ModulePlan | None = None
    estimated_directions_count: int = 5
    platform_specific_notes: str = ""
    architecture_type: str = "双层"  # 单层直出 | 双层 | 多层级联
    # Matrix fields
    target_platforms: list[str] = []
    platform_direction_matrix: list[PlatformDirection] = []
    matrix_skeleton: dict[str, Any] = {}  # active_cells + excluded_cells


# ── 门下省 · Chancellery Review ───────────────────────────────────────────

class ReviewDimension(BaseModel):
    model_config = ConfigDict(extra="allow")

    score: int = 3  # 1-5
    issues: str = ""


class ChancelleryReview(BaseModel):
    model_config = ConfigDict(extra="allow")

    verdict: str = "approved"  # approved | revision_required | rejected
    review_dimensions: dict[str, ReviewDimension] = {}
    mandatory_revisions: list[str] = []
    suggestions: list[str] = []
    revision_instructions: str = ""


# ── 尚书省 · Dispatcher Output ────────────────────────────────────────────

class MinistryTask(BaseModel):
    model_config = ConfigDict(extra="allow")

    objective: str
    inputs: list[str] = []
    deliverable: str = ""
    context: dict[str, Any] = {}


class DispatchResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    dispatch_id: str = ""
    tasks: dict[str, MinistryTask] = {}
    execution_order: str = ""
    dependencies: dict[str, list[str]] = {}


# ── 六部 · Ministry Outputs ───────────────────────────────────────────────

class PersonnelOutput(BaseModel):
    """吏部 — 人设工程部"""
    model_config = ConfigDict(extra="allow")

    personas: list[dict[str, Any]] = []
    differentiation_matrix: list[dict[str, Any]] = []
    naturalness_rules: list[str] = []


class RevenueOutput(BaseModel):
    """户部 — 关键词与搜索部"""
    model_config = ConfigDict(extra="allow")

    core_keywords: list[str] = []
    long_tail_keywords: list[str] = []
    blue_word_strategy: list[dict[str, Any]] = []
    search_scenario_mapping: list[dict[str, Any]] = []
    keyword_hierarchy: dict[str, Any] = {}


class RitesOutput(BaseModel):
    """礼部 — 调性与平台适配部"""
    model_config = ConfigDict(extra="allow")

    tone_standard: dict[str, Any] = {}
    platform_rules: dict[str, Any] = {}
    format_specs: dict[str, Any] = {}
    anti_ai_checklist: list[str] = []


class WarOutput(BaseModel):
    """兵部 — 竞争与争议设计部"""
    model_config = ConfigDict(extra="allow")

    comparison_framework: list[dict[str, Any]] = []
    controversy_triggers: list[dict[str, Any]] = []
    differentiation_strategy: list[str] = []
    defense_talking_points: list[dict[str, Any]] = []


class JusticeOutput(BaseModel):
    """刑部 — 合规与风控部"""
    model_config = ConfigDict(extra="allow")

    forbidden_words: list[str] = []
    platform_red_lines: list[str] = []
    category_compliance: list[dict[str, Any]] = []
    risk_levels: list[dict[str, Any]] = []


class WorksPlan(BaseModel):
    """工部规划阶段产出"""
    model_config = ConfigDict(extra="allow")

    shared_skeleton: dict[str, Any] = {}
    cell_plans: list[dict[str, Any]] = []
    persona_integration_strategy: str = ""
    total_cells: int = 0


class PromptMatrixCell(BaseModel):
    """矩阵中一个格子的最终 prompt"""
    model_config = ConfigDict(extra="allow")

    cell_id: str
    direction_id: str
    direction_name: str = ""
    platform: str
    system_prompt: str = ""
    user_prompt_template: str = ""
    variables: dict[str, Any] = {}
    persona_adaptation_rules: dict[str, Any] = {}
    demo_output: str = ""


class WorksOutput(BaseModel):
    """工部 — 结构工程部 (assembles final prompt system)"""
    model_config = ConfigDict(extra="allow")

    prompt_matrix: list[PromptMatrixCell] = []
    # Note: prompt_templates was removed in v0.6.1. Legacy data is read via
    # `extra="allow"` above so old runs still parse.
    matrix_dimensions: dict[str, list[str]] = {}
    demo_outputs: list[dict[str, Any]] = []


# ── 终审 · Final Review ───────────────────────────────────────────────────

class FinalReview(BaseModel):
    model_config = ConfigDict(extra="allow")

    verdict: str = "approved"  # approved | revision_required
    consistency_check: str = ""
    completeness_check: str = ""
    usability_check: str = ""
    sample_verification: str = ""
    notes: str = ""
