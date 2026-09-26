"""判定服务(judge / Jev)影子阶段的纯函数(v0.37.0)。

orchestrator 只管编排、记账和落库;这里全是无 IO 的纯函数 —— 拼 state /
subject、把 Jev 的闭集答案翻回三省六部的口径、做对照汇总。单测直接喂字典。

几条贯穿全文件的口径
--------------------
- **所有生成内容都按未发布稿发**:demo、采样稿用 `subject_type = "ssll_sample"`;
  预埋评论用 `comment`(评论题库的主体类型),但服务端的数据出境策略默认把
  `comment` 当公开内容放行 —— 所以 orchestrator 每个请求都带 `published: false`,
  外加 `project`(和能认出来的 `category`),处方药项目照样 403。
- **只有采样稿的 fq 判定写账本**(`write=true`)。demo / 画像 / 预埋评论的 id
  不是 TV 的笔记或评论 id,绝不进 `note_feature_answers`,结果只留在 stage_log
  和 critic_result 上。
- **state 里只放文字和策略锚点**,不放项目名、品牌名、任何表现类字段
  (Mode A,D-017 / D-028)。
- **Jev 只出事实,不出判决**。选项翻回 pass / weak / fail 之后,「任一 fail 即
  fail、任一 weak 最多 borderline」这条规则在这里只用来**算一个对照数**,
  不回写任何 critic 结果。
- **有歧义的答案不算数**:对照统计只收 Jev 自己有把握的格子;画像第三路里
  歧义答案一律不算「划走」(误报一票否决的代价是触发策略升级,极不对称)。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from pipeline.config import (
    JUDGE_CATEGORY_CLOSED_MARKERS,
    JUDGE_CATEGORY_EXCLUDE,
    JUDGE_CATEGORY_GENERAL_RULES,
    JUDGE_CATEGORY_RULES,
    JUDGE_PROJECT_CATEGORY_OVERRIDES,
    JUDGE_PROJECT_PREFIX,
    JUDGE_TV_CATEGORIES,
    JEV_SAMPLE_MAX_PARAS,
    JEV_SAMPLE_TITLE_EXTRACTION,
)

SUBJECT_TYPE = "ssll_sample"
COMMENT_SUBJECT_TYPE = "comment"
UNANNOTATED = "（未标注）"


# ══════════════════════════════════════════════════════════════════════
# 项目与品类(数据出境)
# ══════════════════════════════════════════════════════════════════════

def map_category(text: Any) -> str | None:
    """brief.product_category(自由文本)→ TV 统一词表。认不出返回 None ——
    调用方(orchestrator._jev_judge)见 None 就**不发**,见 config 里那段顺序说明。"""
    if isinstance(text, list):
        text = "、".join(str(t) for t in text)
    t = str(text or "").strip().lower()
    if not t:
        return None
    for word in JUDGE_CATEGORY_EXCLUDE:
        t = t.replace(word.lower(), "")
    for markers, label in JUDGE_CATEGORY_RULES:
        if any(m.lower() in t for m in markers):
            return label
    if any(m.lower() in t for m in JUDGE_CATEGORY_CLOSED_MARKERS):
        return None
    for markers, label in JUDGE_CATEGORY_GENERAL_RULES:
        if any(m.lower() in t for m in markers):
            return label
    return None


def judge_scope(project_id: str, brief: dict | None) -> dict[str, str | None]:
    """每个请求都带的 project / category。project 必发(未发布稿没有它服务端 422);
    category 为 None = 认不出品类,调用方一律不发(从严,codex review P1 on #53)。"""
    b = brief or {}
    override = JUDGE_PROJECT_CATEGORY_OVERRIDES.get(str(project_id))
    category = override if override in JUDGE_TV_CATEGORIES else \
        map_category(b.get("product_category") or b.get("category"))
    return {
        "project": f"{JUDGE_PROJECT_PREFIX}{project_id}",
        "category": category,
    }


# ══════════════════════════════════════════════════════════════════════
# 通用:结果解包
# ══════════════════════════════════════════════════════════════════════

def results_by_id(resp: dict | None) -> dict[str, dict]:
    """{subject_id: result}。单个 subject 失败时 result 是 {"error": ...}。"""
    out: dict[str, dict] = {}
    for r in (resp or {}).get("results") or []:
        if isinstance(r, dict) and r.get("subject_id"):
            out[str(r["subject_id"])] = r
    return out


def compact_item(it: dict | None) -> dict[str, Any]:
    it = it or {}
    out: dict[str, Any] = {"answer": it.get("answer"), "p": it.get("p")}
    if it.get("ambiguous"):
        out["ambiguous"] = True
    if it.get("invalid_reason"):
        out["invalid_reason"] = it.get("invalid_reason")
    return out


def resp_meta(resp: dict | None) -> dict[str, Any]:
    """一次 _jev_judge 的元信息(状态、用量、策略回显、失败块),给 stage_log 用。"""
    r = resp or {}
    meta: dict[str, Any] = {"status": r.get("status", "skipped"), "bank": r.get("bank")}
    if r.get("status") == "ok":
        meta.update({
            "usage": r.get("usage"),
            "cost_usd": round(float(r.get("cost_usd") or 0.0), 6),
            "subject_errors": int(r.get("errors") or 0),
            "written": r.get("written"),
            "policy": r.get("policy"),
            "requests": r.get("requests"),
            "elapsed_ms": r.get("elapsed_ms"),
        })
        if r.get("chunk_failures"):
            meta["chunk_failures"] = r["chunk_failures"]
    else:
        meta.update({"reason": r.get("reason"), "http_status": r.get("http_status"),
                     "detail": r.get("detail")})
    return meta


# ══════════════════════════════════════════════════════════════════════
# ① 网感二审影子(题库 ssll_critic_v0.1)
# ══════════════════════════════════════════════════════════════════════

GATE_QIDS: tuple[str, ...] = (
    "reward_signal", "interest_align", "gap_tension", "identity_consistency",
)
TEMPLATE_QID = "template_still_holds"
CRITIC_QIDS: tuple[str, ...] = GATE_QIDS + (TEMPLATE_QID,)

# Jev 选项 → vibe_critic.md 的口径。出口选项(说不清 / 锚点不可用)不在表里 → None。
GATE_LABELS: dict[str, dict[str, str]] = {
    "reward_signal": {"一眼可见且对得上": "pass", "读完才知道": "weak", "看不出或对不上": "fail"},
    "interest_align": {"激活了": "pass", "方向对但不够锐": "weak", "错位": "fail"},
    "gap_tension": {"事件本身": "pass", "方向模糊": "weak", "复现方法": "fail"},
    "identity_consistency": {"一致": "pass", "有点别扭": "weak", "冒充": "fail"},
}
TEMPLATE_LABELS: dict[str, str] = {"仍然成立": "yes", "部分成立": "partially", "不成立": "no"}

# vibe_critic.md 第 0.3 步 ④ 的四种姿态(185-191 行)。「合法 / 冒充」原样转述,
# Jev 偏字面,姿态说明不装进 state 它就只能按「stealth」的直觉判,会把明广告错杀。
STANCE_NOTES: dict[str, tuple[str, str]] = {
    "stealth": (
        "软植入",
        "合法身份：真素人（我是用户，我真的在用），或真情绪（我在表达感受，产品只是其中一个元素）。"
        "冒充：博主假装素人、营销假装路人、品牌假装第三方。",
    ),
    "disclosed_kol": (
        "博主明推",
        "合法身份：我是博主，这是我接的推广，但我觉得值得推。"
        "冒充：假装没收钱、假装素人、明明是品牌自述却冒充博主。",
    ),
    "brand_direct": (
        "品牌自述",
        "合法身份：我是品牌，不装，就是来介绍产品；博主明说在推销在这里也算一致。"
        "冒充：假装第三方、假装素人、假装博主。",
    ),
    "mixed": (
        "矩阵内多种共存",
        "按【方向说明】里本方向自己声明的姿态判：声明的是哪种，就按哪种的合法身份和冒充情形判。",
    ),
}
CRITIC_BODY_CHARS = 2000


def _text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, list):
        return "、".join(str(x) for x in v if str(x).strip())
    return str(v).strip()


