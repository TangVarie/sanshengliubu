"""产出中心 — View and export the generated prompt system."""

import json

import streamlit as st
from db.supabase_client import SupabaseClient
from utils.export import export_as_markdown, export_as_json
from utils.version_badge import show_version_badge

st.set_page_config(page_title="产出中心", page_icon="省", layout="wide")
show_version_badge()
st.title("产出中心")

project_id = st.session_state.get("current_project_id") or st.query_params.get("project_id")
if project_id:
    st.query_params["project_id"] = project_id

try:
    db = SupabaseClient.get_instance()
except Exception as e:
    st.error(f"无法连接数据库：{e}")
    st.stop()

# If no project specified, show project selector
if not project_id:
    projects = db.list_projects(limit=50)
    # needs_revision 的项目也已有(待修订的)产出,一并可选(COR-006)。
    completed = [
        p for p in projects if p["status"] in ("completed", "needs_revision")
    ]
    if not completed:
        st.info("暂无已完成的项目。")
        st.stop()

    selected = st.selectbox(
        "选择项目",
        completed,
        format_func=lambda p: f"{p['name']} ({p['created_at'][:10]})",
    )
    project_id = selected["id"]

# Load output
project = db.get_project(project_id)
output_data = db.get_latest_output_for_project(project_id)

if not output_data:
    st.warning("该项目尚无产出。请等待流水线完成。")
    st.stop()

prompt_system = output_data.get("prompt_system", {})
final_review = output_data.get("final_review", {})

st.subheader(f"{project['name']}")

# 审计 COR-006:needs_revision 的 run 也保存了(待修订的)产出,现在能在
# 这里看到 —— 但必须明确标注,避免把待修订稿当已批准的终稿直接交付。
if output_data.get("_run_status") == "needs_revision":
    st.warning(
        "⚠️ 当前展示的是**待修订版本**:终审未通过,产出已保存供检查,"
        "但尚未按终审意见修订。可回到「流水线详情」页应用修订意见后重跑。"
    )

