"""orchestrator 里的判定服务影子阶段:没配置零副作用、配了只记录不分流、
stage_log 名登记、画像第三路开关前后的判据。"""

from __future__ import annotations

import asyncio
import copy

import pytest

import pipeline.orchestrator as orc
from pipeline import jev_shadow as js
from pipeline.agents import get_run_totals, judge_client, reset_run_budget


def _run(coro):
    return asyncio.run(coro)


def _fail_if_called(*a, **kw):
    raise AssertionError("没配 JUDGE_URL / 开关关着,却调了判定服务")


# ── stage_log 登记 ────────────────────────────────────────────────────

def test_every_jev_stage_log_is_registered_with_an_anchor():
    for name in orc.JEV_STAGE_LOG_NAMES:
        assert name in orc.REFINEMENT_MARKER_ANCHORS, name
        assert orc.REFINEMENT_MARKER_ANCHORS[name] in orc.PIPELINE_STAGE_ORDER


def test_jev_stage_logs_invalidate_with_their_anchor(fake_db):
    from_vibe = set(orc.compute_stages_to_invalidate("vibe_critic", "run-1", fake_db))
    assert {"jev_critic_shadow", "jev_comment_regen", "jev_sample_shadow",
            "jev_comment_shadow"} <= from_vibe
    assert "jev_persona_route" not in from_vibe          # 画像在网感循环上游,保留
    from_final = set(orc.compute_stages_to_invalidate("chancellery_final", "run-1", fake_db))
    assert {"jev_sample_shadow", "jev_comment_shadow"} <= from_final
    assert not {"jev_critic_shadow", "jev_comment_regen"} & from_final
    from_nd = set(orc.compute_stages_to_invalidate("narrative_director", "run-1", fake_db))
    assert set(orc.JEV_STAGE_LOG_NAMES) <= from_nd


# ── _jev_judge:没配置 / 配了 ─────────────────────────────────────────

def test_jev_judge_not_configured_is_a_clean_skip(orch, fake_db):
    reset_run_budget("run-1")
    out = _run(orch._jev_judge("feature_questions_v0_1", [{"subject_id": "x"}], brief={}))
    assert out["status"] == "skipped" and out["reason"] == "not_configured"
    assert fake_db.stage_logs == []
    assert get_run_totals("run-1").get("cost_usd", 0.0) == 0.0


def test_jev_judge_records_cost_as_auxiliary(orch, monkeypatch):
    monkeypatch.setenv("JUDGE_URL", "http://judge.test")
    seen = {}

    def fake_many(bank, subjects, **kw):
        seen.update(kw, bank=bank)
        return {"results": [], "errors": 0, "written": None, "policy": None, "chunk_failures": [],
                "usage": {"input_tokens": 1_000_000, "output_tokens": 0}, "cost_usd": 0.042,
                "elapsed_ms": 5, "requests": 1}

    monkeypatch.setattr(judge_client, "judge_many", fake_many)
    reset_run_budget("run-1")
    out = _run(orch._jev_judge("comment_reader_v0.4", [{"subject_id": "x"}],
                               brief={"product_category": "OTC 感冒药"}))
    assert out["status"] == "ok"
    assert seen["project"] == "ssll:proj-1" and seen["category"] == "OTC药"
    assert seen["published"] is False and seen["write"] is False
    totals = get_run_totals("run-1")
    assert totals["cost_usd"] == pytest.approx(0.042)
    assert totals["aux_jev_judge_tokens"] == 1_000_000
    assert totals["input"] == 0                            # 不进主链路 token 熔断


def test_jev_judge_policy_refusal_is_a_skip(orch, monkeypatch):
    monkeypatch.setenv("JUDGE_URL", "http://judge.test")

    def refuse(*a, **kw):
        raise judge_client.JudgePolicyRefused("policy: 处方药项目不出境")

    monkeypatch.setattr(judge_client, "judge_many", refuse)
    out = _run(orch._jev_judge("feature_questions_v0_1", [{"subject_id": "x"}], brief={}))
    assert out["status"] == "skipped" and out["reason"] == "policy_blocked"


# ── 网感循环:二审影子只记录 ─────────────────────────────────────────

_DEMO = "开学第一天,室友拉开我抽屉愣了三秒,她以为那瓶是我姐落下的。我没解释,转身去洗脸。"