def critic_state(cell: dict, brief_context: dict | None, direction: dict | None) -> dict[str, str]:
    """二审的 state:正文 + 主 critic 判决时对照的全部锚点。

    `cell` 是 vibe_loop 里 _enriched_cell 的产物(已带 reward_type / stop_trigger /
    gap_direction / product_role,来自 _direction_index 与 cell_plan)。
    """
    bc = brief_context or {}
    platform = _text(cell.get("platform")) or "小红书"
    stance_raw = _text(bc.get("advertising_stance"))
    key = stance_raw.lower()
    if not stance_raw:
        stance_label, stance_note = "软植入（brief 没填，按默认）", STANCE_NOTES["stealth"][1]
    elif key in STANCE_NOTES:
        stance_label, stance_note = STANCE_NOTES[key]
    else:
        stance_label, stance_note = stance_raw, "（不在已知的四种姿态里，按字面理解这个姿态该用什么身份说话）"
    st = {
        "说明": f"下面是一篇待发的{platform}内容（生成稿，还没有发布）和它的策略锚点。每道题只根据这些文字判断。",
        "平台": platform,
        "正文": (cell.get("demo_output") or "")[:CRITIC_BODY_CHARS],
        "奖励类型": _text(cell.get("reward_type")) or UNANNOTATED,
        "触发点": _text(cell.get("stop_trigger")) or UNANNOTATED,
        "缺口方向": _text(cell.get("gap_direction")) or UNANNOTATED,
        "广告姿态": stance_label,
        "姿态说明": stance_note,
        "产品角色": _text(cell.get("product_role")) or UNANNOTATED,
    }
    if key == "mixed":
        st["方向说明"] = _text((direction or {}).get("rationale"))[:300] or UNANNOTATED
    return st


def critic_subject_id(run_id: str, cell_id: str, round_tag: str) -> str:
    """round_tag = v<第几次进网感循环>r<第几轮>,例如 v1r2;策略升级重跑的那次是 v2r*。
    二审影子不写账本(demo 的 id 不是 TV 的笔记 id),这个 id 只用来在响应里对号。"""
    return f"{run_id}:{cell_id}:demo:{round_tag}"