# v0.30.0: 成品清单(顶层主视图)— 用户反馈"看不懂分类",这一块用最简
# 形式回答"产出了什么":几个不重复的 prompt + 每个的内容 + 示例输出。
# 详细矩阵 / 诊断信息保留在下方。
_matrix_for_summary = (
    prompt_system.get("prompt_matrix")
    or prompt_system.get("prompt_templates")
    or []
)
if _matrix_for_summary:
    # 去重(同 cell_id 只算一次)+ 按 cell_id 排序方便对照
    _seen_ids: set[str] = set()
    _unique_cells: list[dict] = []
    for _c in _matrix_for_summary:
        _cid = _c.get("cell_id", "")
        if _cid and _cid not in _seen_ids:
            _seen_ids.add(_cid)
            _unique_cells.append(_c)
    _unique_cells.sort(key=lambda c: c.get("cell_id", ""))

    st.markdown(
        f"### 成品提示词清单 · 共 **{len(_unique_cells)}** 个不重复的提示词"
    )
    st.caption(
        "每条 prompt 自带完整 system_prompt + user_prompt_template + 一段示例文稿,"
        "复制 system_prompt 装载一次,按 user template 填变量就能直接用。"
        "下方『Prompt 矩阵』按平台 × 方向展示同样的内容 + 对标参考帖子;"
        "再下方『诊断详情』是网感复检 / 红蓝精炼 / 画像模拟等中间过程,"
        "通常不需要看。"
    )

    # 跨批次多样性的诚实交底。参考方法论里这条是明确要求的:不能让运营
    # 以为这是终极方案,否则跑到第 10 批发现重复率高会以为是 prompt 写坏了。
    with st.expander("连着跑多批 / 多账号时怎么降重复（必读一次）", expanded=False):
        st.markdown(
            """
每条 prompt 的**开头切入池**带了 15 个编号角度（`C01`-`C15`），批量生成时会自动
覆盖 ≥8 个不同编号、同编号一批内不超过 2 次，并在每篇开头标出本篇用的编号。

**连着跑多批**：把上一批用过的编号填进 `used_angles` 变量（如 `C03 C07 C11`），
这一批就会避开它们。不填也能跑，只是不启用回避。

**多账号同时发**：给每个账号填一个不同的 `account_profile`（如
`偏好 C01/C04/C12，语言偏短句，声调散淡`），批次内 ≥60% 的篇目会向该档案倾斜，
从根部分岔，避免"5 个账号发出来像一个人写的"。

---

⚠️ **这是补偿方案，不是根治。**

大模型是无状态的——它不知道你昨天用同一条 prompt 生成过什么，也不知道别的账号
生成过什么。提示词层能做的只有「你手动告诉它上批用了什么，它避开」。

真正的跨批次去重需要工作台做三件事：维护已生产资产库、每次生成自动注入避重池、
账号间自动分配差异化档案。这三件事没做之前，跑到第 20-30 批仍然会开始撞车——
届时该推的是工作台改造，不是继续改 prompt。
            """.strip()
        )

    for _idx, _c in enumerate(_unique_cells, 1):
        _cid = _c.get("cell_id", "?")
        _dname = _c.get("direction_name", "")
        _platform = _c.get("platform", "")
        _label = (
            f"**#{_idx}** · `{_cid}` · {_dname or '(无方向名)'}"
            f" · 平台:{_platform}"
        )
        with st.expander(_label, expanded=(_idx == 1)):
            _sp = _c.get("system_prompt", "") or ""
            _up = _c.get("user_prompt_template", "") or ""
            _vars = _c.get("variables", {}) or {}
            _demo = _c.get("demo_output", "") or ""

            st.markdown(
                f"**system_prompt** _{len(_sp):,} 字_"
            )
            st.code(_sp, language="markdown")

            st.markdown(
                f"**user_prompt_template** _{len(_up):,} 字_"
            )
            st.code(_up, language="markdown")

            if _vars:
                st.markdown("**变量列表**")
                for _vn, _vd in _vars.items():
                    st.markdown(f"- `{{{{{_vn}}}}}`: {_vd}")

            if _demo:
                st.markdown("**示例文稿(用上面这条 prompt 跑出来的)**")
                # 用 success-styled markdown 区分,避免和 prompt 代码混淆
                st.markdown(
                    f"<div style='background:#f0f9f0;padding:12px 16px;"
                    f"border-left:3px solid #4caf50;border-radius:4px;"
                    f"white-space:pre-wrap;font-family:system-ui;"
                    f"line-height:1.7'>{_demo}</div>",
                    unsafe_allow_html=True,
                )
            else:
                st.caption("(没有示例文稿 — 流水线被中断或这条 cell 走的是不带 demo 的旧路径)")

    st.divider()

# Final review summary
if final_review:
    verdict = final_review.get("verdict", "unknown")
    if verdict == "approved":
        st.success("终审通过")
    else:
        st.warning(f"终审状态：{verdict}")

# Display prompt matrix (platform → direction).
# Old runs (pre-v0.6.1) had a duplicate `prompt_templates` field that was
# identical to `prompt_matrix` — fall back to it for backwards compatibility.
matrix = prompt_system.get("prompt_matrix", []) or prompt_system.get("prompt_templates", [])
templates = prompt_system.get("prompt_templates", []) if not matrix else []

