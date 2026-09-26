"""jev_shadow 的纯函数:state 形状、选项映射、对照统计、采样 id、评论 subject。"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from pipeline import jev_shadow as js

# TV feature_bank.PERFORMANCE_KEYWORDS 加上本仓的几个中文说法:Mode A,state 里一个都不能有
_PERF_WORDS = ("tier", "大爆", "爆贴", "爆款", "爆率", "impressions", "reads", "interactions",
               "互动数", "阅读数", "曝光", "评论数", "performance", "实际表现", "点赞", "收藏")


def _no_perf(obj) -> None:
    text = json.dumps(obj, ensure_ascii=False).lower()
    hits = [w for w in _PERF_WORDS if w.lower() in text]
    assert not hits, f"state 里出现了表现类字眼:{hits}"


# ── 项目与品类 ────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,want", [
    ("处方药", "处方药"),
    ("非处方药(OTC)", "OTC药"),
    ("otc 感冒药", "OTC药"),
    ("保健食品", "保健品"),
    ("医疗器械", "医疗器械"),
    ("感冒药", "处方药"),          # 分不清处方 / 非处方 → 从严按处方药
    ("药妆护肤", "美妆"),          # 含「药」字但不是药品
    ("护肤", "美妆"),
    ("零食", "食品饮料"),
    ("婴幼儿奶粉", "母婴"),
    ("药食同源零食", "食品饮料"),
    # 认不出 → None(orchestrator 据此不发,codex review P1 on #53)
    ("司美格鲁肽", None),
    ("Ozempic", None),
    ("降糖针", None),
    ("医用敷料", None),           # 沾医疗但不是明写的医疗器械:不猜
    ("特医食品", None),           # 含「食品」也不归到食品饮料
    ("医美护肤", None),
    ("其他", None),
    ("", None),
    (None, None),
])
def test_map_category(text, want):
    assert js.map_category(text) == want


def test_judge_scope_always_has_project():
    s = js.judge_scope("proj-1", {"product_category": "保健品"})
    assert s == {"project": "ssll:proj-1", "category": "保健品"}
    assert js.judge_scope("proj-1", None) == {"project": "ssll:proj-1", "category": None}


def test_judge_scope_override(monkeypatch):
    monkeypatch.setitem(js.JUDGE_PROJECT_CATEGORY_OVERRIDES, "proj-1", "其他")
    monkeypatch.setitem(js.JUDGE_PROJECT_CATEGORY_OVERRIDES, "proj-2", "不在词表里")
    assert js.judge_scope("proj-1", {"product_category": "降糖针"})["category"] == "其他"
    assert js.judge_scope("proj-2", {"product_category": "降糖针"})["category"] is None


@pytest.mark.parametrize("platform,noun", [("小红书", "一篇小红书帖子"), ("", "一篇小红书帖子"),
                                           ("抖音", "一条抖音内容"), ("B站", "一条B站内容")])
def test_comment_states_use_the_cell_platform(platform, noun):
    cell = {"cell_id": "D1", "platform": platform, "demo_output": "标题:t\n正文:第一句很长很长的正文内容。",
            "comment_seeds": ["楼主在哪买的", "我也想问"]}
    reader, thread = js.comment_subjects("run-1", cell)
    assert reader[0]["state"]["说明"].startswith(f"以下是{noun}和它下面的一条候选评论")
    assert thread["state"]["说明"].startswith(f"以下是{noun}和它下面能看到的全部评论")


# ── ① 二审影子 ────────────────────────────────────────────────────────

_CELL = {
    "cell_id": "D1_xhs", "direction_id": "D1", "platform": "小红书",
    "demo_output": "半夜痔疮破了,摸黑抹药,睡醒发现是甜辣酱。",
    "reward_type": "情绪共鸣", "stop_trigger": "最近被家人健康吓到、半夜睡不着的人",
    "gap_direction": "事件本身", "product_role": "副产品",
}


def test_critic_state_carries_all_anchors():
    st = js.critic_state(_CELL, {"advertising_stance": "stealth"}, None)
    assert st["奖励类型"] == "情绪共鸣"
    assert st["触发点"] == _CELL["stop_trigger"]
    assert st["缺口方向"] == "事件本身"
    assert st["产品角色"] == "副产品"
    assert st["广告姿态"] == "软植入" and "冒充" in st["姿态说明"]
    assert st["正文"] == _CELL["demo_output"]
    _no_perf(st)


def test_critic_state_unannotated_and_mixed():
    cell = {"cell_id": "D2", "demo_output": "x"}
    st = js.critic_state(cell, {}, None)
    assert st["奖励类型"] == st["触发点"] == st["缺口方向"] == st["产品角色"] == js.UNANNOTATED
    assert "按默认" in st["广告姿态"]
    st2 = js.critic_state(cell, {"advertising_stance": "mixed"}, {"rationale": "本方向走博主明推"})
    assert st2["方向说明"] == "本方向走博主明推"


def test_critic_subject_shape():
    s = js.critic_subject("run-1", "v1r2", _CELL, {}, None)
    assert s["subject_type"] == "ssll_sample"
    assert s["subject_id"] == "run-1:D1_xhs:demo:v1r2"
    assert s["qids"] == list(js.CRITIC_QIDS)


@pytest.mark.parametrize("vals,want", [
    ({"reward_signal": "pass", "interest_align": "pass", "gap_tension": "pass",
      "identity_consistency": "pass", "template_still_holds": "no"}, "pass"),
    ({"reward_signal": "pass", "interest_align": "weak", "gap_tension": "pass",
      "identity_consistency": "pass", "template_still_holds": "no"}, "borderline"),
    ({"reward_signal": "pass", "interest_align": "pass", "gap_tension": "pass",
      "identity_consistency": "pass", "template_still_holds": "partially"}, "borderline"),
    ({"reward_signal": "fail", "interest_align": "weak", "gap_tension": None,
      "identity_consistency": "pass", "template_still_holds": None}, "fail"),
    ({"reward_signal": "pass", "interest_align": "pass", "gap_tension": "pass",
      "identity_consistency": "pass", "template_still_holds": "yes"}, "fail"),
    ({"reward_signal": "pass", "interest_align": None, "gap_tension": "pass",
      "identity_consistency": "pass", "template_still_holds": "no"}, "undetermined"),
])
def test_gate_severity_follows_vibe_critic_rules(vals, want):
    assert js.gate_severity(vals) == want


def _item(ans, p=0.9, amb=False):
    return {"answer": ans, "p": p, "ambiguous": amb}


def test_summarize_critic_shadow_agreement_counts_only_confident():
    resp = {"status": "ok", "results": [{
        "subject_id": "run-1:D1_xhs:demo:v1r1",
        "items": {
            "reward_signal": _item("一眼可见且对得上"),
            "interest_align": _item("方向对但不够锐"),
            "gap_tension": _item("复现方法", p=0.5, amb=True),   # 歧义:不计入一致率
            "identity_consistency": _item("一致"),
            "template_still_holds": _item("说不清"),              # 出口:不映射
        }}]}
    main = [{"cell_id": "D1_xhs", "severity": "pass",
             "multiplier_gate": {"reward_signal": "pass", "interest_align": "pass",
                                 "gap_tension": "pass", "identity_consistency": "pass"},
             "template_test": {"still_holds": "no"}}]
    rep = js.summarize_critic_shadow(resp, run_id="run-1", round_tag="v1r1",
                                     cell_ids=["D1_xhs"], main_reviews=main, v4_reviews=None)
    cell = rep["cells"][0]
    assert cell["jev"]["items"]["gap_tension"]["value"] == "fail"
    assert cell["jev"]["items"]["gap_tension"]["ambiguous"] is True
    assert cell["jev"]["items"]["template_still_holds"]["value"] is None
    assert cell["jev"]["gate_severity"] == "borderline"       # weak,无把握的 fail 不算
    assert cell["agree_with_main"] == {"reward_signal": True, "interest_align": False,
                                       "gap_tension": None, "identity_consistency": True,
                                       "template_still_holds": None}
    agr = rep["agreement_with_main"]
    assert agr["reward_signal"] == {"n": 1, "agree": 1, "rate": 1.0}
    assert agr["gap_tension"]["n"] == 0
    assert rep["gate_severity_agreement_with_main"]["n"] == 1


def test_summarize_critic_shadow_subject_error():
    resp = {"status": "ok", "results": [{"subject_id": "run-1:D1_xhs:demo:v1r1", "error": "Jev 调用失败"}]}
    rep = js.summarize_critic_shadow(resp, run_id="run-1", round_tag="v1r1",
                                     cell_ids=["D1_xhs"], main_reviews=[], v4_reviews=None)
    assert rep["cells"][0]["jev"] == {"error": "Jev 调用失败"}


# ── ⑤ 画像第三路 ──────────────────────────────────────────────────────

def test_persona_subject_and_mapping():
    cell = {"cell_id": "D1_xhs", "platform": "小红书", "demo_output": "第一行\n第二行\n第三行\n第四行"}
    s = js.persona_subject("run-1", cell, "25岁宝妈")
    assert s["subject_id"] == "run-1:D1_xhs:persona"
    assert s["state"]["第一屏"] == "第一行\n第二行\n第三行"
    assert "25岁宝妈" in s["state"]["读者A"] and "读者C" in s["state"]
    _no_perf(s["state"])
    resp = {"status": "ok", "results": [{"subject_id": s["subject_id"], "items": {
        "persona_core_action": _item("划走"),
        "persona_edge_action": _item("划走", p=0.55, amb=True),
        "persona_anti_action": _item("说不清"),
    }}]}
    personas = js.personas_from_results(resp, [("D1_xhs", s)])
    assert [p["id"] for p in personas] == ["P_core_jev", "P_edge_jev", "P_anti_jev"]
    assert all(p["_source"] == "jev" for p in personas)
    acts = [p["reactions"][0]["action"] for p in personas]
    # 歧义的「划走」和出口选项都不算划走
    assert acts == ["skip", "unclear", "unclear"]


# ── ② 采样 ────────────────────────────────────────────────────────────

def _sampling():
    return {"status": "ok", "per_cell": [
        {"cell_id": "D1_xhs", "platform": "小红书", "per_sample": [
            {"idx": 0, "seed": 1, "body": "标题:半夜\n正文:第一段。\n第二段。\n#话题"},
            {"idx": 1, "seed": 21, "body": "另一篇"},
        ]},
    ]}


def test_sample_ids_have_generation_and_differ_across_resamples():
    a, b = _sampling(), _sampling()
    g1 = js.new_generation(datetime(2026, 9, 24, 8, 30, 15, tzinfo=timezone.utc))
    g2 = js.new_generation(datetime(2026, 9, 24, 9, 0, 0, tzinfo=timezone.utc))
    assert js.assign_sample_subject_ids(a, "run-1", g1) == 2
    js.assign_sample_subject_ids(b, "run-1", g2)
    ids_a = [ps["subject_id"] for _r, ps in js.iter_samples(a)]
    ids_b = [ps["subject_id"] for _r, ps in js.iter_samples(b)]
    assert ids_a == ["run-1:D1_xhs:g20260924T083015Z:1", "run-1:D1_xhs:g20260924T083015Z:21"]
    assert not set(ids_a) & set(ids_b)          # 同 seed 重采,账本主键不撞
    assert a["generation"] == g1


def test_sample_ids_skip_non_ok_and_bodyless():
    assert js.assign_sample_subject_ids({"status": "failed"}, "r", "g") == 0
    s = _sampling()
    s["per_cell"][0]["per_sample"][1]["body"] = ""
    js.assign_sample_subject_ids(s, "r", "g")
    assert len(list(js.iter_samples(s))) == 1


def test_sample_subjects_and_paragraphs():
    s = _sampling()
    js.assign_sample_subject_ids(s, "run-1", "g1")
    rep, ps = next(js.iter_samples(s))
    fq = js.sample_fq_subject(ps)
    assert fq == {"subject_type": "ssll_sample", "subject_id": "run-1:D1_xhs:g1:1",
                  "raw_content": ps["body"], "title_extraction": "markers"}
    paras = js.sample_para_subjects(ps, "小红书")
    # 标题行和「正文:」标记不算段,话题标签行也不算
    assert [p["subject_id"] for p in paras] == ["run-1:D1_xhs:g1:1:p1", "run-1:D1_xhs:g1:1:p2"]
    assert [p["state"]["段落"] for p in paras] == ["第一段。", "第二段。"]
    assert paras[1]["state"]["位置"] == "第 2 段，共 2 段"


def test_summarize_sample_shadow_distribution():
    s = _sampling()
    js.assign_sample_subject_ids(s, "run-1", "g1")
    fq = {"status": "ok", "results": [
        {"subject_id": "run-1:D1_xhs:g1:1", "items": {"opening_type": _item("具体事件"),
                                                      "efficacy_promise": {"answer": None, "invalid_reason": "text_too_short"}}},
        {"subject_id": "run-1:D1_xhs:g1:21", "items": {"opening_type": _item("具体事件")}},
    ]}
    hf = {"status": "ok", "results": [
        {"subject_id": "run-1:D1_xhs:g1:1:p1", "items": {"para_function": _item("讲事"), "para_friction": _item("是")}},
        {"subject_id": "run-1:D1_xhs:g1:1:p2", "items": {"para_function": _item("讲产品"), "para_friction": _item("否")}},
    ]}
    rep = js.summarize_sample_shadow(s, fq, hf)
    cell = rep["cells"][0]
    assert cell["judged"] == 2
    assert cell["fq_distribution"]["opening_type"] == {"具体事件": 2}
    assert cell["fq_distribution"]["efficacy_promise"] == {"（无效：text_too_short）": 1}
    hfs = cell["samples"][0]["human_feel"]
    assert hfs["paras"] == 2 and hfs["has_friction"] is True and hfs["first_product_para"] == 2


# ── ③ 预埋评论 ────────────────────────────────────────────────────────

def test_seed_labels_are_stripped_before_judging():
    assert js.strip_seed_label("(跨方向引流)楼主有没有研究过 X") == ("跨方向引流", "楼主有没有研究过 X")
    assert js.strip_seed_label("【同方向引导】这个我也试过") == ("同方向引导", "这个我也试过")
    assert js.strip_seed_label("没有标签") == ("", "没有标签")


def test_post_from_demo():
    assert js.post_from_demo("标题:半夜破防\n正文:我妈翻我包。") == {"title": "半夜破防", "body": "我妈翻我包。"}
    assert js.post_from_demo("没有标题的正文") == {"title": "", "body": "没有标题的正文"}


def test_comment_subjects_are_comment_type_with_all_placeholders():
    cell = {"cell_id": "D1_xhs", "demo_output": "开学第一天室友打开我抽屉。她以为那瓶是我姐的。结果是我自己买的。",
            "comment_seeds": ["(同方向引导)这个我也试过,第二天直接……", {"text": "楼主在哪买的"}, ""]}
    reader, thread = js.comment_subjects("run-1", cell)
    assert [s["subject_id"] for s in reader] == ["run-1:D1_xhs:seed1", "run-1:D1_xhs:seed2"]
    assert all(s["subject_type"] == "comment" for s in reader + [thread])
    assert reader[0]["state"]["评论原文"] == "这个我也试过,第二天直接……"   # 运营标签已剥
    # comment_reader_v0.4 / comment_thread_v0.3 的全部占位符都要填上(漏填服务端 422)
    for k in ("subject", "arranger", "decide", "post_points", "post_examples", "praised", "helper"):
        assert reader[0]["fill"].get(k), k
    assert "1. [读者]" in thread["state"]["评论列表"]
    _no_perf([reader, thread])


def test_comment_scoring():
    slot = js.DEFAULT_COMMENT_SLOTS[1]            # 补充经验
    good = {"speech_act": _item("补充经验"), "names_brand": _item("否"), "echoes_post": _item("是"),
            "detail_level": _item("具体"), "register": _item("随手口语"),
            "reader_value": _item("判断依据"), "arranged": _item("否")}
    bad = dict(good, speech_act=_item("亲历背书"), register=_item("文案腔"), arranged=_item("是"))
    s_good, hard_good, _ = js.score_comment(good, slot)
    s_bad, hard_bad, flags = js.score_comment(bad, slot)
    assert s_good == 0 and hard_good == []
    assert s_bad > s_good and {h[0] for h in hard_bad} == {"speech_act", "register"}
    assert "背书体" in flags and "像安排的" in flags
    assert js.thread_flags({"has_friction": _item("否"), "praise_share": _item("少数")}) == ["has_friction"]
