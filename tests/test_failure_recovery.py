"""取样失败(积分不足)链路上的修复:报错落库只记一次且文案准确、续跑补回
用户原文 / 重跑残缺太子 / 复用已取到的样本、中文引号不再弄坏 JSON 解析、
设置页不直读 st.secrets、REQUIRED 可由环境变量关闭。"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

import pipeline.orchestrator as orc
from pipeline import config as cfg
from pipeline.agents import BaseAgent
from pipeline.agents.socialdatax_client import classify_failure

ROOT = Path(__file__).resolve().parent.parent

# 2026-10-26 生产上真实的取样失败原因(run 5e440827)
_QUOTA_REASON = (
    "call_failed: xhs/xhs_search_notes returned an error: 当前 API Key 积分不足。"
    "请访问 https://socialdatax.com/dashboard/billing 充值，或切换有余额的 API Key 后重试。"
)


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def project_db(fake_db):
    """fake_db + project 读写(orchestrator.run() 与取样阶段都要用)。"""
    fake_db.project = {"free_text": "用户原文:能量胶,马拉松补给", "brief": {}}
    fake_db.run_updates = []
    fake_db.get_project = lambda pid: fake_db.project
    fake_db.update_project = lambda pid, **kw: fake_db.project.update(kw)

    def _update_run(run_id, **kw):
        fake_db.run_updates.append(kw)

    fake_db.update_pipeline_run = _update_run
    return fake_db


# ── JSON 解析:中文引号不再把合法 JSON 弄成残缺 ───────────────────────

def test_chinese_quotes_inside_values_keep_every_field():
    # 修复前:引号归一化把值里的 “” 换成 ASCII ",合法 JSON 解析失败,截断
    # 修补从 raw_materials 处砍断 —— 只剩 product_name + _json_truncated。
    text = (
        '{"product_name": "能量胶", '
        '"raw_materials": "用户说“很好用”,复购率高", '
        '"reference_summary": "方法论“三段式”原文"}'
    )
    out = BaseAgent._extract_json(text)
    assert out == {
        "product_name": "能量胶",
        "raw_materials": "用户说“很好用”,复购率高",
        "reference_summary": "方法论“三段式”原文",
    }


def test_smart_quotes_used_as_delimiters_still_parse():
    assert BaseAgent._lenient_json_loads("{“a”: “b”}") == {"a": "b"}


def test_real_truncation_is_still_repaired_and_flagged():
    out = BaseAgent._extract_json('{"a": "他说“好”", "b": "写到一半')
    assert out["a"] == "他说“好”"            # 值里的中文引号原样保留
    assert out["_json_truncated"] is True
    assert "b" not in out


# ── 失败分类与报错文案 ─────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("reason", "kind"),
    [
        (_QUOTA_REASON, "quota_exhausted"),
        ("call_failed: xhs/x returned an error: HTTP 402 Payment Required", "quota_exhausted"),
        ("not_configured: SOCIALDATAX_API_KEY not set", "not_configured"),
        ("call_failed: xhs/x returned an error: 401 Unauthorized", "auth"),
        ("call_failed: xhs/x returned an error: invalid api key", "auth"),
        ("call_failed: xhs/x: Read timed out", "other"),
        ("unexpected_exception: boom", "other"),
    ],
)
def test_classify_failure(reason, kind):
    assert classify_failure(reason) == kind


def test_quota_message_is_actionable_for_railway():
    msg = orc._trend_scout_required_message(_QUOTA_REASON, "quota_exhausted")
    assert "socialdatax.com/dashboard/billing" in msg
    assert "worker 服务" in msg                      # key 配在 worker,不是 secrets.toml
    assert "「继续执行」" in msg and "不要点「重跑流水线」" in msg
    assert "SOCIALDATAX_TREND_SCOUT_PRE_REQUIRED=0" in msg  # 运行时开关,不用改代码
    assert "。。" not in msg
    assert "请重试" not in msg and "pipeline/config.py" not in msg


def test_transient_failure_message_does_not_send_user_to_billing():
    msg = orc._trend_scout_required_message("call_failed: timed out", "other")
    assert "billing" not in msg and "「继续执行」" in msg


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, True), ("", True), ("0", False), ("false", False), ("OFF", False),
     ("1", True), ("yes", True)],
)
def test_required_flag_env_override(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("X_TEST_FLAG", raising=False)
    else:
        monkeypatch.setenv("X_TEST_FLAG", raw)
    assert cfg._env_flag("X_TEST_FLAG", True) is expected


# ── 取样阶段:复用 / 积分不足 ─────────────────────────────────────────

_POSTS = [{"title": "马拉松补给实测", "snippet": "30km 撞墙前吃了一支", "url": "u1",
           "engagement_display": "赞 1.2万"}]


def test_scout_reuses_this_runs_previous_posts_without_calling_socialdatax(
    orch, project_db, monkeypatch
):
    async def _must_not_call(**kw):
        raise AssertionError("续跑时已有成功取样,不应再调 SocialDataX")

    monkeypatch.setattr(orc, "run_trend_scout", _must_not_call)
    done = {"gemini_trend_scout_pre": {
        "verdict": "all_pass", "posts": _POSTS, "queries_used": ["能量胶"],
        "grounding_urls": [], "_engagement_resolved": True,
    }}
    brief: dict = {}
    _run(orch._run_gemini_trend_scout_pre(brief, done))
    assert brief["_trend_intel"]["posts"] == _POSTS
    assert "马拉松补给实测" in brief["_trend_intel"]["formatted_block"]
    assert project_db.logs("gemini_trend_scout_pre") == []   # 不新建 stage_log
    assert project_db.project["brief"]["_trend_intel"]["posts"] == _POSTS


def test_scout_quota_failure_records_kind_and_actionable_error(
    orch, project_db, monkeypatch
):
    async def _quota(**kw):
        return {"verdict": "skipped", "posts": [], "queries_used": [],
                "_skip_reason": _QUOTA_REASON}

    monkeypatch.setattr(orc, "run_trend_scout", _quota)
    monkeypatch.setattr(orc, "SOCIALDATAX_TREND_SCOUT_PRE_REQUIRED", True)
    monkeypatch.setattr(orc, "ENABLE_SOCIALDATAX_TREND_SCOUT_PRE", True)
    with pytest.raises(orc.TrendScoutRequiredError) as ei:
        _run(orch._run_gemini_trend_scout_pre({"product_category": "能量胶"}, {}))
    (log,) = project_db.logs("gemini_trend_scout_pre")
    assert log["status"] == "failed"
    assert log["output_data"]["_failure_kind"] == "quota_exhausted"
    assert log["error_message"] == str(ei.value)
    assert "「继续执行」" in str(ei.value)


# ── run():失败只落一次、文案准确;续跑补回原文 / 重跑残缺太子 ─────────

@pytest.fixture
def run_stubs(orch, project_db, monkeypatch):
    """把 run() 跑到取样就停:参考分析器空跑,取样按测试指定的方式失败。"""
    seen: dict = {}

    async def _ref_analyzer(brief):
        seen["brief"] = brief

    monkeypatch.setattr(orch, "_run_gemini_reference_analyzer", _ref_analyzer)

    def _fail_scout_with(exc):
        async def _scout(brief, done=None):
            raise exc
        monkeypatch.setattr(orch, "_run_gemini_trend_scout_pre", _scout)

    seen["fail_scout_with"] = _fail_scout_with
    return seen


def _seed_crown_prince(db, output):
    row = db.create_stage_log("run-1", "crown_prince", {})
    db.update_stage_log(row["id"], status="completed", output_data=output)


def test_precondition_failure_writes_one_clean_marker_and_does_not_raise(
    orch, project_db, run_stubs
):
    _seed_crown_prince(project_db, {"product_name": "能量胶"})
    run_stubs["fail_scout_with"](orc.TrendScoutRequiredError("积分不足,充值后点「继续执行」"))
    assert _run(orch.run()) is None          # 不 re-raise → _thread_target 不会再记一条
    markers = [r["stage_name"] for r in project_db.stage_logs
               if r["stage_name"].startswith("_")]
    assert markers == ["_precondition_failed"]
    (m,) = project_db.logs("_precondition_failed")
    assert m["error_message"] == "积分不足,充值后点「继续执行」"   # 无 traceback / 无误导前缀
    assert {"status": "failed"}.items() <= project_db.run_updates[-1].items()


def test_unexpected_failure_is_tagged_so_thread_target_skips_it(
    orch, project_db, run_stubs
):
    _seed_crown_prince(project_db, {"product_name": "能量胶"})
    run_stubs["fail_scout_with"](RuntimeError("boom"))
    with pytest.raises(RuntimeError) as ei:
        _run(orch.run())
    assert getattr(ei.value, "_failure_recorded_by_run", False) is True
    (m,) = project_db.logs("_pipeline_error")
    assert "RuntimeError: boom" in m["error_message"]
    assert "工部阶段的校验失败" not in m["error_message"]


def test_resume_restores_user_raw_input_and_passthrough_fields(
    orch, project_db, run_stubs
):
    project_db.project["brief"] = {
        "_screenshot_analysis_text": "截图:配料表含 BCAA",
        "_reference_post_urls": ["https://www.xiaohongshu.com/explore/abc"],
    }
    _seed_crown_prince(project_db, {"product_name": "能量胶"})
    run_stubs["fail_scout_with"](orc.TrendScoutRequiredError("stop"))
    _run(orch.run())
    brief = run_stubs["brief"]
    assert brief["_user_raw_input"].startswith("用户原文:能量胶")
    assert "截图:配料表含 BCAA" in brief["_user_raw_input"]
    assert brief["_raw_input_text"] == brief["_user_raw_input"]
    assert brief["_reference_post_urls"] == ["https://www.xiaohongshu.com/explore/abc"]


def test_truncated_crown_prince_is_rerun_on_resume_with_clean_input(
    orch, project_db, run_stubs, monkeypatch
):
    # 本 run 的太子产出是残缺的,下游还没建策略 → 续跑重跑太子
    project_db.project["brief"] = {
        "product_name": "能量胶", "_json_truncated": True,
        "_user_raw_input": "x" * 50, "_raw_input_text": "x" * 50,
    }
    _seed_crown_prince(project_db, {"product_name": "能量胶", "_json_truncated": True})
    calls = []

    async def _crown_prince(agent, raw_input):
        calls.append(raw_input)
        return {"product_name": "能量胶", "raw_materials": "完整素材"}

    monkeypatch.setattr(orch, "_run_with_clarification", _crown_prince)
    run_stubs["fail_scout_with"](orc.TrendScoutRequiredError("stop"))
    _run(orch.run())
    (raw_input,) = calls
    # 派生字段不再混进太子输入(原文只经 free_text 传一份)
    assert not {"_user_raw_input", "_raw_input_text", "_json_truncated"} & set(raw_input["brief"])
    assert run_stubs["brief"]["raw_materials"] == "完整素材"


def test_truncated_crown_prince_is_kept_once_strategy_exists(
    orch, project_db, run_stubs, monkeypatch
):
    _seed_crown_prince(project_db, {"product_name": "能量胶", "_json_truncated": True})
    row = project_db.create_stage_log("run-1", "strategy_debate_0", {})
    project_db.update_stage_log(row["id"], status="completed", output_data={"x": 1})

    async def _must_not_rerun(agent, raw_input):
        raise AssertionError("已建策略时不应重跑太子")

    monkeypatch.setattr(orch, "_run_with_clarification", _must_not_rerun)
    run_stubs["fail_scout_with"](orc.TrendScoutRequiredError("stop"))
    _run(orch.run())
    assert run_stubs["brief"]["_json_truncated"] is True


# ── 后台线程:run() 已落库的失败不再重复记 ─────────────────────────────

def _run_thread_with(monkeypatch, project_db, exc):
    class _Orch:
        def __init__(self, *a):
            pass

        async def run(self):
            raise exc

    monkeypatch.setattr(orc, "PipelineOrchestrator", _Orch)
    orc.start_pipeline_in_background("proj-1", "run-1", project_db, claim=False).join(5)


def test_thread_target_skips_failures_already_recorded_by_run(monkeypatch, project_db):
    exc = RuntimeError("boom")
    exc._failure_recorded_by_run = True
    _run_thread_with(monkeypatch, project_db, exc)
    assert project_db.logs("_thread_target") == []
    assert not any(u.get("status") == "failed" for u in project_db.run_updates)


def test_thread_target_still_catches_failures_outside_run(monkeypatch, project_db):
    _run_thread_with(monkeypatch, project_db, RuntimeError("init blew up"))
    (m,) = project_db.logs("_thread_target")
    assert "init blew up" in m["error_message"]
    assert any(u.get("status") == "failed" for u in project_db.run_updates)


# ── 设置页 ───────────────────────────────────────────────────────────

def test_pages_do_not_read_st_secrets_directly():
    # Railway 的 web 服务没有 secrets.toml:st.secrets.get(k, default) 直接抛
    # StreamlitSecretNotFoundError,设置页曾因此在生产上一打开就崩。
    offenders = []
    for path in sorted((ROOT / "pages").glob("*.py")):
        for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]
            if re.search(r"\bst\.secrets\b", code):
                offenders.append(f"{path.name}:{no}")
    assert offenders == [], offenders
