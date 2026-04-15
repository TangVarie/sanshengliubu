"""流水线详情 — Real-time pipeline progress and stage outputs."""

import base64
import json
import time

import streamlit as st
from db.supabase_client import SupabaseClient
from pipeline.agents import get_run_totals
from pipeline.config import (
    MAX_FINAL_REJECTIONS,
    MAX_TOKENS_PER_RUN,
    PIPELINE_STAGES,
    POLL_INTERVAL_SECONDS,
)
from utils.version_badge import show_version_badge

st.set_page_config(page_title="流水线详情", page_icon="🏛️", layout="wide")
show_version_badge()


def render_stage_error(log: dict) -> None:
    """Render an error message for a failed stage. Always shows something,
    even if error_message is empty/missing — silent failures are the worst.
    Accepts non-string error_message (e.g. legacy rows that stored a dict)
    and pretty-prints it as JSON instead of rendering a raw repr.
    """
    msg = (log or {}).get("error_message")
    if msg:
        if isinstance(msg, str):
            text = msg.strip()
        else:
            try:
                text = json.dumps(msg, ensure_ascii=False, indent=2)
            except Exception:
                text = repr(msg)
        if text:
            st.error(text)
            return
    st.error(
        f"⚠️ 该阶段标记为 failed，但 error_message 为空。\n\n"
        f"可能原因：旧版本运行 / 中转站返回空 body / 异常对象无 __str__。\n"
        f"请查看 Streamlit 服务端日志（运行控制台）获取完整 traceback。\n\n"
        f"stage_log id: `{(log or {}).get('id', '?')}`"
    )


def render_stage_meta(log: dict | None) -> None:
    """Show a compact caption at the top of each stage tab with the key
    observability fields: model actually used (including [thinking ✓]
    tag when applicable), duration, total tokens. This is how users
    answer 'did thinking actually fire on this stage?' without needing
    to dig into raw DB rows.
    """
    if not log:
        return
    bits: list[str] = []
    mu = log.get("model_used")
    if mu:
        # Color the thinking indicator so ✓/✗ is easy to skim.
        if "[thinking ✓]" in str(mu):
            mu_disp = str(mu).replace(
                "[thinking ✓]", "**`[thinking ✓]`**"
            )
        elif "[thinking ✗]" in str(mu):
            mu_disp = str(mu).replace(
                "[thinking ✗]",
                "**`[thinking ✗ — 中转站忽略了 thinking JSON]`**",
            )
        else:
            mu_disp = str(mu)
        bits.append(f"模型 {mu_disp}")
    tokens = log.get("tokens_used")
    if tokens:
        bits.append(f"tokens {int(tokens):,}")
    dur = log.get("duration_seconds")
    if dur:
        bits.append(f"耗时 {float(dur):.1f}s")
    status = log.get("status")
    if status and status not in ("completed",):
        bits.append(f"状态 {status}")
    if bits:
        st.caption(" · ".join(bits))


def render_stage_output(output_data: dict):
    """Render stage output with uncertainty annotations separated out."""
    uncertainties = output_data.get("_uncertainty", [])
    # Show clean output without _uncertainty metadata
    clean = {k: v for k, v in output_data.items() if not k.startswith("_uncertainty")}
    st.json(clean)

    if uncertainties:
        with st.expander(f"⚠️ 不确定性标注 ({len(uncertainties)}项)", expanded=False):
            for u in uncertainties:
                level = u.get("level", "inferred")
                impact = u.get("impact", "low")
                impact_label = {"high": "🔴 高", "medium": "🟡 中", "low": "🟢 低"}.get(impact, impact)
                msg = (
                    f"**{u.get('field', '')}**（影响：{impact_label}）\n\n"
                    f"{u.get('reason', '')}\n\n"
                    f"📋 **建议补充：** {u.get('data_suggestion', '')}"
                )
                if level == "speculative":
                    st.error(msg)
                else:
                    st.warning(msg)

# ── Get project ID ─────────────────────────────────────────────────────────

project_id = st.session_state.get("current_project_id") or st.query_params.get("project_id")
if not project_id:
    st.warning("请从「项目总览」选择一个项目查看。")
    st.stop()
# Sync for URL sharing and page refresh
st.query_params["project_id"] = project_id

try:
    db = SupabaseClient.get_instance()
    project = db.get_project(project_id)
    runs = db.get_runs_for_project(project_id)
except Exception as e:
    st.error(f"无法加载项目数据：{e}")
    st.stop()

# ── Header ─────────────────────────────────────────────────────────────────

STATUS_EMOJI = {
    "draft": "📝", "running": "🔄", "completed": "✅", "failed": "❌",
    "paused_for_review": "⏸️", "needs_revision": "📝🔁",
}
status = project.get("status", "draft")

# Reset session-scoped busy flags once the project is no longer running.
# Without this, a button would stay disabled in a user's tab even after
# the pipeline completed in another tab / finished via background thread.
if status != "running":
    for _key in (
        f"pipeline_busy_{project_id}",
        f"pipeline_busy_actions_{project_id}",
    ):
        if st.session_state.get(_key):
            st.session_state[_key] = False