if matrix:
    st.subheader("Prompt 矩阵")
    st.caption("每个格子是一个**自包含、可复制粘贴**的完整 system prompt——所有人设规则、防批量感、合规约束都已经内置。复制 system prompt 装载一次，按 user template 填变量即可。")
    platforms = list(dict.fromkeys(c.get("platform", "") for c in matrix))
    platform_tabs = st.tabs(platforms)

    for p_idx, platform in enumerate(platforms):
        with platform_tabs[p_idx]:
            cells = [c for c in matrix if c.get("platform") == platform]
            for cell in cells:
                label = f"{cell.get('direction_id', '')}: {cell.get('direction_name', '')}"
                with st.expander(label, expanded=False):
                    st.markdown("**System Prompt**（复制即可使用）")
                    st.code(cell.get("system_prompt", ""), language="markdown")

                    st.markdown("**User Prompt Template**")
                    st.code(cell.get("user_prompt_template", ""), language="markdown")

                    variables = cell.get("variables", {})
                    if variables:
                        st.markdown("**变量说明**")
                        for var_name, var_desc in variables.items():
                            st.markdown(f"- `{{{{{var_name}}}}}`: {var_desc}")

                    demo = cell.get("demo_output", "")
                    if demo:
                        st.markdown("**示例输出**")
                        st.markdown(demo)

                    rewrite_summary = cell.get("rewrite_summary")
                    if rewrite_summary:
                        st.info(f"网感重写：{rewrite_summary}")

                    # v0.29.2: 红蓝精炼 per-cell 状态 — 即使没改动也显示,
                    # 让用户能区分"跑了没改" vs "跑了失败" vs "没跑"
                    rb_status = cell.get("_red_blue_status")
                    rb_summary = cell.get("_red_blue_summary", "")
                    if rb_status:
                        _rb_icon = {
                            "refined": "[已精炼]",
                            "unchanged": "[无需修改]",
                            "failed": "[失败]",
                        }.get(rb_status, "[已精炼]")
                        _rb_func = {
                            "refined": st.success,
                            "unchanged": st.caption,
                            "failed": st.error,
                        }.get(rb_status, st.info)
                        _rb_func(f"{_rb_icon} 红蓝精炼 [{rb_status}]: {rb_summary}")

                    # A2: per-direction reference posts (Gemini pulled
                    # real current Xiaohongshu content with same direction
                    # theme). Side-by-side comparison anchor — did we
                    # hit the right vibe or not?
                    _refs = (prompt_system.get("_per_direction_references") or {})
                    _cell_refs = _refs.get(cell.get("direction_id", ""), {})
                    _ref_posts = _cell_refs.get("posts") or []
                    if _ref_posts:
                        st.markdown(
                            "**对标参考**（Gemini 搜到的当前小红书真实同方向帖子）"
                        )
                        st.caption(
                            "对比我们的 demo 跟下面这些真人帖子的第一句/场景感——"
                            "如果差距很大，考虑点「应用修订意见」重跑工部。"
                        )
                        for rp in _ref_posts:
                            title = rp.get("title") or "(无标题)"
                            snippet = rp.get("snippet") or ""
                            url = rp.get("url", "")
                            flag = " 疑似分析文" if rp.get("_suspect_analysis") else ""
                            st.markdown(f"- **《{title}》**{flag}")
                            if snippet:
                                st.caption(snippet)
                            if url:
                                st.caption(f"→ [{url}]({url})")

elif templates:
    # Legacy: direction-based display for old project data
    st.subheader("Prompt 模板")
    direction_tabs = st.tabs([t.get("direction_name", f"方向{i+1}") for i, t in enumerate(templates)])

    for i, template in enumerate(templates):
        with direction_tabs[i]:
            st.markdown(f"**方向 ID**: {template.get('direction_id', '')}")

            with st.expander("System Prompt", expanded=True):
                st.code(template.get("system_prompt", ""), language=None)

            with st.expander("User Prompt Template"):
                st.code(template.get("user_prompt_template", ""), language=None)

            variables = template.get("variables", {})
            if variables:
                with st.expander("变量说明"):
                    for var_name, var_desc in variables.items():
                        st.markdown(f"- `{{{{{var_name}}}}}`: {var_desc}")

            demo = template.get("demo_output", "")
            if demo:
                with st.expander("示例输出"):
                    st.markdown(demo)