def critic_subject(run_id: str, round_tag: str, cell: dict,
                   brief_context: dict | None, direction: dict | None) -> dict[str, Any]:
    return {
        "subject_type": SUBJECT_TYPE,
        "subject_id": critic_subject_id(run_id, cell.get("cell_id") or "?", round_tag),
        "state": critic_state(cell, brief_context, direction),
        "qids": list(CRITIC_QIDS),
    }


def _norm(v: Any, allowed: set[str]) -> str | None:
    s = str(v or "").strip().lower()
    return s if s in allowed else None


def main_gate(review: dict | None) -> dict[str, str | None]:
    """主 critic / v4-flash 二审的一条 cell_review → {四项, template_still_holds}。"""
    rv = review or {}
    mg = rv.get("multiplier_gate") or {}
    tt = rv.get("template_test") or {}
    out: dict[str, str | None] = {q: _norm(mg.get(q), {"pass", "weak", "fail"}) for q in GATE_QIDS}
    out[TEMPLATE_QID] = _norm(tt.get("still_holds"), {"yes", "partially", "no"})
    return out


def gate_severity(values: dict[str, str | None]) -> str:
    """vibe_critic.md 210-213 + 第 2.5 步:任一 fail(或模板仍成立)即 fail,任一 weak
    (或模板部分成立)最多 borderline,四项全 pass 且模板不成立才 pass;信息不全 = undetermined。
    注意这只是乘数门槛 + 模板这两层,主 critic 的 gut_call / taste_match 不在里面。"""
    gates = [values.get(q) for q in GATE_QIDS]
    th = values.get(TEMPLATE_QID)
    if "fail" in gates or th == "yes":
        return "fail"
    if "weak" in gates or th == "partially":
        return "borderline"
    if all(g == "pass" for g in gates) and th == "no":
        return "pass"
    return "undetermined"


def jev_gate(items: dict | None) -> tuple[dict[str, dict], dict[str, str | None]]:
    """Jev items → ({qid: {label, value, p, ambiguous, invalid_reason}}, {qid: 有把握时的 value})。"""
    detail: dict[str, dict] = {}
    confident: dict[str, str | None] = {}
    for q in CRITIC_QIDS:
        it = (items or {}).get(q) or {}
        label = it.get("answer")
        table = TEMPLATE_LABELS if q == TEMPLATE_QID else GATE_LABELS[q]
        value = table.get(label) if isinstance(label, str) else None
        d: dict[str, Any] = {"label": label, "value": value, "p": it.get("p")}
        if it.get("ambiguous"):
            d["ambiguous"] = True
        if it.get("invalid_reason"):
            d["invalid_reason"] = it.get("invalid_reason")
        detail[q] = d
        confident[q] = value if (value is not None and not it.get("ambiguous")) else None
    return detail, confident


def summarize_critic_shadow(
    resp: dict | None,
    *,
    run_id: str,
    round_tag: str,
    cell_ids: list[str],
    main_reviews: list[dict] | None,
    v4_reviews: list[dict] | None,
) -> dict[str, Any]:
    """一轮二审影子的汇总:逐 cell 三方(主 critic / v4-flash 二审 / Jev)对照 + 逐项一致率。"""
    by_main = {r.get("cell_id"): r for r in (main_reviews or []) if isinstance(r, dict)}
    by_v4 = {r.get("cell_id"): r for r in (v4_reviews or []) if isinstance(r, dict)}
    res_by = results_by_id(resp)
    agreement = {q: {"n": 0, "agree": 0} for q in CRITIC_QIDS}
    sev_agree = {"n": 0, "agree": 0}
    out_cells: list[dict] = []
    for cid in cell_ids:
        sid = critic_subject_id(run_id, cid or "?", round_tag)
        r = res_by.get(sid) or {}
        entry: dict[str, Any] = {"cell_id": cid, "subject_id": sid}
        mrv = by_main.get(cid)
        main_vals = main_gate(mrv) if mrv else None
        if mrv:
            entry["main"] = {**main_vals, "severity": _norm(mrv.get("severity"), {"pass", "borderline", "fail"}),
                             "gate_severity": gate_severity(main_vals)}
        vrv = by_v4.get(cid)
        if vrv:
            v4_vals = main_gate(vrv)
            entry["v4flash"] = {**v4_vals, "severity": _norm(vrv.get("severity"), {"pass", "borderline", "fail"}),
                                "gate_severity": gate_severity(v4_vals)}
        if r.get("error") or not r:
            entry["jev"] = {"error": r.get("error") or "没有返回这个 subject"}
            out_cells.append(entry)
            continue
        detail, confident = jev_gate(r.get("items"))
        entry["jev"] = {"items": detail, "gate_severity": gate_severity(confident)}
        if main_vals:
            agree_map: dict[str, bool | None] = {}
            for q in CRITIC_QIDS:
                jv, mv = confident.get(q), main_vals.get(q)
                if jv is None or mv is None:
                    agree_map[q] = None
                    continue
                agree_map[q] = jv == mv
                agreement[q]["n"] += 1
                agreement[q]["agree"] += int(jv == mv)
            entry["agree_with_main"] = agree_map
            js, ms = entry["jev"]["gate_severity"], entry["main"]["gate_severity"]
            if js != "undetermined" and ms != "undetermined":
                sev_agree["n"] += 1
                sev_agree["agree"] += int(js == ms)
        out_cells.append(entry)
    for q, a in agreement.items():
        a["rate"] = round(a["agree"] / a["n"], 3) if a["n"] else None
    sev_agree["rate"] = round(sev_agree["agree"] / sev_agree["n"], 3) if sev_agree["n"] else None
    return {
        "mode": "shadow",
        "round": round_tag,
        "cells": out_cells,
        "agreement_with_main": agreement,
        "gate_severity_agreement_with_main": sev_agree,
        "_note": ("影子期只记不分流:Jev 的结论不进 failed、不改 severity、不触发策略升级。"
                  "一致率只统计 Jev 有把握(非歧义、非出口选项)的格子。"),
    }