st.title(f"🏛️ {project['name']}")
st.caption(f"状态：{STATUS_EMOJI.get(status, '❓')} {status}　|　ID: {project_id[:8]}...")

# Banner for needs_revision: surface 终审 verdict + revision instructions immediately
if status == "needs_revision":
    try:
        latest_run_id = runs[0]["id"] if runs else None
        if latest_run_id:
            _final_logs = [
                l for l in db.get_stage_logs(latest_run_id)
                if l.get("stage_name") == "chancellery_final"
            ]
            _final_review = (_final_logs[-1].get("output_data") if _final_logs else None) or {}
            _verdict = _final_review.get("verdict", "unknown")
            _instructions = _final_review.get("revision_instructions", "")
            _revisions = _final_review.get("mandatory_revisions", []) or []
            _dimensions = _final_review.get("review_dimensions", {}) or {}
            _suggestions = _final_review.get("suggestions", []) or []
            # Current final-review round from _revision_context (if any)
            _brief = (project or {}).get("brief") or {}
            _rc = _brief.get("_revision_context") or {}
            _current_round = int(_rc.get("round", 1)) if _rc else 1
            _next_round = _current_round + 1 if _rc else 2
            with st.container(border=True):
                # Round badge — shows user how close they are to force-pass
                if _current_round >= MAX_FINAL_REJECTIONS:
                    st.warning(
                        f"🔔 当前终审为 **第 {_current_round} 轮**，已达硬上限 "
                        f"({MAX_FINAL_REJECTIONS})。再次点击「应用修订意见」会触发"
                        f"**强制通过**——意味着门下省会自动放行，建议改为人工复核产出后"
                        f"点「📦 查看部分产出」导出。"
                    )
                else:
                    st.info(
                        f"🔁 当前终审为 **第 {_current_round} 轮 / 共 {MAX_FINAL_REJECTIONS} 轮**。"
                        f"下次点击「应用修订意见」将触发第 {_next_round} 轮，"
                        f"门下省会做增量评审（只看本次未解决的问题，不重复上轮已修的事）。"
                    )
                st.error(
                    f"📝🔁 **终审判定：{_verdict}** — 流水线已运行完毕，但门下省终审认为产出"
                    f"未达交付标准。下方「终审」tab 有完整审查报告。"
                )
                if _revisions:
                    st.markdown("**必须修改：**")
                    for r in _revisions:
                        st.markdown(f"- {r}")
                if _instructions:
                    with st.expander("📋 修改指令", expanded=True):
                        st.markdown(_instructions)

                # If chancellery flagged revision_required but didn't populate
                # mandatory_revisions / revision_instructions, surface whatever
                # it DID return so the user isn't stuck with an empty banner.
                if not _revisions and not _instructions:
                    st.warning(
                        "⚠️ 终审标记为 revision_required 但 `mandatory_revisions` 和 "
                        "`revision_instructions` 两个字段都是空的。下面展开可以看到终审"
                        "的完整原始输出——可能问题写在 `review_dimensions.issues` 或 "
                        "`suggestions` 里，也可能是模型没填好。"
                    )
                    if _dimensions:
                        dim_issues = []
                        for dim_name, dim_data in _dimensions.items():
                            if isinstance(dim_data, dict):
                                score = dim_data.get("score", 0)
                                issues = dim_data.get("issues", "")
                                if issues and score < 5:
                                    dim_issues.append(
                                        f"**{dim_name}** ({score}/5): {issues}"
                                    )
                        if dim_issues:
                            st.markdown("**审查维度中发现的问题：**")
                            for di in dim_issues:
                                st.markdown(f"- {di}")
                    if _suggestions:
                        st.markdown("**终审建议（非强制）：**")
                        for s in _suggestions:
                            st.markdown(f"- {s}")
                    with st.expander(
                        "🔍 终审 stage_log 完整原始输出（调试用）", expanded=False
                    ):
                        st.json(_final_review)

                # ── Apply revision button ─────────────────────────────────
                st.markdown("---")
                st.markdown(
                    "**应用修订**：把上面的 mandatory_revisions 反喂给工部，"
                    "只重跑 工部架构 → 格子规划 → 构建 → 网感复检 → 终审，"
                    "保留前面已完成的太子/中书省/尚书省/五部，不浪费 token。"
                )
                _busy_key = f"pipeline_busy_{project_id}"
                _is_busy = st.session_state.get(_busy_key, False)
                if st.button(
                    "✅ 应用修订意见并重跑工部",
                    type="primary",
                    key="apply_revision_btn",
                    disabled=_is_busy,
                    help="读取终审的 mandatory_revisions 和 revision_instructions，"
                         "存到 project.brief._revision_context，删除 ministry_works/"
                         "cell_planner/builder/vibe_critic/vibe_rewriter/chancellery_final 的 "
                         "stage_logs，然后触发 resume — 工部会拿到修订指令做针对性修复。",
                ):
                    st.session_state[_busy_key] = True
                    from pipeline.orchestrator import (
                        PipelineAlreadyRunningError,
                        revise_and_resume_pipeline_in_background,
                    )
                    from pipeline.agents import init_api_config
                    init_api_config()
                    try:
                        revise_and_resume_pipeline_in_background(
                            project_id, latest_run_id, db
                        )
                        st.success(
                            "✅ 已触发修订流程。修订指令已存入 project.brief，"
                            "工部相关 stage_logs 已清除，流水线将从工部重跑并把"
                            "修订意见作为 _revision_directives 注入到工部各 agent。"
                        )
                        st.rerun()
                    except PipelineAlreadyRunningError as _err:
                        st.session_state[_busy_key] = False
                        st.warning(
                            f"⚠️ {_err}\n\n"
                            "如果任务实际已卡死，先点页面顶部的「⛔ 强制终止卡死任务」"
                            "按钮重置状态，再试。"
                        )
                    except Exception as _err:
                        st.session_state[_busy_key] = False
                        st.error(f"应用修订失败：{_err}")
    except Exception as _e:
        st.warning(f"无法加载终审报告：{_e}")