def _matrix():
    return {"prompt_matrix": [
        {"cell_id": "D1_xhs", "direction_id": "D1", "platform": "小红书",
         "system_prompt": "你是……", "demo_output": _DEMO, "comment_seeds": ["楼主在哪买的"]},
    ]}


def _all_pass_critic():
    return {"verdict": "all_pass", "failed_cells": [], "cross_cell_duplicates": [],
            "cell_reviews": [{"cell_id": "D1_xhs", "severity": "pass",
                              "multiplier_gate": {"reward_signal": "pass", "interest_align": "pass",
                                                  "gap_tension": "pass", "identity_consistency": "pass"},
                              "template_test": {"still_holds": "no"}}]}


@pytest.fixture
def vibe_stubs(orch, monkeypatch):
    async def critic_run(inp, run_id, db):
        return _all_pass_critic()

    async def rewriter_run(*a, **kw):
        raise AssertionError("影子阶段不该让任何 cell 进重写")

    async def kimi_critic(cells):
        return {"verdict": "all_pass", "failed_cells": [], "cell_reviews": []}

    monkeypatch.setattr(orch.vibe_critic, "run", critic_run)
    monkeypatch.setattr(orch.vibe_rewriter, "run", rewriter_run)
    monkeypatch.setattr(orch.structural_rewriter, "run", rewriter_run)
    monkeypatch.setattr(orc, "run_kimi_critic", kimi_critic)
    orch._direction_index = {"D1": {"direction_id": "D1", "reward_type": "情绪共鸣",
                                    "stop_trigger": "刚搬进宿舍、被室友看见自己私物的人",
                                    "gap_direction": "事件本身", "paradigm": "A_emotional_hook"}}
    orch._cell_plan_index = {"D1_xhs": {"cell_id": "D1_xhs", "product_role": "副产品"}}
    return orch


def test_vibe_loop_without_judge_url_has_no_jev_side_effects(vibe_stubs, fake_db, monkeypatch):
    monkeypatch.setattr(judge_client, "judge_many", _fail_if_called)
    fs = _run(vibe_stubs._run_vibe_loop(_matrix(), {"advertising_stance": "stealth"}))
    vcr = fs["vibe_critic_result"]
    assert "_jev_arbitration" not in vcr
    assert not [r for r in fake_db.stage_logs if r["stage_name"].startswith("jev_")]
    assert not fs.get("strategic_warnings")


def test_vibe_loop_critic_shadow_records_but_never_routes(vibe_stubs, fake_db, monkeypatch):
    monkeypatch.setenv("JUDGE_URL", "http://judge.test")
    sent = {}

    def jev_says_everything_fails(bank, subjects, **kw):
        sent.update(bank=bank, subjects=copy.deepcopy(subjects), kw=kw)
        return {"results": [{"subject_id": s["subject_id"], "items": {
                    "reward_signal": {"answer": "看不出或对不上", "p": 0.95},
                    "interest_align": {"answer": "错位", "p": 0.95},
                    "gap_tension": {"answer": "复现方法", "p": 0.95},
                    "identity_consistency": {"answer": "冒充", "p": 0.95},
                    "template_still_holds": {"answer": "仍然成立", "p": 0.95}},
                    "usage": {"input_tokens": 2000, "output_tokens": 0}} for s in subjects],
                "errors": 0, "written": None, "policy": {"published": False},
                "chunk_failures": [], "usage": {"input_tokens": 2000, "output_tokens": 0},
                "cost_usd": 0.000084, "elapsed_ms": 7, "requests": 1}

    monkeypatch.setattr(judge_client, "judge_many", jev_says_everything_fails)
    fs = _run(vibe_stubs._run_vibe_loop(_matrix(), {"advertising_stance": "stealth",
                                                    "product_category": "保健品"}))
    # Jev 全判 fail:没有任何 cell 进重写(rewriter 桩会炸)、没有策略告警
    assert not fs.get("strategic_warnings")
    rep = fs["vibe_critic_result"]["_jev_arbitration"]
    assert rep["status"] == "ok" and rep["cells"][0]["jev"]["gate_severity"] == "fail"
    assert rep["gate_severity_agreement_with_main"] == {"n": 1, "agree": 0, "rate": 0.0}
    # 发出去的 state 装了全部锚点,且只问二审那 5 题、不写账本
    s = sent["subjects"][0]
    assert sent["bank"] == "ssll_critic_v0.1"
    assert s["subject_id"] == "run-1:D1_xhs:demo:v1r1" and s["qids"] == list(js.CRITIC_QIDS)
    st = s["state"]
    assert st["触发点"].startswith("刚搬进宿舍") and st["奖励类型"] == "情绪共鸣"
    assert st["缺口方向"] == "事件本身" and st["产品角色"] == "副产品"
    assert st["广告姿态"] == "软植入"
    assert sent["kw"]["write"] is False and sent["kw"]["published"] is False
    assert sent["kw"]["project"] == "ssll:proj-1" and sent["kw"]["category"] == "保健品"
    logs = fake_db.logs("jev_critic_shadow")
    assert len(logs) == 1 and logs[0]["status"] == "completed"