# ══════════════════════════════════════════════════════════════════════
# ⑤ 画像第三路(题库 ssll_critic_v0.1 的 persona_* 三题)
# ══════════════════════════════════════════════════════════════════════

# (题号, 画像 id, state 键, 画像说明模板)
PERSONA_ROUTE: tuple[tuple[str, str, str, str], ...] = (
    ("persona_core_action", "P_core_jev", "读者A",
     "核心目标读者：和「{ta}」完全吻合的人"),
    ("persona_edge_action", "P_edge_jev", "读者B",
     "边缘读者：只部分符合「{ta}」，这条可能会看也可能不会"),
    ("persona_anti_action", "P_anti_jev", "读者C",
     "反面读者：完全不是「{ta}」这类人，但会在{platform}上刷到这条"),
)
PERSONA_LABELS: dict[str, str] = {"点开": "click", "划走": "skip", "想留着": "save"}


def first_screen(text: str, max_lines: int = 3, max_chars: int = 200) -> str:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    return "\n".join(lines[:max_lines])[:max_chars]


PERSONA_TA_CHARS = 200


def persona_profiles(target_audience: str, platform: str) -> dict[str, str]:
    ta = _text(target_audience)[:PERSONA_TA_CHARS] or "brief 没写目标人群"
    return {state_key: tpl.format(ta=ta, platform=platform or "小红书")
            for _q, _pid, state_key, tpl in PERSONA_ROUTE}


def persona_subject(run_id: str, cell: dict, target_audience: str) -> dict[str, Any]:
    platform = _text(cell.get("platform")) or "小红书"
    st: dict[str, str] = {
        "说明": f"下面是一条{platform}内容的第一屏，和三位读者的画像。每道题只按题目指定的那位读者判断。",
        "平台": platform,
        "第一屏": first_screen(cell.get("demo_output") or "") or "（没有内容）",
    }
    st.update(persona_profiles(target_audience, platform))
    return {
        "subject_type": SUBJECT_TYPE,
        "subject_id": f"{run_id}:{cell.get('cell_id') or '?'}:persona",
        "state": st,
        "qids": [q for q, _pid, _k, _t in PERSONA_ROUTE],
    }


def personas_from_results(resp: dict | None, pairs: list[tuple[str, dict]]) -> list[dict]:
    """Jev 的答案 → 和 persona_simulator 同形的 3 个画像(每个画像对每个 cell 一条 reaction)。

    pairs = [(cell_id, 发出去的 subject)]。
    歧义 / 出口选项 / 漏答 → action = "unclear",不算划走(一票否决从严)。
    某个 cell 整条失败时这个 cell 没有 Jev 反应,凑不够 3 条就不参与否决。
    """
    res_by = results_by_id(resp)
    personas: dict[str, dict] = {}
    for q, pid, state_key, _tpl in PERSONA_ROUTE:
        personas[pid] = {"id": pid, "_source": "jev", "profile": "", "reactions": []}
    for cid, s in pairs:
        r = res_by.get(s["subject_id"]) or {}
        if not r or r.get("error"):
            continue
        items = r.get("items") or {}
        for q, pid, state_key, _tpl in PERSONA_ROUTE:
            if not personas[pid]["profile"]:
                personas[pid]["profile"] = s["state"].get(state_key, "")
            it = items.get(q) or {}
            label = it.get("answer")
            action = PERSONA_LABELS.get(label) if isinstance(label, str) else None
            if action is None or it.get("ambiguous"):
                action = "unclear"
            p = it.get("p")
            personas[pid]["reactions"].append({
                "cell_id": cid,
                "action": action,
                "reaction": f"（Jev）{label or '未作答'}",
                "reason": f"Jev 概率 {p}" + ("，有歧义，不计入否决" if it.get("ambiguous") else ""),
            })
    return [p for p in personas.values() if p["reactions"]]


# ══════════════════════════════════════════════════════════════════════
# ② 批量采样影子(fq 题库写账本 + 人感题库按段)
# ══════════════════════════════════════════════════════════════════════