if not runs:
    st.info("此项目尚未启动流水线。")
    st.stop()

# Use the latest run
run = runs[0]
run_id = run["id"]
stage_logs = db.get_stage_logs(run_id)

# If Supabase couldn't return the full payload (free-tier connection hiccups,
# big output_data blobs), .partial flips True and we fall back to a slim
# query without output_data. Surface it so the user isn't puzzled by empty
# stage details after a flaky refresh.
if getattr(stage_logs, "partial", False):
    st.warning(
        "⚠️ 数据未完整加载：当前表格未包含各阶段的 `output_data`（受 Supabase "
        "连接/负载影响，系统自动降级成轻量查询）。刷新页面通常会恢复。"
    )

# Build a lookup: stage_name -> log. Skip rows missing stage_name (defensive
# against partial writes / schema drift) instead of KeyError-ing the page.
log_map: dict[str, dict] = {}
for log in stage_logs:
    name = log.get("stage_name")
    if name:
        log_map[name] = log

# ── Clarification Alert ───────────────────────────────────────────────────

STAGE_DISPLAY_NAMES = {
    "crown_prince": "太子",
    "gemini_reference_analyzer": "参考帖子·Gemini",
    "gemini_trend_scout_pre": "趋势取样·Gemini",
    "gemini_trend_scout_post": "网感对标·Gemini",
    "secretariat": "中书省",
    "chancellery": "门下省",
    "dispatcher": "尚书省",
    "ministry_personnel": "吏部",
    "ministry_revenue": "户部",
    "ministry_rites": "礼部",
    "ministry_war": "兵部",
    "ministry_justice": "刑部",
    "ministry_works": "工部·架构",
    "ministry_works_cell_planner": "工部·格子规划",
    "ministry_works_builder": "工部·构建",
    "ministry_works_structure_review": "结构审·Gemini",
    "vibe_critic": "网感复检",
    "vibe_rewriter": "网感重写",
    "chancellery_final": "终审",
}

needs_input_logs = [
    l for l in stage_logs
    if l.get("status") == "needs_input" and not l.get("human_intervention")
]
for ni_log in needs_input_logs:
    output = ni_log.get("output_data", {})
    _ni_name = ni_log.get("stage_name", "unknown")
    stage_display = STAGE_DISPLAY_NAMES.get(_ni_name, _ni_name)
    questions = output.get("questions", [])
    context_info = output.get("context", "")

    st.warning(f"⏸️ **{stage_display}** 需要你补充信息才能继续")

    if context_info:
        st.info(f"**为什么需要这些信息：** {context_info}")

    if questions:
        st.markdown("**需要回答的问题：**")
        for i, q in enumerate(questions, 1):
            st.markdown(f"{i}. {q}")

    with st.form(key=f"clarification_{ni_log['id']}"):
        user_answer = st.text_area(
            "请补充说明",
            placeholder="回答上述问题，提供缺失的信息...",
            height=120,
            key=f"answer_{ni_log['id']}",
        )
        uploaded_files = st.file_uploader(
            "拖拽或点击上传补充文件（支持多选，可一次拖入多张图片）",
            accept_multiple_files=True,
            type=["pdf", "txt", "md", "docx", "png", "jpg", "jpeg", "gif", "webp", "json"],
            key=f"files_{ni_log['id']}",
        )
        submitted = st.form_submit_button("📤 提交补充信息")

        if submitted and user_answer.strip():
            intervention = {"answer": user_answer.strip()}
            if uploaded_files:
                file_texts = []
                for f in uploaded_files:
                    try:
                        name = f.name.lower()
                        if name.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
                            b64 = base64.b64encode(f.read()).decode("utf-8")
                            file_texts.append(f"[补充图片: {f.name}]\n[BASE64_IMAGE:{b64}]")
                        else:
                            file_texts.append(f"[补充文件: {f.name}]\n{f.read().decode('utf-8', errors='replace')}")
                    except Exception:
                        file_texts.append(f"[补充文件: {f.name}]（无法读取）")
                intervention["supplementary_files"] = "\n\n".join(file_texts)
            try:
                db.update_stage_log(ni_log["id"], human_intervention=intervention)
                st.success("已提交！流水线将自动恢复执行。")
                time.sleep(1)
                st.rerun()
            except Exception as e:
                st.error(
                    f"提交失败：{e}\n\n"
                    "如果是权限错误，请在 Supabase SQL Editor 中执行：\n"
                    "`ALTER TABLE stage_logs DISABLE ROW LEVEL SECURITY;`"
                )

    st.divider()