def test_vibe_loop_matrix_cells_no_longer_carry_prose_soft_flags(vibe_stubs):
    m = _matrix()
    m["prompt_matrix"][0]["demo_output"] = _DEMO + "说白了其实换句话说也就是说更重要的是这点。"
    fs = _run(vibe_stubs._run_vibe_loop(m, {}))
    assert "_prose_soft_flags" not in fs["prompt_matrix"][0]
    assert fs["vibe_critic_result"]["_prose_soft_flags"]["D1_xhs"][0]["rule"] == "road_signs"


# ── 画像第三路 ─────────────────────────────────────────────────────────

def _persona_result(actions: dict[str, str]):
    """actions: cell_id → 三个画像统一的 action。"""
    return {"mode": "persona_spectrum", "summary": {},
            "personas": [{"id": pid, "profile": pid,
                          "reactions": [{"cell_id": cid, "action": a, "reaction": "…"}
                                        for cid, a in actions.items()]}
                         for pid in ("P_core", "P_edge", "P_anti")]}


def _persona_fs():
    return {"prompt_matrix": [
        {"cell_id": c, "direction_id": c[:2], "platform": "小红书", "demo_output": _DEMO}
        for c in ("D1_xhs", "D2_xhs", "D3_xhs")]}


@pytest.fixture
def persona_stubs(orch, monkeypatch):
    claude = _persona_result({"D1_xhs": "skip", "D2_xhs": "skip", "D3_xhs": "click"})
    deepseek = _persona_result({"D1_xhs": "skip", "D2_xhs": "click", "D3_xhs": "click"})

    async def c_run(*a, **kw):
        return copy.deepcopy(claude)

    async def d_run(*a, **kw):
        return copy.deepcopy(deepseek)

    monkeypatch.setattr(orch.persona_simulator, "run", c_run)
    monkeypatch.setattr(orch.persona_simulator_alt, "run", d_run)
    return orch


def _weak(fs):
    return sorted(w["cell_id"] for w in fs.get("strategic_warnings") or []
                  if w.get("source") == "persona_simulator")


def test_persona_flag_off_keeps_intersection_and_ignores_judge(persona_stubs, monkeypatch):
    monkeypatch.setenv("JUDGE_URL", "http://judge.test")          # 配了也不该调
    monkeypatch.setattr(judge_client, "judge_many", _fail_if_called)
    assert orc.ENABLE_JEV_PERSONA_ROUTE is False
    fs = _persona_fs()
    _run(persona_stubs._run_persona_simulation(fs, {"target_audience": "宝妈"}))
    assert _weak(fs) == ["D1_xhs"]                     # 两个后端都否决才算
    pkg = fs["_persona_reactions"]
    assert "_jev_route" not in pkg
    assert {p["_source"] for p in pkg["personas"]} == {"claude", "deepseek"}
    assert "vetoed_by" not in fs["strategic_warnings"][0]