def new_generation(now: datetime | None = None) -> str:
    """采样代际:一次采样一个值。全局修订 / cell 级修订会删掉 batch_sampling 日志、
    按**同一组 seed** 重采(seed = 1, 21, 41…),不带代际的话新旧两批正文对应同一个
    账本 subject_id,后写覆盖先写,旧 stage_log 又已删掉 —— 「回查到那篇采样」只对
    最后一次成立。UTC 时间戳足够区分同一个 run 里的各次采样(一次采样要几分钟)。"""
    return (now or datetime.now(timezone.utc)).strftime("g%Y%m%dT%H%M%SZ")


def sample_subject_id(run_id: str, cell_id: str, generation: str, seed: Any) -> str:
    return f"{run_id}:{cell_id}:{generation}:{seed}"


def assign_sample_subject_ids(sampling: dict | None, run_id: str, generation: str) -> int:
    """给采样报告里每一篇编账本 id(原地修改),返回编了几篇。

    run_id 不在 batch_sampler 的函数签名里 —— 采样模块不该知道 run 是谁,
    所以在 orchestrator 这一层拼。"""
    if not isinstance(sampling, dict) or sampling.get("status") != "ok":
        return 0
    sampling["generation"] = generation
    n = 0
    for rep in sampling.get("per_cell") or []:
        cid = rep.get("cell_id") or "?"
        for ps in rep.get("per_sample") or []:
            ps["subject_id"] = sample_subject_id(run_id, cid, generation, ps.get("seed"))
            n += 1
    return n


def iter_samples(sampling: dict | None):
    """(cell 报告, 单篇) —— 只给带正文和 id 的篇(老版本采样日志没有正文,跳过)。"""
    for rep in (sampling or {}).get("per_cell") or []:
        for ps in rep.get("per_sample") or []:
            if (ps.get("body") or "").strip() and ps.get("subject_id"):
                yield rep, ps


def sample_fq_subject(ps: dict) -> dict[str, Any]:
    return {
        "subject_type": SUBJECT_TYPE,
        "subject_id": ps["subject_id"],
        "raw_content": ps["body"],
        "title_extraction": JEV_SAMPLE_TITLE_EXTRACTION,
    }


def split_paragraphs(body: str, cap: int | None = None) -> list[str]:
    """同 judge.loop._para_split:按换行切段,话题标签行不算段。"""
    paras = [p.strip() for p in re.split(r"\n\s*\n|\n", body or "") if p.strip()]
    paras = [p for p in paras if not p.startswith(("#", "＃"))]
    return paras[: (cap if cap is not None else JEV_SAMPLE_MAX_PARAS)]


def sample_para_subjects(ps: dict, platform: str) -> list[dict[str, Any]]:
    """按段判人感。只切正文:「标题:…」「正文:…」这类标记先剥掉,标题不当一段。"""
    paras = split_paragraphs(post_from_demo(ps.get("body") or "")["body"])
    n = len(paras)
    return [
        {
            "subject_type": SUBJECT_TYPE,
            "subject_id": f"{ps['subject_id']}:p{i + 1}",
            "state": {
                "说明": f"下面是一篇{platform or '小红书'}稿子里的一段。只看这一段。",
                "段落": p,
                "位置": f"第 {i + 1} 段，共 {n} 段",
            },
        }
        for i, p in enumerate(paras)
    ]


def _para_stats(items_list: list[dict]) -> dict[str, Any]:
    """段级答案 → 篇级分布(同 judge.loop.para_stats 的几个数)。"""
    reg: dict[str, int] = {}
    fun: dict[str, int] = {}
    friction = close = adj = 0
    first_product = None
    for i, it in enumerate(items_list, 1):
        ans = {q: (v or {}).get("answer") for q, v in it.items()}
        r, f = str(ans.get("para_register")), str(ans.get("para_function"))
        reg[r] = reg.get(r, 0) + 1
        fun[f] = fun.get(f, 0) + 1
        friction += ans.get("para_friction") == "是"
        close += ans.get("para_summary_close") == "是"
        adj += ans.get("para_adjective_pile") == "是"
        if first_product is None and ans.get("para_function") == "讲产品":
            first_product = i
    n = len(items_list)
    return {"paras": n, "register": reg, "function": fun, "has_friction": friction > 0,
            "summary_close_paras": close, "adjective_pile_paras": adj,
            "first_product_para": first_product,
            "no_product_share": round(1 - fun.get("讲产品", 0) / n, 2) if n else None}


