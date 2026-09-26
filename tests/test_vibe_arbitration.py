"""二审仲裁(v4-flash)每轮落一条 `vibe_arbitration` stage_log(v0.37.1),详情页 Tab 6 读它。

以前结果挂在 critic_result["_gemini_arbitration"] 上,但那是 vibe_critic.run() 写完
stage_log 之后才挂的,永远到不了库。这里钉住:每轮都有一条记录、内容是本轮真实的
二审结果、写库失败不影响循环、不改变哪些 cell 进重写、详情页能把它配回对应那一轮。
"""

from __future__ import annotations

import asyncio
import copy

import pytest

import pipeline.orchestrator as orc
from utils.stage_log_pairing import pair_following, second_review_summary

_DEMO = "开学第一天,室友拉开我抽屉愣了三秒,她以为那瓶是我姐落下的。我没解释,转身去洗脸。"


def _run(coro):
    return asyncio.run(coro)


def _matrix():
    return {"prompt_matrix": [
        {"cell_id": "D1_xhs", "direction_id": "D1", "platform": "小红书",
         "system_prompt": "你是……", "demo_output": _DEMO, "comment_seeds": ["楼主在哪买的"]},
    ]}


def _review(sev="pass"):
    return {"cell_id": "D1_xhs", "severity": sev,
            "multiplier_gate": {"reward_signal": "pass", "interest_align": "pass",
                                "gap_tension": "pass", "identity_consistency": "pass"},
            "template_test": {"still_holds": "no"}}


@pytest.fixture
def loop(orch, fake_db, monkeypatch):
    """像 BaseAgent.run 那样:vibe_critic 先建 log、返回前写 output_data。"""
    state = {"critic": [], "kimi": [], "rewriter": []}

    async def critic_run(inp, run_id, db):
        log = db.create_stage_log(run_id, "vibe_critic", {"cells": len(inp.get("prompt_cells") or [])})
        out = state["critic"].pop(0) if state["critic"] else {
            "verdict": "all_pass", "failed_cells": [], "cross_cell_duplicates": [], "cell_reviews": [_review()]}
        db.update_stage_log(log["id"], status="completed", output_data=copy.deepcopy(out))
        return out

    async def kimi_critic(cells):
        state["kimi_calls"] = state.get("kimi_calls", 0) + 1
        out = state["kimi"].pop(0) if state["kimi"] else {"verdict": "all_pass", "failed_cells": [], "cell_reviews": [_review()]}
        return copy.deepcopy(out)

    async def rewriter_run(inp, run_id, db):
        state["rewriter"].append(copy.deepcopy(inp.get("failed_cells") or []))
        return {"prompt_cells": [{**c, "demo_output": c.get("demo_output", "") + "改了一句。"}
                                 for c in inp.get("failed_cells") or []]}

    monkeypatch.setattr(orch.vibe_critic, "run", critic_run)
    monkeypatch.setattr(orch.vibe_rewriter, "run", rewriter_run)
    monkeypatch.setattr(orch.structural_rewriter, "run", rewriter_run)
    monkeypatch.setattr(orc, "run_kimi_critic", kimi_critic)
    orch._direction_index = {"D1": {"direction_id": "D1", "reward_type": "情绪共鸣",
                                    "stop_trigger": "刚搬进宿舍的人", "gap_direction": "事件本身"}}
    orch._cell_plan_index = {"D1_xhs": {"cell_id": "D1_xhs", "product_role": "副产品"}}
    orch._state = state
    return orch


def test_arbitration_is_persisted_even_though_vibe_critic_log_never_sees_it(loop, fake_db):
    loop._state["kimi"] = [{"verdict": "all_pass", "failed_cells": [], "cell_reviews": [_review()],
                            "_gemini_usage": {"input_tokens": 900, "output_tokens": 120, "cost_usd": 0.0012,
                                              "model": "deepseek-v4-flash"}}]
    fs = _run(loop._run_vibe_loop(_matrix(), {"advertising_stance": "stealth"}))
    # 原来的毛病:内存里有,库里那条 vibe_critic 没有
    assert fs["vibe_critic_result"]["_gemini_arbitration"]["verdict"] == "all_pass"
    (vc,) = fake_db.logs("vibe_critic")
    assert "_gemini_arbitration" not in (vc["output_data"] or {})
    # 现在:每轮一条 vibe_arbitration,内容就是本轮的二审结果
    (arb,) = fake_db.logs("vibe_arbitration")
    out = arb["output_data"]
    assert arb["status"] == "completed" and arb["model_used"] == "deepseek-v4-flash"
    assert "tokens_used" not in arb                         # 用量已进 run 总账,不再按 stage_log 加一遍
    assert out["round"] == "v1r1" and out["iteration"] == 1
    assert out["main_critic_passed"] == ["D1_xhs"] and out["second_review_added"] == []
    assert out["second_review"]["verdict"] == "all_pass" and out["second_review"]["_gemini_usage"]["cost_usd"] == 0.0012
    assert out["jev_shadow"] is None                        # 没配 JUDGE_URL
    # 详情页配得回来
    ((anchor, comp),) = pair_following(fake_db.stage_logs, "vibe_critic", ("vibe_arbitration", "jev_critic_shadow"))
    assert anchor is vc and comp == {"vibe_arbitration": arb}
    s = second_review_summary(comp["vibe_arbitration"]["output_data"])
    assert s["ran"] and s["verdict"] == "all_pass" and s["added"] == [] and s["usage"]["model"] == "deepseek-v4-flash"