# ── Stuck detection + force-cancel ────────────────────────────────────────
# When status=running but no stage_log has updated_at in the last
# STUCK_THRESHOLD_SECONDS, the daemon thread is almost certainly wedged
# in an external call (relay hang, network timeout, Gemini deadlock).
# We can't kill Python threads from outside, so instead we let the user
# force-reset the project state — new runs can then be triggered and
# the zombie thread is just ignored (its future writes land on a run
# nobody looks at).
STUCK_THRESHOLD_SECONDS = 300  # 5 minutes since last stage_log update

if status == "running":
    # Find the most recent update across stage_logs
    _most_recent = 0.0
    for _l in stage_logs:
        _ts = _l.get("updated_at") or _l.get("created_at")
        if not _ts:
            continue
        try:
            from datetime import datetime as _dt
            # Supabase timestamps are ISO8601 with timezone
            _ts_str = str(_ts).replace("Z", "+00:00")
            _dt_obj = _dt.fromisoformat(_ts_str)
            _most_recent = max(_most_recent, _dt_obj.timestamp())
        except Exception:
            continue

    if _most_recent > 0:
        _elapsed = time.time() - _most_recent
        _looks_stuck = _elapsed > STUCK_THRESHOLD_SECONDS
    else:
        _looks_stuck = False
        _elapsed = 0.0

    # Always show a cancel button when status=running (primary escape
    # hatch). Make the stuck banner loud so the user sees why they'd
    # click it.
    with st.container(border=True):
        if _looks_stuck:
            st.error(
                f"⚠️ **流水线疑似卡死** — 已经 {int(_elapsed // 60)} 分 "
                f"{int(_elapsed % 60)} 秒没有任何阶段更新。"
                f"大概率是某个外部调用（中转站 / Gemini / 网络）被 hang 住。"
                f"Python 无法从外部杀死后台线程，但可以**强制重置状态**让你重新开始——"
                f"点下面按钮即可。"
            )
        else:
            st.info(
                f"🔄 流水线正在执行（最近更新 {int(_elapsed)} 秒前）。"
                f"需要中止当前任务可以点下面的强制终止。"
            )

        _force_busy_key = f"force_cancel_busy_{project_id}"
        _force_busy = st.session_state.get(_force_busy_key, False)
        if st.button(
            "⛔ 强制终止卡死任务",
            disabled=_force_busy,
            key=f"force_cancel_btn_{project_id}",
            type="primary" if _looks_stuck else "secondary",
            help=(
                "把 project.status 和所有 running 的 pipeline_run 硬标为 failed。"
                "zombie 线程可能仍在后台消耗资源，直到下次 Streamlit Cloud Reboot "
                "才彻底清掉——但你可以立刻点「重跑流水线」开新 run，不受旧线程干扰"
                "（每次重跑都是新 run_id）。"
            ),
        ):
            st.session_state[_force_busy_key] = True
            try:
                from pipeline.orchestrator import force_cancel_pipeline
                summary = force_cancel_pipeline(project_id, db)
                st.success(
                    f"✅ 已强制终止 {summary['cancelled_runs']} 个 running 的 run。"
                    f"点「重跑流水线」可以开新 run 了。"
                )
                time.sleep(1.5)
                st.session_state[_force_busy_key] = False
                st.rerun()
            except Exception as err:
                st.session_state[_force_busy_key] = False
                st.error(f"强制终止失败：{type(err).__name__}: {err}")

# ── Pipeline Progress Bar ──────────────────────────────────────────────────

st.subheader("流水线进度")

cols = st.columns(len(PIPELINE_STAGES))
for i, (stage_key, stage_label, stage_icon) in enumerate(PIPELINE_STAGES):
    # Chancellery may have numbered variants
    log = log_map.get(stage_key)
    if not log and stage_key == "chancellery":
        for k in ["chancellery_1", "chancellery_2", "chancellery_3"]:
            if k in log_map:
                log = log_map[k]
                break

    with cols[i]:
        s = log.get("status", "pending") if log else "pending"
        if s == "completed":
            st.success(f"{stage_icon}\n{stage_label}")
        elif s == "running":
            st.warning(f"{stage_icon}\n{stage_label}")
        elif s == "needs_input":
            st.error(f"⏸️\n{stage_label}\n需要补充")
        elif s == "failed":
            st.error(f"{stage_icon}\n{stage_label}")
        elif s == "skipped":
            # Skipped may be either (a) a resume that intentionally jumped
            # over an already-completed stage, or (b) a Gemini advisory
            # stage that couldn't run (not configured / API error / parse
            # error). Surface the underlying _skip_reason when present so
            # the user can distinguish. Abbreviate it to fit the box.
            _skip_reason = ""
            _out = (log or {}).get("output_data") or {}
            if isinstance(_out, dict):
                _skip_reason = str(_out.get("_skip_reason", ""))[:30]
            if _skip_reason:
                st.info(f"{stage_icon}\n{stage_label}\n_{_skip_reason}_")
            else:
                st.info(f"{stage_icon}\n{stage_label}")
        else:
            st.container(border=True).markdown(
                f"<div style='text-align:center;color:#999'>{stage_icon}<br>{stage_label}</div>",
                unsafe_allow_html=True,
            )