def summarize_sample_shadow(sampling: dict | None, fq_resp: dict | None,
                            hf_resp: dict | None) -> dict[str, Any]:
    """逐篇紧凑答案 + 逐 cell 的答案分布(每题每个答案几篇)。"""
    fq_by = results_by_id(fq_resp) if (fq_resp or {}).get("status") == "ok" else {}
    hf_by = results_by_id(hf_resp) if (hf_resp or {}).get("status") == "ok" else {}
    cells: dict[str, dict] = {}
    for rep, ps in iter_samples(sampling):
        cid = rep.get("cell_id") or "?"
        c = cells.setdefault(cid, {"cell_id": cid, "platform": rep.get("platform", ""),
                                   "samples": [], "fq_distribution": {}, "judged": 0})
        sid = ps["subject_id"]
        entry: dict[str, Any] = {"subject_id": sid, "seed": ps.get("seed")}
        r = fq_by.get(sid)
        if r and not r.get("error"):
            items = r.get("items") or {}
            entry["fq"] = {q: compact_item(it) for q, it in items.items()}
            c["judged"] += 1
            for q, it in items.items():
                if it.get("invalid_reason") or it.get("answer") is None:
                    key = f"（无效：{it.get('invalid_reason') or '未作答'}）"
                else:
                    key = str(it.get("answer"))
                dist = c["fq_distribution"].setdefault(q, {})
                dist[key] = dist.get(key, 0) + 1
        elif r:
            entry["fq_error"] = r.get("error")
        if hf_by:
            paras = [hf_by.get(f"{sid}:p{i}") for i in range(1, JEV_SAMPLE_MAX_PARAS + 1)]
            paras = [p for p in paras if p and not p.get("error")]
            if paras:
                entry["human_feel"] = _para_stats([p.get("items") or {} for p in paras])
        c["samples"].append(entry)
    return {"mode": "shadow", "generation": (sampling or {}).get("generation"),
            "cells": list(cells.values())}


# ══════════════════════════════════════════════════════════════════════
# ③④ 预埋评论(读者侧 comment_reader_v0.4 · 评论区 comment_thread_v0.3)
# ══════════════════════════════════════════════════════════════════════

# 三省六部推的是产品:占位符按 judge.comments.PRODUCT_FILL 填,不给产品名(D-079)。
PRODUCT_FILL: dict[str, str] = {
    "subject": "这个产品", "arranger": "商家", "decide": "买",
    "praised": "这个产品", "helper": "这个产品",
}
# 同 judge.spans.split_sentences 的切法
_SENT_SPLIT = re.compile(r"(?<=[。！？!?；;\n])")
_WS = re.compile(r"\s+")
# 工部·构建给预埋评论加的运营标签,例如「(同方向引导)」「【跨方向引流】」。
# 送判前剥掉:对抗性的题(像不像安排的)看到这种标签会被带偏(judge 仓 docs/02 §4 #14)。
_SEED_LABEL = re.compile(r"^\s*[（(【\[]([^）)】\]\n]{1,12})[）)】\]]\s*")
_TITLE_MARK = re.compile(r"^\s*(?:【\s*标\s*题\s*】|标题\s*[：:])\s*(.+?)\s*(?:\n|$)")
_BODY_MARK = re.compile(r"^\s*(?:【\s*正\s*文\s*】|正文\s*[：:])\s*")


def split_sentences(text: str, cap: int = 120) -> list[str]:
    parts = [p.strip() for p in _SENT_SPLIT.split(text or "") if p and p.strip()]
    out: list[str] = []
    for p in parts:
        if out and len(_WS.sub("", p)) < 6:
            out[-1] = out[-1] + p
        else:
            out.append(p)
    return out[:cap]


def post_from_demo(demo: str) -> dict[str, str]:
    """demo_output → {title, body}。开头带「标题:」/「【标题】」才切,否则没有标题。"""
    text = (demo or "").strip()
    m = _TITLE_MARK.match(text)
    if not m:
        return {"title": "", "body": text}
    body = _BODY_MARK.sub("", text[m.end():].lstrip("\n"), count=1)
    return {"title": m.group(1).strip(), "body": body.strip()}


def post_points(body: str, n: int = 6, max_chars: int = 24) -> list[str]:
    """同 judge.comments.post_points:从正文取要点,给 echoes_post 那题当对照。"""
    pts: list[str] = []
    for s in split_sentences(body or ""):
        s = re.sub(r"[\s。！？!?，,；;：:\"“”]+$", "", s.strip())
        s = re.sub(r"^[\s#＃]+", "", s)
        if len(s) < 6:
            continue
        pts.append(s[:max_chars])
        if len(pts) >= n:
            break
    return pts


def comment_fill(post: dict) -> dict[str, str]:
    fill = dict(PRODUCT_FILL)
    pts = post_points(post.get("body", ""))
    fill["post_points"] = "、".join(pts) if pts else "（正文太短，没有可接的要点）"
    fill["post_examples"] = "".join(f"「{p}」" for p in pts[:3]) if pts else "「…」"
    return fill


def _post_noun(platform: str) -> str:
    """state 里怎么称呼这条内容。小红书沿用 judge.comments 的原话(「一篇小红书帖子」),
    别的平台按 cell 自己的平台说(codex review P2 on #53:以前一律说成小红书帖子,
    抖音 / 微博 / B站的预埋评论是在错的平台语境下判的)。"""
    p = (platform or "").strip() or "小红书"
    return "一篇小红书帖子" if p == "小红书" else f"一条{p}内容"


def comment_state(post: dict, text: str, *, role: str = "", reply_to_text: str = "",
                  platform: str = "小红书") -> dict[str, str]:
    """同 judge.comments.comment_state:帖子 + 这一条评论。platform = cell 的平台。"""
    st = {"说明": f"以下是{_post_noun(platform)}和它下面的一条候选评论。只根据帖子和这条评论判断。",
          "帖子标题": post.get("title") or "（无标题）", "帖子正文": (post.get("body") or "")[:600]}
    if reply_to_text:
        st["回复对象"] = reply_to_text
    if role:
        st["评论角色"] = role
    st["评论原文"] = text
    return st