def test_persona_flag_on_two_of_three(persona_stubs, fake_db, monkeypatch):
    monkeypatch.setattr(orc, "ENABLE_JEV_PERSONA_ROUTE", True)
    monkeypatch.setenv("JUDGE_URL", "http://judge.test")
    jev_actions = {"D1_xhs": "点开", "D2_xhs": "划走", "D3_xhs": "划走"}

    def fake_many(bank, subjects, **kw):
        assert bank == "ssll_critic_v0.1"
        out = []
        for s in subjects:
            cid = s["subject_id"].split(":")[1]
            a = jev_actions[cid]
            out.append({"subject_id": s["subject_id"], "items": {
                q: {"answer": a, "p": 0.9} for q in s["qids"]}})
        return {"results": out, "errors": 0, "written": None, "policy": None,
                "chunk_failures": [], "usage": {"input_tokens": 100, "output_tokens": 0},
                "cost_usd": 0.0, "elapsed_ms": 1, "requests": 1}

    monkeypatch.setattr(judge_client, "judge_many", fake_many)
    fs = _persona_fs()
    _run(persona_stubs._run_persona_simulation(fs, {"target_audience": "宝妈"}))
    # D1: claude+deepseek 否决(Jev 点开)→ 2 路;D2: claude+Jev → 2 路;D3: 只有 Jev → 不算
    assert _weak(fs) == ["D1_xhs", "D2_xhs"]
    by = {w["cell_id"]: w for w in fs["strategic_warnings"]}
    assert by["D2_xhs"]["vetoed_by"] == ["claude", "jev"]
    pkg = fs["_persona_reactions"]
    jev = [p for p in pkg["personas"] if p["_source"] == "jev"]
    assert len(jev) == 3 and all(len(p["reactions"]) == 3 for p in jev)
    assert pkg["_jev_route"]["status"] == "ok"
    assert fake_db.logs("jev_persona_route")[0]["status"] == "completed"


def test_persona_flag_on_but_judge_unconfigured_degrades_to_intersection(persona_stubs, monkeypatch):
    monkeypatch.setattr(orc, "ENABLE_JEV_PERSONA_ROUTE", True)
    monkeypatch.setattr(judge_client, "judge_many", _fail_if_called)
    fs = _persona_fs()
    _run(persona_stubs._run_persona_simulation(fs, {"target_audience": "宝妈"}))
    assert _weak(fs) == ["D1_xhs"]
    assert fs["_persona_reactions"]["_jev_route"]["reason"] == "not_configured"


# ── 采样 / 预埋评论影子 ───────────────────────────────────────────────

def _sampling():
    s = {"status": "ok", "per_cell": [{"cell_id": "D1_xhs", "platform": "小红书", "per_sample": [
        {"idx": 0, "seed": 1, "body": "标题:半夜\n正文:第一段。\n第二段。"},
        {"idx": 1, "seed": 21, "body": "另一篇,只有一段。"},
    ]}]}
    js.assign_sample_subject_ids(s, "run-1", "g1")
    return s


def test_sample_shadow_writes_ledger_only_for_fq(orch, fake_db, monkeypatch):
    monkeypatch.setenv("JUDGE_URL", "http://judge.test")
    calls = []

    def fake_many(bank, subjects, **kw):
        calls.append((bank, kw["write"], [s["subject_id"] for s in subjects]))
        return {"results": [{"subject_id": s["subject_id"], "items": {}} for s in subjects],
                "errors": 0, "written": len(subjects) if kw["write"] else None, "policy": None,
                "chunk_failures": [], "usage": {"input_tokens": 10, "output_tokens": 0},
                "cost_usd": 0.0, "elapsed_ms": 1, "requests": 1}

    monkeypatch.setattr(judge_client, "judge_many", fake_many)
    _run(orch._run_jev_sample_shadow(_sampling(), {}))
    by_bank = {b: (w, ids) for b, w, ids in calls}
    assert by_bank["feature_questions_v0_1"] == (True, ["run-1:D1_xhs:g1:1", "run-1:D1_xhs:g1:21"])
    assert by_bank["human_feel_para_v0.1"][0] is False
    assert by_bank["human_feel_para_v0.1"][1][0] == "run-1:D1_xhs:g1:1:p1"
    log = fake_db.logs("jev_sample_shadow")[0]
    assert log["status"] == "completed" and log["output_data"]["ledger"]["written"] == 2


def test_comment_shadow_sends_comment_type_unpublished_no_write(orch, fake_db, monkeypatch):
    monkeypatch.setenv("JUDGE_URL", "http://judge.test")
    calls = []

    def fake_many(bank, subjects, **kw):
        calls.append((bank, kw, subjects))
        return {"results": [{"subject_id": s["subject_id"], "items": {}} for s in subjects],
                "errors": 0, "written": None, "policy": None, "chunk_failures": [],
                "usage": {"input_tokens": 0, "output_tokens": 0}, "cost_usd": 0.0,
                "elapsed_ms": 1, "requests": 1}

    monkeypatch.setattr(judge_client, "judge_many", fake_many)
    _run(orch._run_jev_comment_shadow(_matrix(), {}))
    assert sorted(b for b, _k, _s in calls) == ["comment_reader_v0.4", "comment_thread_v0.3"]
    for _b, kw, subjects in calls:
        assert kw["write"] is False and kw["published"] is False
        assert kw["project"] == "ssll:proj-1"
        assert all(s["subject_type"] == "comment" for s in subjects)
    assert fake_db.logs("jev_comment_shadow")[0]["status"] == "completed"