# ── Stage Details ──────────────────────────────────────────────────────────

st.divider()
st.subheader("各环节详情")

# Group stages into tabs
tab_names = ["太子", "中书省", "门下省", "尚书省", "六部", "终审"]
tabs = st.tabs(tab_names)

# Tab 0: Crown Prince
with tabs[0]:
    log = log_map.get("crown_prince")
    render_stage_meta(log)
    if log and log.get("output_data"):
        render_stage_output(log["output_data"])
    elif log:
        st.info(f"状态：{log.get('status', 'pending')}")
        if log.get("status") == "failed":
            render_stage_error(log)
    else:
        st.caption("等待执行...")

    # Gemini trend scout (pre-secretariat) — surface the raw posts
    # that got injected into brief._trend_intel so the user sees
    # exactly what secretariat received as calibration samples.
    ts_log = log_map.get("gemini_trend_scout_pre")
    if ts_log:
        st.divider()
        st.markdown("**🔭 Gemini 趋势取样（pre-secretariat）**")
        render_stage_meta(ts_log)
        ts_status = ts_log.get("status", "pending")
        ts_out = ts_log.get("output_data") or {}
        if ts_status == "skipped":
            reason = str(ts_out.get("_skip_reason", "unknown"))
            if "not_configured" in reason:
                st.warning(
                    "⚠️ 跳过（Gemini 未配置）——配置好 `VERTEX_EXPRESS_API_KEY` "
                    "且 `ENABLE_GEMINI_TREND_SCOUT_PRE=True` 后下次生效。"
                )
            elif "parse_error" in reason:
                _raw = ts_out.get("_raw_text_preview", "")
                st.error(
                    f"❌ Gemini 输出非 JSON，无法解析（`{reason}`）。"
                    "Google Search grounding 经常让 Gemini 改口用自然语言叙述。"
                    "下面是它实际返回的前 500 字："
                )
                if _raw:
                    st.code(_raw, language="text")
                else:
                    st.caption("（没抓到原文预览，试试 Reboot 或换 GEMINI_MODEL。）")
            else:
                st.info(f"⏭️ 跳过：`{reason}`")
        elif ts_status == "completed":
            posts = ts_out.get("posts") or []
            queries = ts_out.get("queries_used") or []
            rejected = ts_out.get("_rejected_off_domain_count", 0)
            if posts:
                st.success(
                    f"✅ 拉到 {len(posts)} 条小红书原文帖子"
                    + (f"（过滤掉 {rejected} 条非 xiaohongshu 域名）" if rejected else "")
                )
                for i, p in enumerate(posts, 1):
                    title = p.get("title") or "(无标题)"
                    snippet = p.get("snippet") or ""
                    url = p.get("url", "")
                    flag = " ⚠️ 疑似分析文" if p.get("_suspect_analysis") else ""
                    with st.expander(f"#{i} 《{title}》{flag}"):
                        if snippet:
                            st.markdown(f"**片段**：{snippet}")
                        if url:
                            st.markdown(f"**URL**：[{url}]({url})")
                        if p.get("cover_image_url"):
                            st.image(
                                p["cover_image_url"],
                                caption="封面（来自 Google 缩略图）",
                                width=200,
                            )
                if queries:
                    with st.expander("🔍 Gemini 用到的搜索查询", expanded=False):
                        for q in queries:
                            st.markdown(f"- `{q}`")
            else:
                nf = ts_out.get("_not_found_reason") or "搜索返回空"
                st.warning(f"⚠️ 没找到真实原文帖子：{nf}")
        elif ts_status == "failed":
            render_stage_error(ts_log)
        else:
            st.caption(f"状态：{ts_status}")

# Tab 1: Secretariat
with tabs[1]:
    # May have multiple secretariat runs
    sec_logs = [l for l in stage_logs if l["stage_name"] == "secretariat"]
    if sec_logs:
        for sl in sec_logs:
            with st.expander(f"方案 (轮次 {sec_logs.index(sl) + 1})", expanded=(sl == sec_logs[-1])):
                render_stage_meta(sl)
                if sl.get("output_data"):
                    render_stage_output(sl["output_data"])
                elif sl.get("status") == "failed":
                    render_stage_error(sl)
                else:
                    st.caption(f"状态：{sl.get('status', 'pending')}")
    else:
        st.caption("等待执行...")