def thread_state(post: dict, comments: list[dict], *, platform: str = "小红书") -> dict[str, str]:
    """同 judge.comments.thread_state:帖子 + 能看到的全部评论。platform = cell 的平台。"""
    lines = []
    for i, c in enumerate(comments, 1):
        who = c.get("role") or "读者"
        rep = f"（回复「{c['reply_to_text'][:20]}」）" if c.get("reply_to_text") else ""
        lines.append(f"{i}. [{who}]{rep} {c['text']}")
    return {"说明": f"以下是{_post_noun(platform)}和它下面能看到的全部评论。只根据这些文字判断评论区整体。",
            "帖子标题": post.get("title") or "（无标题）", "帖子正文": (post.get("body") or "")[:600],
            "评论列表": "\n".join(lines)}


def cell_platform(cell: dict) -> str:
    return _text((cell or {}).get("platform")) or "小红书"


def strip_seed_label(text: str) -> tuple[str, str]:
    m = _SEED_LABEL.match(text or "")
    if not m:
        return "", (text or "").strip()
    return m.group(1).strip(), (text or "")[m.end():].strip()


def seed_texts(cell: dict) -> list[tuple[str, str]]:
    """cell.comment_seeds → [(运营标签, 评论正文)]。兼容 dict 形态({text|content})。"""
    out: list[tuple[str, str]] = []
    for s in cell.get("comment_seeds") or []:
        raw = s if isinstance(s, str) else (s.get("text") or s.get("content") or "") if isinstance(s, dict) else ""
        label, text = strip_seed_label(str(raw))
        if text:
            out.append((label, text))
    return out


def comment_subjects(run_id: str, cell: dict) -> tuple[list[dict], dict | None]:
    """(逐条读者侧 subjects, 整组评论区 subject)。subject_type = comment,
    调用方必须带 published=False(见模块头)。"""
    cid = cell.get("cell_id") or "?"
    seeds = seed_texts(cell)
    if not seeds:
        return [], None
    post = post_from_demo(cell.get("demo_output") or "")
    fill = comment_fill(post)
    platform = cell_platform(cell)
    reader = [
        {"subject_type": COMMENT_SUBJECT_TYPE, "subject_id": f"{run_id}:{cid}:seed{i + 1}",
         "state": comment_state(post, text, platform=platform), "fill": fill}
        for i, (_label, text) in enumerate(seeds)
    ]
    thread = {"subject_type": COMMENT_SUBJECT_TYPE, "subject_id": f"{run_id}:{cid}:seeds",
              "state": thread_state(post, [{"text": t} for _l, t in seeds], platform=platform),
              "fill": fill}
    return reader, thread


READER_FLAGS = (
    ("speech_act", ("亲历背书", "旁观推荐"), "背书体"),
    ("register", ("文案腔",), "文案腔"),
    ("reader_value", ("只有评价",), "只有评价"),
    ("arranged", ("是",), "像安排的"),
)
THREAD_FAILS = (
    ("praise_share", ("多数",)),
    ("same_template", ("是",)),
    ("has_friction", ("否",)),
    ("thread_arranged", ("是",)),
)


def reader_flags(items: dict) -> list[str]:
    flags = []
    for q, bad, name in READER_FLAGS:
        if (items.get(q) or {}).get("answer") in bad:
            flags.append(name)
    return flags


def thread_flags(items: dict) -> list[str]:
    """评论区四题「不过就换位」的口径(docs/01 §4;摩擦那题也算)。影子期只记。"""
    return [q for q, bad in THREAD_FAILS if (items.get(q) or {}).get("answer") in bad]


def summarize_comment_shadow(cells: list[dict], run_id: str, reader_resp: dict | None,
                             thread_resp: dict | None) -> dict[str, Any]:
    rb = results_by_id(reader_resp) if (reader_resp or {}).get("status") == "ok" else {}
    tb = results_by_id(thread_resp) if (thread_resp or {}).get("status") == "ok" else {}
    out_cells = []
    tally: dict[str, int] = {}
    for cell in cells:
        cid = cell.get("cell_id") or "?"
        seeds = seed_texts(cell)
        if not seeds:
            continue
        entry: dict[str, Any] = {"cell_id": cid, "seeds": []}
        for i, (label, text) in enumerate(seeds):
            r = rb.get(f"{run_id}:{cid}:seed{i + 1}") or {}
            s: dict[str, Any] = {"text": text}
            if label:
                s["label"] = label
            if r.get("error"):
                s["error"] = r["error"]
            elif r:
                items = r.get("items") or {}
                s["items"] = {q: compact_item(it) for q, it in items.items()}
                s["flags"] = reader_flags(items)
                for f in s["flags"]:
                    tally[f] = tally.get(f, 0) + 1
            entry["seeds"].append(s)
        t = tb.get(f"{run_id}:{cid}:seeds") or {}
        if t.get("error"):
            entry["thread"] = {"error": t["error"]}
        elif t:
            items = t.get("items") or {}
            entry["thread"] = {"items": {q: compact_item(it) for q, it in items.items()},
                               "would_swap": thread_flags(items)}
        out_cells.append(entry)
    return {"mode": "shadow", "cells": out_cells, "reader_flag_tally": tally}


