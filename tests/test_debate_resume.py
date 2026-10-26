"""「应用修订意见并重跑」后中书省被整场重辩、撞上模型空返回失败(run 5e440827):
末轮强制放行要回写 stage_log、老 run 能从跑满的辩论重建方案、旧失败横幅随新
尝试作废、空返回报成空返回而不是 JSON 解析失败、多个 text 块全部拼接。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS

import pytest

import pipeline.agents as agents_mod
import pipeline.orchestrator as orc
from pipeline.agents import EmptyModelResponseError, reset_run_budget
from pipeline.agents.crown_prince import CrownPrince

_PLAN = {"tactical_directions": [{"direction_id": "D1"}, {"direction_id": "D2"}]}


def _run(coro):
    return asyncio.run(coro)


class _LoggingAgent:
    """模拟 BaseAgent.run:按当前 stage_name 写一条 stage_log 再返回产出。"""

    def __init__(self, outputs):
        self.stage_name = "agent"
        self.outputs = list(outputs)

    async def run(self, input_data, run_id, db):
        out = self.outputs.pop(0)
        row = db.create_stage_log(run_id, self.stage_name, {})
        if isinstance(out, Exception):
            db.update_stage_log(row["id"], status="failed", error_message=str(out))
            raise out
        db.update_stage_log(row["id"], status="completed", output_data=out)
        return out


@pytest.fixture
def debate(orch, fake_db, monkeypatch):
    def _setup(max_turns, sec_outputs, chan_outputs):
        monkeypatch.setattr(orc, "MAX_DEBATE_TURNS", max_turns)
        orch.secretariat = _LoggingAgent(sec_outputs)
        orch.chancellery = _LoggingAgent(chan_outputs)

        async def _with_clarification(agent, input_data):
            return await agent.run(input_data, orch.run_id, fake_db)

        monkeypatch.setattr(orch, "_run_with_clarification", _with_clarification)
    return _setup


# ── 末轮放行写回 stage_log ─────────────────────────────────────────────

def test_forced_pass_on_last_turn_is_written_back(orch, fake_db, debate):
    debate(2, [_PLAN], [{"verdict": "challenge", "challenges": [{"point": "太抽象"}]}])
    assert _run(orch._strategy_loop({})) == _PLAN
    (log,) = fake_db.logs("strategy_debate_1")
    assert log["status"] == "completed"
    assert log["output_data"]["verdict"] == "approved"
    assert log["output_data"]["_forced_pass"] is True
    assert log["output_data"]["challenges"] == [{"point": "太抽象"}]  # 质疑保留


def test_synthetic_pass_after_failed_last_call_is_written_back(orch, fake_db, debate):
    debate(2, [_PLAN], [RuntimeError("upstream 502")])
    assert _run(orch._strategy_loop({})) == _PLAN
    (log,) = fake_db.logs("strategy_debate_1")
    assert log["status"] == "completed"
    assert log["output_data"]["_synthetic_pass"] is True


# ── run():续跑 / 应用修订时重建策略 ────────────────────────────────────

def _seed(db, name, output, status="completed"):
    row = db.create_stage_log("run-1", name, {})
    db.update_stage_log(row["id"], status=status, output_data=output)
    return row


@pytest.fixture
def run_until_strategy(orch, project_db, monkeypatch):
    """run() 跑到策略之后就停(在「策略辩论之后」的取消检查点抛 PipelineCancelled)。"""

    async def _noop(*a, **kw):
        return None

    monkeypatch.setattr(orch, "_run_gemini_reference_analyzer", _noop)
    monkeypatch.setattr(orch, "_run_gemini_trend_scout_pre", _noop)

    def _check(where):
        if where == "策略辩论之后":
            raise orc.PipelineCancelled("test stop")

    monkeypatch.setattr(orch, "_check_cancelled", _check)
    _seed(project_db, "crown_prince", {"product_name": "能量胶"})
    return project_db


@pytest.fixture
def project_db(fake_db):
    fake_db.project = {"free_text": "原文", "brief": {}}
    fake_db.get_project = lambda pid: fake_db.project
    fake_db.update_project = lambda pid, **kw: fake_db.project.update(kw)
    fake_db.update_pipeline_run = lambda *a, **kw: None
    return fake_db


def _seed_legacy_debate(db, turns):
    """老格式辩论:中书省每轮一份方案(D<turn>),门下省每轮 challenge,
    末轮强制放行没回写。"""
    for t in range(turns):
        if t % 2 == 0:
            _seed(db, f"strategy_debate_{t}",
                  {"current_plan": {"tactical_directions": [{"direction_id": f"D{t}"}]}})
        else:
            _seed(db, f"strategy_debate_{t}", {"verdict": "challenge", "challenges": [1]})


@pytest.mark.parametrize(
    ("ran_turns", "max_now", "reused"),
    [
        (4, 4, "strategy_debate_2"),   # 跑满当前上限
        (8, 6, "strategy_debate_6"),   # 上限后来调小了:真正放行的是第 7 轮
    ],
)
def test_resume_reuses_plan_of_a_full_length_debate_with_legacy_forced_pass(
    orch, run_until_strategy, monkeypatch, caplog, ran_turns, max_now, reused
):
    monkeypatch.setattr(orc, "MAX_DEBATE_TURNS", max_now)
    _seed_legacy_debate(run_until_strategy, ran_turns)

    async def _must_not_rerun(brief):
        raise AssertionError("辩论已跑满并强制放行,不应整场重辩")

    monkeypatch.setattr(orch, "_strategy_loop", _must_not_rerun)
    with caplog.at_level("INFO", logger="pipeline.orchestrator"):
        assert _run(orch.run()) is None      # 停在「策略辩论之后」
    assert f"沿用 {reused} 的方案" in caplog.text


def test_resume_reruns_when_limit_was_raised_after_the_run(
    orch, run_until_strategy, monkeypatch
):
    # 跑的时候上限 4(第 3 轮强制放行),之后调到 6:够不到新上限,保守重辩
    monkeypatch.setattr(orc, "MAX_DEBATE_TURNS", 6)
    _seed_legacy_debate(run_until_strategy, 4)
    calls = []

    async def _rerun(brief):
        calls.append(brief)
        return _PLAN

    monkeypatch.setattr(orch, "_strategy_loop", _rerun)
    _run(orch.run())
    assert len(calls) == 1


def test_resume_still_reruns_an_unfinished_debate(orch, run_until_strategy, monkeypatch):
    # 只跑了 2/4 轮、门下省也没批 —— 没有可信的定稿,必须重辩
    monkeypatch.setattr(orc, "MAX_DEBATE_TURNS", 4)
    _seed(run_until_strategy, "strategy_debate_0", {"current_plan": _PLAN})
    _seed(run_until_strategy, "strategy_debate_1", {"verdict": "challenge", "challenges": [1]})
    calls = []

    async def _rerun(brief):
        calls.append(brief)
        return _PLAN

    monkeypatch.setattr(orch, "_strategy_loop", _rerun)
    _run(orch.run())
    assert len(calls) == 1


def test_new_attempt_supersedes_previous_failure_markers(orch, run_until_strategy, monkeypatch):
    old_tt = _seed(run_until_strategy, "_thread_target", None, status="failed")
    old_pe = _seed(run_until_strategy, "_pipeline_error", None, status="failed")

    async def _scout_fails(brief, done=None):
        raise orc.TrendScoutRequiredError("积分不足")

    monkeypatch.setattr(orch, "_run_gemini_trend_scout_pre", _scout_fails)
    _run(orch.run())
    assert old_tt["status"] == "superseded" and old_pe["status"] == "superseded"
    (new,) = run_until_strategy.logs("_precondition_failed")
    assert new["status"] == "failed"         # 只剩本次失败会上横幅


# ── 模型空返回 ───────────────────────────────────────────────────────

def test_empty_model_text_is_reported_as_empty_not_as_json_error(fake_db, monkeypatch):
    monkeypatch.setattr(agents_mod, "MAX_RETRIES", 0)
    reset_run_budget("run-1")
    agent = CrownPrince()
    monkeypatch.setattr(
        agent, "_call_model",
        lambda *a: ("", 18000, 7800, 0, 0, False, "kimi-k3"),
    )
    with pytest.raises(EmptyModelResponseError):
        _run(agent.run({"free_text": "x"}, "run-1", fake_db))
    (log,) = fake_db.logs("crown_prince")
    assert "空内容" in log["error_message"]
    assert "output_tokens=7800" in log["error_message"]
    assert "Could not extract JSON" not in log["error_message"]


def test_all_text_blocks_are_joined(monkeypatch):
    # 上游先给一个空 text 块、正文在第二块:旧代码只取第一块 → 0 字
    response = NS(
        content=[NS(type="thinking", thinking="…"), NS(type="text", text=""),
                 NS(type="text", text='{"ok": 1}')],
        usage=NS(input_tokens=10, output_tokens=5,
                 cache_read_input_tokens=0, cache_creation_input_tokens=0),
        stop_reason="end_turn",
    )

    class _Stream:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get_final_message(self):
            return response

    client = NS(messages=NS(stream=lambda **kw: _Stream()))
    agent = CrownPrince()
    monkeypatch.setattr(agent, "_get_client_for_model", lambda m: client)
    monkeypatch.setattr(agent, "_model_fallback_candidates", lambda m: [m])
    monkeypatch.setitem(agents_mod._api_config, "mode", "direct")
    monkeypatch.setitem(agents_mod._api_config, "supports_cache", False)
    text, *_ = agent._call_model("SYS", '{"x": 1}')
    assert text == '{"ok": 1}'