def test_apply_comment_seeds():
    cells = [{"cell_id": "D1", "comment_seeds": ["旧"]}, {"cell_id": "D2", "comment_seeds": ["旧2"]}]
    assert orc._apply_comment_seeds(cells, {"D1": ["新1", "新2"], "D9": ["x"]}) == 1
    assert cells[0]["comment_seeds"] == ["新1", "新2"] and cells[1]["comment_seeds"] == ["旧2"]


def test_new_flags_default_off():
    from pipeline import config
    assert config.ENABLE_JEV_PERSONA_ROUTE is False
    assert config.ENABLE_JEV_COMMENT_REGEN is False
    assert config.JUDGE_TIMEOUT_SECONDS == 8.0


# ── 预埋评论重生成(默认关,这里直接调方法)──────────────────────────────

def _regen_fakes(monkeypatch, *, reader_status="ok"):
    import pipeline.agents.kimi_client as kc

    n = {"gen": 0}

    def fake_text(system, user, **kw):
        n["gen"] += 1
        return {"text": f"「(同方向引导)候选{n['gen']}」", "input_tokens": 5, "output_tokens": 5,
                "cost_usd": 0.0001, "model": "kimi-k2.6"}

    monkeypatch.setattr(kc, "call_kimi_text", fake_text)
    import pipeline.agents as agents
    monkeypatch.setattr(agents, "_get_active_limiter", lambda: None)   # 别让测试去排主链路的限流
    monkeypatch.setenv("JUDGE_URL", "http://judge.test")
    sent = []

    def fake_many(bank, subjects, **kw):
        sent.append((bank, kw, subjects))
        if reader_status == "policy":
            raise judge_client.JudgePolicyRefused("policy: 不出境")
        out = []
        for i, s in enumerate(subjects):
            # 第一条候选打成背书体(应被排掉),其余正常
            sa = "亲历背书" if s["subject_id"].endswith(":1") else None
            items = {"arranged": {"answer": "否", "p": 0.9}}
            if sa:
                items["speech_act"] = {"answer": sa, "p": 0.9}
            out.append({"subject_id": s["subject_id"], "items": items})
        return {"results": out, "errors": 0, "written": None, "policy": None, "chunk_failures": [],
                "usage": {"input_tokens": 1, "output_tokens": 0}, "cost_usd": 0.0,
                "elapsed_ms": 1, "requests": 1}

    monkeypatch.setattr(judge_client, "judge_many", fake_many)
    return sent


def test_comment_regen_writes_back_and_logs_marker(orch, fake_db, monkeypatch):
    sent = _regen_fakes(monkeypatch)
    fs = _matrix()
    _run(orch._run_jev_comment_regen(fs, {}))
    seeds = fs["prompt_matrix"][0]["comment_seeds"]
    assert len(seeds) == len(js.DEFAULT_COMMENT_SLOTS)
    assert all(not s.startswith(("(", "(", "「")) for s in seeds)      # 标签 / 引号已清
    endorsed = {s["state"]["评论原文"] for _b, _k, ss in sent for s in ss
                if s["subject_id"].endswith(":1")}
    assert endorsed and not endorsed & set(seeds)                       # 背书体那条被排掉
    log = fake_db.logs("jev_comment_regen")[0]
    assert log["status"] == "completed"
    assert log["output_data"]["applied"]["D1_xhs"] == seeds
    assert log["output_data"]["original"]["D1_xhs"] == ["楼主在哪买的"]
    assert {b for b, _k, _s in sent} == {"comment_reader_v0.4", "comment_thread_v0.3"}
    assert all(k["published"] is False and k["write"] is False for _b, k, _s in sent)
    # 回复位的 state 带着它回复的那条
    reply_subjects = [s for _b, _k, ss in sent for s in ss if ":regen:reply:" in s["subject_id"]]
    assert reply_subjects and reply_subjects[0]["state"]["回复对象"] == seeds[0]