# Tab 2: Chancellery
with tabs[2]:
    chan_logs = [l for l in stage_logs if l["stage_name"].startswith("chancellery_")]
    if chan_logs:
        for cl in chan_logs:
            round_label = cl["stage_name"].replace("chancellery_", "第") + "轮审议"
            with st.expander(round_label, expanded=(cl == chan_logs[-1])):
                render_stage_meta(cl)
                output = cl.get("output_data", {})
                if output:
                    verdict = output.get("verdict", "unknown")
                    if verdict == "approved":
                        st.success(f"✅ 判定：{verdict}")
                    else:
                        st.warning(f"⚠️ 判定：{verdict}")

                    dims = output.get("review_dimensions", {})
                    if dims:
                        st.markdown("**审查评分：**")
                        for dim_name, dim_data in dims.items():
                            score = dim_data.get("score", 0) if isinstance(dim_data, dict) else 0
                            st.progress(score / 5, text=f"{dim_name}: {score}/5")
                            issues = dim_data.get("issues", "") if isinstance(dim_data, dict) else ""
                            if issues:
                                st.caption(issues)

                    revisions = output.get("mandatory_revisions", [])
                    if revisions:
                        st.markdown("**必须修改：**")
                        for r in revisions:
                            st.markdown(f"- {r}")

                    # Human intervention area
                    if verdict == "revision_required":
                        with st.expander("💬 给中书省补充指令（可选）"):
                            note = st.text_area("你的补充说明", key=f"human_{cl['id']}")
                            if st.button("发送并继续", key=f"send_{cl['id']}"):
                                db.update_stage_log(
                                    cl["id"],
                                    human_intervention={"note": note},
                                )
                                st.success("已发送")
                elif cl.get("status") == "failed":
                    render_stage_error(cl)
    else:
        st.caption("等待执行...")

# Tab 3: Dispatcher
with tabs[3]:
    log = log_map.get("dispatcher")
    render_stage_meta(log)
    if log and log.get("output_data"):
        render_stage_output(log["output_data"])
    elif log:
        st.info(f"状态：{log.get('status', 'pending')}")
        if log.get("status") == "failed":
            render_stage_error(log)
    else:
        st.caption("等待执行...")

# Tab 4: Six Ministries
with tabs[4]:
    ministry_tabs = st.tabs(["吏部", "户部", "礼部", "兵部", "刑部", "工部"])
    ministry_keys = [
        "ministry_personnel", "ministry_revenue", "ministry_rites",
        "ministry_war", "ministry_justice", "ministry_works",
    ]
    def _render_stage_log(stage_key: str, label: str = ""):
        log = log_map.get(stage_key)
        render_stage_meta(log)
        if log and log.get("output_data"):
            render_stage_output(log["output_data"])
        elif log:
            s = log.get("status", "pending")
            if s == "running":
                st.info("⏳ 执行中...")
            elif s == "failed":
                render_stage_error(log)
            elif s == "skipped":
                st.warning("⏭️ 已跳过")
            else:
                st.caption(f"状态：{s}")
        else:
            st.caption("等待执行...")

    for i, mk in enumerate(ministry_keys):
        with ministry_tabs[i]:
            _render_stage_log(mk)
            # Works tab also shows cell planner + builder logs
            if mk == "ministry_works":
                def _batch_label(log: dict, fallback_prefix: str, idx: int) -> str:
                    """Read _batch_info from log.input_data for human-friendly label.
                    Falls back to legacy sequential numbering."""
                    input_data = log.get("input_data") or {}
                    info = input_data.get("_batch_info") or {}
                    label = info.get("label")
                    cell_ids = info.get("cell_ids") or []
                    status = log.get("status", "pending")
                    status_tag = ""
                    if status == "failed":
                        status_tag = " ❌"
                    elif status == "completed":
                        status_tag = " ✅"
                    elif status == "running":
                        status_tag = " ⏳"
                    if label:
                        cells_str = f" [{', '.join(cell_ids)}]" if cell_ids else ""
                        return f"{label}{cells_str}{status_tag}"
                    return f"{fallback_prefix} {idx + 1}{status_tag}"

                cell_planner_logs = [l for l in stage_logs if l["stage_name"] == "ministry_works_cell_planner"]
                if cell_planner_logs:
                    st.divider()
                    st.markdown(f"**工部·格子规划**（共 {len(cell_planner_logs)} 次调用，含 batch/cell 重试）")
                    for idx, cl in enumerate(cell_planner_logs):
                        with st.expander(_batch_label(cl, "格子规划批次", idx), expanded=False):
                            if cl.get("output_data"):
                                render_stage_output(cl["output_data"])
                            elif cl.get("status") == "failed":
                                render_stage_error(cl)
                builder_logs = [l for l in stage_logs if l["stage_name"] == "ministry_works_builder"]
                if builder_logs:
                    st.divider()
                    st.markdown(f"**工部·构建**（共 {len(builder_logs)} 次调用，含 batch/cell 重试）")
                    for idx, bl in enumerate(builder_logs):
                        with st.expander(_batch_label(bl, "构建批次", idx), expanded=False):
                            if bl.get("output_data"):
                                render_stage_output(bl["output_data"])
                            elif bl.get("status") == "failed":
                                render_stage_error(bl)

                # Gemini structure review (advisory, between builder and vibe).
                # Surface the skip reason prominently when skipped — that's
                # how the operator finds out "Gemini isn't actually running".
                sr_log = log_map.get("ministry_works_structure_review")
                if sr_log:
                    st.divider()
                    st.markdown("**🔎 Gemini 结构审**")
                    render_stage_meta(sr_log)
                    sr_status = sr_log.get("status", "pending")
                    sr_out = sr_log.get("output_data") or {}
                    if sr_status == "skipped":
                        reason = sr_out.get("_skip_reason", "unknown")
                        if "not_configured" in str(reason):
                            st.warning(
                                f"⚠️ 跳过（Gemini 未配置）：`{reason}`\n\n"
                                "检查 `.streamlit/secrets.toml` 的 "
                                "`VERTEX_EXPRESS_API_KEY` 是否填了，"
                                "以及 `pipeline/config.py` 的 "
                                "`ENABLE_GEMINI_ASSIST=True`。"
                            )
                        elif "call_failed" in str(reason):
                            st.error(
                                f"❌ 调用失败：`{reason}`\n\n"
                                "大概率是模型名 `GEMINI_MODEL` 不对（Vertex 返回 404），"
                                "或 API key 无效、配额用光。"
                                "去 `pipeline/config.py` 把 `GEMINI_MODEL` 改成"
                                "你 Vertex 账户里实际可用的模型 ID "
                                "（常见：`gemini-2.5-pro` / `gemini-2.5-flash`）。"
                            )
                        elif "parse_error" in str(reason):
                            _raw = sr_out.get("_raw_text_preview", "")
                            st.error(
                                f"❌ 输出非 JSON：`{reason}`\n\n"
                                "Gemini 返回的内容没法解析成 JSON——"
                                "可能被安全过滤，或 Google Search 工具接管了响应格式。"
                                "下面是它实际返回的前 500 字，看看它到底说了什么："
                            )
                            if _raw:
                                st.code(_raw, language="text")
                            else:
                                st.caption(
                                    "（没抓到原文预览——可能响应完全空，"
                                    "换个 `GEMINI_MODEL` 或关掉 `ENABLE_GEMINI_TREND_SCOUT_*` 试试。）"
                                )
                        else:
                            st.info(f"⏭️ 跳过：`{reason}`")
                    elif sr_status == "completed":
                        verdict = sr_out.get("verdict", "unknown")
                        incomplete = sr_out.get("cells_incomplete") or []
                        if verdict == "all_pass":
                            st.success(
                                f"✅ 所有 cell 结构完整 · "
                                f"Gemini 评审通过（{len(sr_out.get('cell_reviews', []))} 条）"
                            )
                        elif incomplete:
                            st.warning(
                                f"⚠️ {len(incomplete)} 个 cell 结构不全："
                            )
                            for item in incomplete:
                                cid = item.get("cell_id", "?")
                                missing = item.get("missing_items") or []
                                hint = item.get("revision_hint", "")
                                with st.expander(f"📌 {cid}（缺 {len(missing)} 项）"):
                                    if missing:
                                        st.markdown("**缺失项：**")
                                        for m in missing:
                                            st.markdown(f"- {m}")
                                    if hint:
                                        st.markdown(f"**建议修法：** {hint}")
                        else:
                            st.info(f"判定：{verdict}")
                        with st.expander("🔍 完整评审输出", expanded=False):
                            st.json(sr_out)
                    elif sr_status == "failed":
                        render_stage_error(sr_log)
                    else:
                        st.caption(f"状态：{sr_status}")