# v0.29.2: 红蓝精炼总计 — 即使整批失败/全部 unchanged 也会显示,
# 和之前"啥都没有"的行为对齐不上,让用户能看到阶段真的跑了。
rb_stats = prompt_system.get("_red_blue_stats")
if rb_stats:
    attempted = rb_stats.get("attempted", 0)
    refined = rb_stats.get("refined", 0)
    unchanged = rb_stats.get("unchanged", 0)
    failed = rb_stats.get("failed", 0)
    note = rb_stats.get("note", "")
    label = (
        f"红蓝精炼: {refined} 已修复 · {unchanged} 无需修改 · "
        f"{failed} 失败 / 总计 {attempted}"
    )
    if failed > 0 and failed == attempted:
        st.error(f"{label} — 全部失败,请检查 stage_log 里的 red_blue_refiner 报错")
    elif failed > 0:
        st.warning(label)
    elif refined == 0 and attempted > 0:
        st.info(f"{label} — 这轮所有 cell 都通过 Red Team 检查,无需精修")
    elif attempted == 0:
        st.caption(f"红蓝精炼: {note or '未执行'}")
    else:
        st.success(label)

    details = rb_stats.get("details") or []
    if details:
        with st.expander(
            f"展开红蓝精炼详情({len(details)} 条 cell 的攻击 / 修复清单)",
            expanded=False,
        ):
            for d in details:
                cid = d.get("cell_id", "?")
                status = d.get("status", "?")
                st.markdown(
                    f"**{cid}** · `{status}` — {d.get('summary', '')}"
                )
                attacks = d.get("attacks") or []
                fixes = d.get("fixes") or []
                if attacks:
                    st.caption("Red Team 攻击:")
                    for a in attacks:
                        st.markdown(
                            f"- [{a.get('severity', '?')}] "
                            f"`{a.get('target', '')[:80]}` — {a.get('issue', '')}"
                        )
                if fixes:
                    st.caption("Blue Team 修复:")
                    for fx in fixes:
                        st.markdown(
                            f"- `{fx.get('original', '')[:60]}` → "
                            f"**{fx.get('replacement', '')[:100]}**"
                        )
                if d.get("refined_demo_output"):
                    with st.expander(f"精修后的 demo ({cid})", expanded=False):
                        st.markdown(d["refined_demo_output"])
                st.divider()

# v0.29.2: 画像模拟反应(3 个画像对每个 cell 的 click/skip/save 反应)
persona_reactions = prompt_system.get("_persona_reactions")
if persona_reactions:
    ps_status = persona_reactions.get("status", "ok")
    if ps_status == "failed":
        st.error(
            f"画像模拟: 调用失败 — {persona_reactions.get('error', '未知错误')}"
        )
        if persona_reactions.get("reason"):
            st.caption(persona_reactions["reason"])
    elif ps_status == "skipped":
        st.caption(
            f"画像模拟: 跳过 — {persona_reactions.get('reason', '')}"
        )
    else:
        summary = persona_reactions.get("summary", {}) or {}
        strong = summary.get("strong_cells", []) or []
        narrow = summary.get("narrow_cells", []) or []
        weak = summary.get("weak_cells", []) or []
        label = (
            f"画像模拟: {len(strong)} 强 · {len(narrow)} 窄 · {len(weak)} 弱"
        )
        if weak:
            st.error(f"{label} — 弱 cell (3 个画像都划走): {weak}")
        elif narrow:
            st.warning(label)
        else:
            st.success(label)

        overall = summary.get("overall", "")
        if overall:
            st.caption(f"{overall}")

        personas = persona_reactions.get("personas") or []
        if personas:
            with st.expander(
                f"展开 {len(personas)} 个画像对每个 cell 的反应",
                expanded=False,
            ):
                for p in personas:
                    pid = p.get("id", "?")
                    profile = p.get("profile", "")
                    st.markdown(f"### {pid}")
                    st.caption(profile)
                    for r in p.get("reactions", []) or []:
                        action = r.get("action", "?")
                        _act_icon = {
                            "click": "[点击]", "save": "[收藏]", "skip": "[划走]",
                        }.get(action, "[未知]")
                        st.markdown(
                            f"- {_act_icon} **{r.get('cell_id', '?')}** · "
                            f"`{action}` — "
                            f"*「{r.get('reaction', '')}」* "
                            f"({r.get('reason', '')})"
                        )
                    st.divider()