def test_cells_added_by_second_review_are_recorded_and_routing_is_unchanged(loop, fake_db):
    flagged = {"cell_id": "D1_xhs", "platform": "小红书", "severity": "fail", "root_cause_kind": "surface",
               "rewrite_directives": "开头像广告"}
    loop._state["kimi"] = [{"verdict": "some_failed", "failed_cells": [flagged], "cell_reviews": [_review("fail")]}]
    _run(loop._run_vibe_loop(_matrix(), {}))
    arbs = fake_db.logs("vibe_arbitration")
    assert [a["output_data"]["round"] for a in arbs] == ["v1r1", "v1r2"]      # 每轮一条
    first = second_review_summary(arbs[0]["output_data"])
    assert first["added"] == ["D1_xhs"] and "开头像广告" in first["added_directives"]["D1_xhs"]
    assert second_review_summary(arbs[1]["output_data"])["added"] == []
    # 分流和以前一样:二审 flag 的 cell 带着二审标记进了重写,且只进一次
    assert len(loop._state["rewriter"]) == 1
    (sent,) = loop._state["rewriter"][0]
    assert sent["cell_id"] == "D1_xhs" and "【Gemini 二审提出】开头像广告" in sent["rewrite_directives"]
    # 两轮 vibe_critic 各配到自己那一轮的二审
    pairs = pair_following(fake_db.stage_logs, "vibe_critic", ("vibe_arbitration",))
    assert [c["vibe_arbitration"]["output_data"]["round"] for _, c in pairs] == ["v1r1", "v1r2"]


def test_not_run_when_main_critic_passed_nothing(loop, fake_db):
    failed = {"cell_id": "D1_xhs", "platform": "小红书", "severity": "fail", "root_cause_kind": "surface",
              "rewrite_directives": "x"}
    loop._state["critic"] = [{"verdict": "some_failed", "failed_cells": [failed], "cross_cell_duplicates": [],
                              "cell_reviews": [_review("fail")]}]
    _run(loop._run_vibe_loop(_matrix(), {}))
    arb = fake_db.logs("vibe_arbitration")[0]
    assert arb["status"] == "skipped" and arb["output_data"]["second_review"]["verdict"] == "not_run"
    assert loop._state.get("kimi_calls") == 1                 # 第一轮没调二审,第二轮(重写后全过)调了一次
    s = second_review_summary(arb["output_data"])
    assert not s["ran"] and s["skip_reason"]


def test_write_failure_does_not_break_the_loop(loop, fake_db, monkeypatch):
    real = fake_db.create_stage_log

    def flaky(run_id, stage_name, input_data=None):
        if stage_name == "vibe_arbitration":
            raise RuntimeError("db down")
        return real(run_id, stage_name, input_data)

    monkeypatch.setattr(fake_db, "create_stage_log", flaky)
    fs = _run(loop._run_vibe_loop(_matrix(), {}))
    assert fs["vibe_critic_result"]["verdict"] == "all_pass" and not fake_db.logs("vibe_arbitration")


def test_vibe_arbitration_invalidates_with_vibe_critic(fake_db):
    assert orc.REFINEMENT_MARKER_ANCHORS["vibe_arbitration"] == "vibe_critic"
    assert "vibe_arbitration" in set(orc.compute_stages_to_invalidate("vibe_critic", "run-1", fake_db))
    assert "vibe_arbitration" not in set(orc.compute_stages_to_invalidate("chancellery_final", "run-1", fake_db))


def test_pair_following_timeline():
    logs = [{"stage_name": "vibe_arbitration", "id": "stray"},                 # 第一条锚点之前的:丢
            {"stage_name": "vibe_critic", "id": "c1_failed_attempt"},
            {"stage_name": "vibe_critic", "id": "c1"},
            {"stage_name": "jev_critic_shadow", "id": "j1"},
            {"stage_name": "vibe_arbitration", "id": "a1"},
            {"stage_name": "vibe_rewriter", "id": "r1"},
            {"stage_name": "vibe_critic", "id": "c2"},
            {"stage_name": "vibe_arbitration", "id": "a2"},
            {"stage_name": "vibe_arbitration", "id": "a2_dup"}]
    pairs = pair_following(logs, "vibe_critic", ("vibe_arbitration", "jev_critic_shadow"))
    got = [(a["id"], {k: v["id"] for k, v in c.items()}) for a, c in pairs]
    assert got == [("c1_failed_attempt", {}), ("c1", {"jev_critic_shadow": "j1", "vibe_arbitration": "a1"}),
                   ("c2", {"vibe_arbitration": "a2"})]
    assert second_review_summary(None)["verdict"] == "unknown" and not second_review_summary({})["ran"]