# Tab 5: Final Review
with tabs[5]:
    log = log_map.get("chancellery_final")
    render_stage_meta(log)
    if log and log.get("output_data"):
        output = log["output_data"]
        verdict = output.get("verdict", "unknown")
        if verdict == "approved":
            st.success("✅ 终审通过")
        else:
            st.warning(f"⚠️ 终审判定：{verdict}")
        render_stage_output(output)
    elif log:
        st.info(f"状态：{log.get('status', 'pending')}")
    else:
        st.caption("等待执行...")

# ── Stats footer ───────────────────────────────────────────────────────────

st.divider()
col_a, col_b, col_c, col_d = st.columns(4)

total_tokens = sum(l.get("tokens_used", 0) for l in stage_logs)
total_duration = sum(l.get("duration_seconds", 0) or 0 for l in stage_logs)
completed_stages = sum(1 for l in stage_logs if l.get("status") == "completed")

# Cost: prefer the DB-authoritative value on pipeline_runs.total_cost_usd
# (updated after each agent completion via agents/__init__.py), fall back
# to in-process _run_totals for the same-process running case, then to 0.
_run_cost_db = 0.0
try:
    _run_cost_db = float(run.get("total_cost_usd", 0) or 0)
except (TypeError, ValueError):
    _run_cost_db = 0.0

# Live per-run totals from the in-process budget tracker (only populated
# while the thread is running in this Streamlit process). Used mainly to
# surface cache effectiveness; falls back to stage_logs sum otherwise.
_run_totals = get_run_totals(run_id)
_cache_read = _run_totals.get("cache_read", 0)
_cache_creation = _run_totals.get("cache_creation", 0)
_calls = _run_totals.get("calls", 0)
_cache_calls = _run_totals.get("calls_with_cache_activity", 0)
_run_cost_live = float(_run_totals.get("cost_usd", 0.0))
_run_cost = max(_run_cost_db, _run_cost_live)

with col_a:
    st.metric("已完成环节", f"{completed_stages}/{len(PIPELINE_STAGES)}")