# Vibe critic summary
vibe_result = prompt_system.get("vibe_critic_result")
if vibe_result:
    verdict = vibe_result.get("verdict", "")
    summary = vibe_result.get("summary", "")
    if verdict == "all_pass":
        st.success(f"网感复检：全部通过 — {summary}")
    else:
        st.warning(f"网感复检：{verdict} — {summary}")

# v0.29.1: 消费者模拟二级校验 — AI 扮演 stop_trigger 描述的具体目标用户,
# 对每个 cell 做 stop/scroll 二元判决。被 scroll 的 cell 会同步追加进
# strategic_warnings(下面那块),这里单独展示 binary 结果让用户看到
# "谁真的会被钩住"的直观对比。
consumer_sim = prompt_system.get("_consumer_simulation")
# v0.29.2: 处理失败 / 跳过状态(之前只处理 judgments 非空的成功路径)
if consumer_sim and consumer_sim.get("status") == "failed":
    st.error(
        f"消费者二层校验: 调用失败 — {consumer_sim.get('error', '未知错误')}"
    )
    if consumer_sim.get("reason"):
        st.caption(consumer_sim["reason"])
elif consumer_sim and consumer_sim.get("status") == "skipped":
    st.caption(
        f"消费者二层校验: 跳过 — {consumer_sim.get('reason', '')}"
    )
elif consumer_sim and consumer_sim.get("judgments"):
    summary = consumer_sim.get("summary", {}) or {}
    stop_n = summary.get("stop_count", 0)
    scroll_n = summary.get("scroll_count", 0)
    total = stop_n + scroll_n
    if total > 0:
        label = f"消费者二层校验: {stop_n}/{total} 会停下来(另 {scroll_n} 被划走)"
        if scroll_n == 0:
            st.success(label)
        elif scroll_n / max(total, 1) >= 0.5:
            st.error(label)
        else:
            st.warning(label)
        systemic = summary.get("systemic_issues", "") or ""
        if systemic:
            st.caption(f"系统性观察: {systemic}")
        with st.expander("展开每条 cell 的消费者反应", expanded=False):
            for j in consumer_sim.get("judgments", []) or []:
                action = j.get("action", "?")
                icon = "[停下]" if action == "stop" else "[划走]"
                st.markdown(
                    f"{icon} **{j.get('cell_id', '?')}** · {action}  \n"
                    f"*构造的用户*: {j.get('constructed_user', '(空)')[:160]}  \n"
                    f"*内心独白*: {j.get('reason', '(空)')}  \n"
                    f"*stop_trigger 锐度*: `{j.get('stop_trigger_quality', '?')}`"
                )

# v0.29.0: 策略层隐患警告 — critic 判定 interest_align / reward_signal 错配,
# rewriter 改不了,需要用户人工介入(回 secretariat 改 direction,或回
# cell_planner 改 product_role)。红色 callout,直接挂在 vibe 结果下面。
strategic_warnings = prompt_system.get("strategic_warnings") or []