def test_comment_regen_policy_refusal_keeps_original(orch, fake_db, monkeypatch):
    _regen_fakes(monkeypatch, reader_status="policy")
    fs = _matrix()
    _run(orch._run_jev_comment_regen(fs, {}))
    assert fs["prompt_matrix"][0]["comment_seeds"] == ["楼主在哪买的"]
    log = fake_db.logs("jev_comment_regen")[0]
    assert log["status"] == "skipped"
    assert log["output_data"]["abort_reason"] == "policy_blocked"


def test_circuit_opens_after_service_down(orch, monkeypatch):
    monkeypatch.setenv("JUDGE_URL", "http://judge.test")
    n = {"calls": 0}

    def down(*a, **kw):
        n["calls"] += 1
        raise judge_client.JudgeCallFailed("judge request timed out after 8s", kind="timeout")

    monkeypatch.setattr(judge_client, "judge_many", down)
    first = _run(orch._jev_judge("feature_questions_v0_1", [{"subject_id": "a"}], brief={}))
    second = _run(orch._jev_judge("comment_reader_v0.4", [{"subject_id": "b"}], brief={}))
    assert first["reason"] == "timeout" and second["reason"] == "circuit_open"
    assert n["calls"] == 1


def test_bank_level_failures_do_not_open_circuit(orch, monkeypatch):
    monkeypatch.setenv("JUDGE_URL", "http://judge.test")
    n = {"calls": 0}

    def missing(*a, **kw):
        n["calls"] += 1
        raise judge_client.JudgeCallFailed("没有题库", kind="bank_missing", status=404)

    monkeypatch.setattr(judge_client, "judge_many", missing)
    _run(orch._jev_judge("ssll_critic_v0.1", [{"subject_id": "a"}], brief={}))
    out = _run(orch._jev_judge("feature_questions_v0_1", [{"subject_id": "b"}], brief={}))
    assert out["reason"] == "bank_missing" and n["calls"] == 2


# ── run() 里的两个入口:没配置时零副作用 ─────────────────────────────────

def test_post_final_shadows_not_configured_touch_nothing(orch, fake_db, monkeypatch):
    monkeypatch.setattr(judge_client, "judge_many", _fail_if_called)
    fs = _matrix()
    fs["_batch_sampling"] = _sampling()
    before = copy.deepcopy(fs)
    _run(orch._run_jev_post_final_shadows(fs, {}, {}))
    assert fs == before
    assert fake_db.calls == []          # 连取消检查那次 get_pipeline_run 都没发


def test_post_final_shadows_resume_skips_done(orch, fake_db, monkeypatch):
    monkeypatch.setenv("JUDGE_URL", "http://judge.test")
    monkeypatch.setattr(judge_client, "judge_many", _fail_if_called)
    fs = _matrix()
    fs["_batch_sampling"] = _sampling()
    _run(orch._run_jev_post_final_shadows(
        fs, {}, {"jev_sample_shadow": {"x": 1}, "jev_comment_shadow": {"x": 1}}))
    assert [c[0] for c in fake_db.calls] == ["get_pipeline_run"]   # 只有取消检查


def test_post_final_shadows_propagates_cancel(orch, fake_db, monkeypatch):
    monkeypatch.setenv("JUDGE_URL", "http://judge.test")
    fake_db.get_pipeline_run = lambda run_id: {"status": "failed"}
    with pytest.raises(orc.PipelineCancelled):
        _run(orch._run_jev_post_final_shadows(_matrix(), {}, {}))


def test_comment_regen_entry_reapplies_marker_on_resume(orch, monkeypatch):
    monkeypatch.setattr(judge_client, "judge_many", _fail_if_called)
    fs = _matrix()
    _run(orch._maybe_run_jev_comment_regen(
        fs, {}, {"jev_comment_regen": {"applied": {"D1_xhs": ["新评论"]}}}))
    assert fs["prompt_matrix"][0]["comment_seeds"] == ["新评论"]


def test_comment_regen_entry_not_configured_keeps_seeds(orch, fake_db, monkeypatch):
    monkeypatch.setattr(judge_client, "judge_many", _fail_if_called)
    fs = _matrix()
    _run(orch._maybe_run_jev_comment_regen(fs, {}, {}))
    assert fs["prompt_matrix"][0]["comment_seeds"] == ["楼主在哪买的"]
    assert fake_db.calls == []