with col_b:
    # Show budget remaining as the subtitle so the operator can eyeball how
    # close we are to the hard cap. Uses stage_logs sum as authoritative
    # (works across resume); in-process totals are additive during a fresh run.
    budget_pct = (total_tokens / MAX_TOKENS_PER_RUN * 100) if MAX_TOKENS_PER_RUN else 0
    st.metric(
        "总 Token",
        f"{total_tokens:,}",
        f"{budget_pct:.0f}% of budget",
        delta_color="off",
    )
with col_c:
    # Cost is best-effort (unknown models → $0). The caption below states
    # the precision so a $0.00 reading isn't mistaken for free execution.
    st.metric("预计成本", f"${_run_cost:.2f}")
with col_d:
    st.metric("总耗时", f"{total_duration:.1f}s")

# Cache effectiveness indicator — only shown when the background thread
# reported at least a few calls in this process. Otherwise the totals are
# 0 because a different process/restart handled those calls.
if _calls >= 3:
    if _cache_read + _cache_creation == 0:
        st.caption(
            f"🟡 Prompt 缓存：本次 {_calls} 次调用中 0 次命中缓存。"
            f"很可能中转站丢弃了 `cache_control` 字段——在 `pipeline/config.py` "
            f"把 `ENABLE_PROMPT_CACHING` 改成 `False` 可以避免发送多余字段。"
        )
    else:
        hit_rate = _cache_calls / _calls * 100
        st.caption(
            f"🟢 Prompt 缓存生效：{_cache_calls}/{_calls} 次调用命中 "
            f"({hit_rate:.0f}%)，已累计 cache_read={_cache_read:,} / "
            f"cache_creation={_cache_creation:,} tokens。"
        )

# ── Actions + auto-refresh ─────────────────────────────────────────────────

st.divider()
c1, c2, c3 = st.columns(3)
with c1:
    if status in ("completed", "needs_revision"):
        label = "📦 查看产出" if status == "completed" else "📦 查看部分产出"
        if st.button(label):
            st.session_state["current_project_id"] = project_id
            st.query_params["project_id"] = project_id
            st.switch_page("pages/4_output_center.py")
# Session-scoped busy flag prevents a second click before Streamlit reruns.
_actions_busy_key = f"pipeline_busy_actions_{project_id}"
_actions_busy = st.session_state.get(_actions_busy_key, False)

with c2:
    # Allow resume whenever not actively completed — covers failed, paused, and
    # silently-hung "running" states (e.g. background thread crashed without
    # updating the project status).
    if status != "completed":
        if st.button(
            "▶️ 继续执行",
            disabled=_actions_busy,
            help=(
                "从失败/中断处继续。已完成的 stage 直接跳过；工部的格子规划和构建"
                "还会做 cell 级 resume——扫描之前成功的批次，只重跑真正还没成功的"
                "cell，不会让已经花过 token 的产出白白重算。"
            ),
        ):
            st.session_state[_actions_busy_key] = True
            from pipeline.orchestrator import (
                PipelineAlreadyRunningError,
                resume_pipeline_in_background,
            )
            from pipeline.agents import init_api_config
            init_api_config()
            try:
                # Don't pre-set project.status here; orchestrator.run() does
                # it, and our guard checks project.status so pre-setting
                # would make the guard reject our own call.
                resume_pipeline_in_background(project_id, run_id, db)
                st.rerun()
            except PipelineAlreadyRunningError as _err:
                st.session_state[_actions_busy_key] = False
                st.warning(
                    f"⚠️ {_err}\n\n"
                    "如果任务实际已卡死，先点页面顶部的「⛔ 强制终止卡死任务」"
                    "按钮重置状态，再试。"
                )
            except Exception as _err:
                st.session_state[_actions_busy_key] = False
                st.error(f"继续执行失败：{_err}")
with c3:
    # Restart from scratch — always available except while a fresh run is healthy
    if st.button(
        "🔄 重跑流水线",
        disabled=_actions_busy,
        help="完全从头开始（会创建新的 run）",
    ):
        st.session_state[_actions_busy_key] = True
        from pipeline.orchestrator import (
            PipelineAlreadyRunningError,
            start_pipeline_in_background,
        )
        from pipeline.agents import init_api_config
        init_api_config()
        try:
            # create_pipeline_run + start — orchestrator sets project.status
            # internally, so we skip the pre-emptive update that would
            # trip our own guard.
            new_run = db.create_pipeline_run(project_id)
            start_pipeline_in_background(project_id, new_run["id"], db)
            st.rerun()
        except PipelineAlreadyRunningError as _err:
            st.session_state[_actions_busy_key] = False
            st.warning(
                f"⚠️ {_err}\n\n"
                "如果任务实际已卡死，先点页面顶部的「⛔ 强制终止卡死任务」"
                "按钮重置状态，再试。"
            )
        except Exception as _err:
            st.session_state[_actions_busy_key] = False
            st.error(f"重跑失败：{_err}")

# Auto-refresh when running or paused for review (waiting for user input)
if status in ("running", "paused_for_review") or run.get("status") in ("running", "paused_for_review"):
    time.sleep(POLL_INTERVAL_SECONDS)
    st.rerun()