# 回归哨兵的告警是 run 级的（跟项目历史比），不是 cell 级的策略隐患。
# 下面那个 expander 按 cell_id + root_cause_explanation 渲染，塞进去会显示成
# 一行空白。所以先摘出来单独给个显眼位置，剩下的再走原通道。
_regression_warnings = [
    w for w in strategic_warnings
    if w.get("type") == "quality_regression"
]
strategic_warnings = [
    w for w in strategic_warnings
    if w.get("type") != "quality_regression"
]
for _rw in _regression_warnings:
    st.error(_rw.get("message", "本轮质量分低于历史水平"))
    _d = _rw.get("detail") or {}
    _rl, _hs = _d.get("redline") or {}, _d.get("high_score_rate") or {}
    _c1, _c2 = st.columns(2)
    _c1.metric(
        "红线通过率",
        f"{_rl.get('current', 0):.0%}",
        delta=f"{(_rl.get('current', 0) - _rl.get('baseline_median', 0)):+.0%} vs 历史中位数",
        delta_color="inverse",
    )
    _c2.metric(
        "高分篇率",
        f"{_hs.get('current', 0):.0%}"
        + ("" if _hs.get("_scored", True) else "（本轮覆盖不全，未参与判定）"),
        delta=f"{(_hs.get('current', 0) - _hs.get('baseline_median', 0)):+.0%} vs 历史中位数",
        delta_color="inverse",
    )
    # 两条指标的基线是**两批不同的 run**，分开写。合成一个数字的话，"覆盖完整"
    # 这个限定词会被安到红线头上——而红线基线根本没做这个筛选。
    st.caption(
        f"红线基线：该项目最近 {_d.get('baseline_runs_redline', '?')} 条 run 的中位数"
        "（红线是确定性判定，critic 挂没挂跟它无关，所以不筛覆盖度）。"
        f"　高分篇基线：其中**评分覆盖完整**的 {_d.get('baseline_runs_high_score', '?')} 条 "
        "run 的中位数 —— 覆盖不全的 run 高分篇数是被低估的，混进来会把标准拉低、"
        "真正的退步反而报不出来。"
    )

if strategic_warnings:
    with st.expander(
        f"策略层隐患({len(strategic_warnings)} 条)——这些 cell 流水线跑完了但策略层有漏洞",
        expanded=True,
    ):
        st.error(
            "这些 cell 的 fail 根因被 critic 判为策略层问题(interest_align / "
            "reward_signal 错配),rewriter 改不了。建议人工审查:回 secretariat "
            "调整 direction 的 `stop_trigger` / `reward_type`,或回 cell_planner "
            "调整 `product_role` / `path_combination`。"
        )
        for w in strategic_warnings:
            cell_id = w.get("cell_id", "?")
            explanation = w.get("root_cause_explanation", "")
            gate = w.get("multiplier_gate", {}) or {}
            gate_desc = " · ".join(
                f"{k}={v}"
                for k, v in gate.items()
                if k in ("reward_signal", "interest_align",
                         "gap_tension", "identity_consistency")
                and v
            )
            st.markdown(
                f"**{cell_id}** (iter {w.get('iteration', '?')})  \n"
                f"{explanation or '(无 critic 解释)'}  \n"
                f"`{gate_desc}`"
            )

# Demo outputs section
demos = prompt_system.get("demo_outputs", [])
if demos:
    st.subheader("示例输出")
    for demo in demos:
        with st.expander(f"{demo.get('direction_id', '')} — {demo.get('platform', '')}"):
            st.markdown(f"**人设**: {demo.get('persona_used', '')}")
            st.markdown(demo.get("output_content", ""))