# ── ④ 重生成(默认关):评论位、生成 prompt、候选打分 ──────────────────

@dataclass
class CommentSlot:
    """一个评论位 = 这条评论的目标画像(同 judge.comments.Slot 的默认组)。"""
    id: str
    speech_act: tuple[str, ...]
    ask: str                               # 给生成端的一句话要求
    role: str = "读者"
    reply_to: str | None = None
    may_name_brand: bool = False
    must_echo: bool = True
    value_ok: tuple[str, ...] = ("可行动信息", "判断依据")
    min_detail: str = "模糊"


# 默认组没有「亲历背书 / 旁观推荐」位:评论区的营销职能是承接不是背书(judge docs/31 §6.3)。
DEFAULT_COMMENT_SLOTS: tuple[CommentSlot, ...] = (
    CommentSlot("q1", ("提问",), "问一件笔记里没写清楚的事", must_echo=False,
                value_ok=("可行动信息", "判断依据", "无"), min_detail="无"),
    CommentSlot("exp", ("补充经验",), "讲一段自己相关的经历，不对产品下结论"),
    CommentSlot("reply", ("回复答疑",), "以笔记作者的身份回答第一条评论的提问，给能用的信息",
                role="笔记作者", reply_to="q1", may_name_brand=True, must_echo=False,
                value_ok=("可行动信息",), min_detail="无"),
)
_DETAIL_RANK = {"无": 0, "模糊": 1, "具体": 2}
COMMENT_SP_CHARS = 1500


def comment_generation_prompt(cell: dict, post: dict, slot: CommentSlot,
                              reply_to_text: str = "") -> tuple[str, str]:
    """(system, user)。人设与口吻取自该 cell 的 system_prompt(节选)。"""
    platform = _text(cell.get("platform")) or "小红书"
    system = (f"你是{platform}上的真实用户，只写一条评论。像刷手机时随手打的：口语、短、"
              "有自己的情境，不写文案腔，不堆形容词，不加运营标签，不解释。")
    rules = [
        f"这条评论的身份：{slot.role}",
        f"这条评论要做的事：{slot.ask}",
        "可以写出品牌或产品的名字" if slot.may_name_brand else "不要写出品牌或产品的名字",
        "要接住笔记里具体说到的某件事" if slot.must_echo else "不必复述笔记内容",
        "40 字以内",
    ]
    if reply_to_text:
        rules.insert(2, f"回复的是这一条评论：「{reply_to_text}」")
    user = (
        f"下面是一条{platform}笔记的写作设定（节选，人设和口吻以它为准）和按它写出来的笔记。\n\n"
        f"【写作设定（节选）】\n{(cell.get('system_prompt') or '')[:COMMENT_SP_CHARS]}\n\n"
        f"【笔记】\n{((post.get('title') + chr(10)) if post.get('title') else '')}{(post.get('body') or '')[:800]}\n\n"
        "【这一条评论】\n" + "\n".join(f"- {r}" for r in rules)
        + "\n\n只输出评论原文。"
    )
    return system, user


def clean_generated_comment(text: str) -> str:
    t = (text or "").strip().strip("「」\"'“”")
    t = t.splitlines()[0].strip() if t else ""
    return strip_seed_label(t)[1]


def score_comment(items: dict, slot: CommentSlot) -> tuple[float, list[tuple], list[str]]:
    """越小越好(同 judge.comments.score_comment):硬伤每条 100,「像安排的」10,歧义每处 1。
    没判出来的题不参与硬伤(同 judge 当前口径)。"""
    a = {q: (it or {}).get("answer") for q, it in (items or {}).items()}
    hard: list[tuple] = []
    if a.get("speech_act") is not None and a.get("speech_act") not in slot.speech_act:
        hard.append(("speech_act", a.get("speech_act"), "/".join(slot.speech_act)))
    if a.get("names_brand") == "是" and not slot.may_name_brand:
        hard.append(("names_brand", "是", "否"))
    if slot.must_echo and a.get("echoes_post") == "否":
        hard.append(("echoes_post", "否", "是"))
    if a.get("register") == "文案腔":
        hard.append(("register", "文案腔", "随手口语/认真分享"))
    if a.get("reader_value") is not None and a.get("reader_value") not in slot.value_ok:
        hard.append(("reader_value", a.get("reader_value"), "/".join(slot.value_ok)))
    if a.get("detail_level") is not None and \
            _DETAIL_RANK.get(a.get("detail_level"), 0) < _DETAIL_RANK.get(slot.min_detail, 0):
        hard.append(("detail_level", a.get("detail_level"), f"至少{slot.min_detail}"))
    flags = reader_flags(items or {})
    amb = sum(1 for it in (items or {}).values() if (it or {}).get("ambiguous"))
    score = 100.0 * len(hard) + 10.0 * ("像安排的" in flags) + 1.0 * amb
    return score, hard, flags