# Uncertainty summary (low-impact residuals — high-impact resolved via clarification upstream)
uncertainty_summary = prompt_system.get("_uncertainty_summary", {})
if uncertainty_summary:
    items = uncertainty_summary.get("items", [])
    checklist = uncertainty_summary.get("data_checklist", [])
    if items or checklist:
        with st.expander("可选优化项 — 补充数据可进一步提升产出质量", expanded=True):
            if items:
                for item in items:
                    st.warning(
                        f"**{item.get('source', '')}** → {item.get('field', '')}\n\n"
                        f"{item.get('reason', '')}\n\n"
                        f"{item.get('data_suggestion', '')}"
                    )
            if checklist:
                st.markdown("**建议补充的数据源：**")
                for c in checklist:
                    st.markdown(f"- {c}")

            st.divider()
            st.markdown("### 补充数据并自动优化")
            with st.form(key="uncertainty_supplement"):
                supplement_text = st.text_area(
                    "补充说明",
                    placeholder="针对上述优化项，补充你掌握的数据和信息...",
                    height=120,
                )
                supplement_files = st.file_uploader(
                    "上传补充文件",
                    accept_multiple_files=True,
                    type=["pdf", "txt", "md", "docx", "png", "jpg", "jpeg"],
                    key="uncertainty_files",
                )
                submitted = st.form_submit_button("补充并重跑优化")

            if submitted and (supplement_text.strip() or supplement_files):
                # Build supplementary content
                parts = []
                if supplement_text.strip():
                    parts.append(supplement_text.strip())
                if supplement_files:
                    for f in supplement_files:
                        try:
                            content = f.read().decode("utf-8", errors="replace")
                            parts.append(f"[补充文件: {f.name}]\n{content}")
                        except Exception:
                            parts.append(f"[补充文件: {f.name}]（无法读取）")

                supplement_content = "\n\n".join(parts)

                # Update project brief with supplement and create iteration run
                existing_brief = project.get("brief") or {}
                existing_free_text = project.get("free_text", "")
                new_free_text = (
                    existing_free_text
                    + "\n\n--- 用户补充数据（针对可选优化项） ---\n\n"
                    + supplement_content
                )

                # Clear any stale revision context so this iteration run starts
                # clean (same rationale as pages/3 「重跑流水线」). Do NOT pre-set
                # status="running" here: start_pipeline_in_background's atomic
                # claim flips the project to running as its side effect, and a
                # project already marked running would make that claim fail
                # (PipelineAlreadyRunningError) — which is exactly what happened
                # before this fix.
                _clean_brief = dict(existing_brief)
                _clean_brief.pop("_revision_context", None)
                db.update_project(
                    project_id,
                    free_text=new_free_text,
                    task_type="iteration",
                    brief=_clean_brief,
                )

                from pipeline.orchestrator import (
                    PipelineAlreadyRunningError,
                    start_pipeline_in_background,
                )
                from pipeline.agents import init_api_config
                init_api_config()
                try:
                    new_run = db.create_pipeline_run(project_id)
                    start_pipeline_in_background(project_id, new_run["id"], db)
                except PipelineAlreadyRunningError as _e:
                    st.warning(
                        f"{_e}\n\n如果任务其实已卡死，先到「流水线详情」页点"
                        "「强制终止卡死任务」重置状态，再重试。"
                    )
                    st.stop()

                st.success("已提交补充数据，流水线正在重新优化...")
                st.session_state["current_project_id"] = project_id
                st.query_params["project_id"] = project_id
                st.switch_page("pages/3_pipeline_detail.py")

# ── Export ──────────────────────────────────────────────────────────────────

st.divider()
st.subheader("导出")

col1, col2, col3 = st.columns(3)
with col1:
    md_content = export_as_markdown(prompt_system, project["name"])
    st.download_button(
        "导出 Markdown",
        data=md_content,
        file_name=f"{project['name']}_prompt_system.md",
        mime="text/markdown",
    )
with col2:
    json_content = export_as_json(prompt_system)
    st.download_button(
        "导出 JSON",
        data=json_content,
        file_name=f"{project['name']}_prompt_system.json",
        mime="application/json",
    )
with col3:
    # Full data including review
    full_data = {
        "prompt_system": prompt_system,
        "final_review": final_review,
        "project": {
            "name": project["name"],
            "task_type": project.get("task_type"),
            "brief": project.get("brief"),
        },
    }
    st.download_button(
        "导出完整数据",
        data=json.dumps(full_data, ensure_ascii=False, indent=2),
        file_name=f"{project['name']}_full_output.json",
        mime="application/json",
    )
