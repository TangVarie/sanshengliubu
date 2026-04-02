"""流水线详情 — Real-time pipeline progress and stage outputs."""

import base64
import json
import time

import streamlit as st
from db.supabase_client import SupabaseClient
from pipeline.config import PIPELINE_STAGES, POLL_INTERVAL_SECONDS

st.set_page_config(page_title="流水线详情", page_icon="🏛️", layout="wide")


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

STATUS_EMOJI = {"draft": "📝", "running": "🔄", "completed": "✅", "failed": "❌", "paused_for_review": "⏸️"}
status = project.get("status", "draft")
st.title(f"🏛️ {project['name']}")
st.caption(f"状态：{STATUS_EMOJI.get(status, '❓')} {status}　|　ID: {project_id[:8]}...")

if not runs:
    st.info("此项目尚未启动流水线。")
    st.stop()

# Use the latest run
run = runs[0]
run_id = run["id"]
stage_logs = db.get_stage_logs(run_id)

# Build a lookup: stage_name -> log
log_map: dict[str, dict] = {}
for log in stage_logs:
    log_map[log["stage_name"]] = log

# ── Clarification Alert ───────────────────────────────────────────────────

STAGE_DISPLAY_NAMES = {
    "crown_prince": "太子",
    "secretariat": "中书省",
    "chancellery": "门下省",
    "dispatcher": "尚书省",
    "ministry_personnel": "吏部",
    "ministry_revenue": "户部",
    "ministry_rites": "礼部",
    "ministry_war": "兵部",
    "ministry_justice": "刑部",
    "ministry_works": "工部·规划",
    "ministry_works_builder": "工部·构建",
}

needs_input_logs = [l for l in stage_logs if l.get("status") == "needs_input"]
for ni_log in needs_input_logs:
    output = ni_log.get("output_data", {})
    stage_display = STAGE_DISPLAY_NAMES.get(ni_log["stage_name"], ni_log["stage_name"])
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
    if log and log.get("output_data"):
        render_stage_output(log["output_data"])
    elif log:
        st.info(f"状态：{log.get('status', 'pending')}")
        if log.get("error_message"):
            st.error(log["error_message"])
    else:
        st.caption("等待执行...")

# Tab 1: Secretariat
with tabs[1]:
    # May have multiple secretariat runs
    sec_logs = [l for l in stage_logs if l["stage_name"] == "secretariat"]
    if sec_logs:
        for sl in sec_logs:
            with st.expander(f"方案 (轮次 {sec_logs.index(sl) + 1})", expanded=(sl == sec_logs[-1])):
                if sl.get("output_data"):
                    render_stage_output(sl["output_data"])
                elif sl.get("error_message"):
                    st.error(sl["error_message"])
    else:
        st.caption("等待执行...")


# Tab 2: Chancellery
with tabs[2]:
    chan_logs = [l for l in stage_logs if l["stage_name"].startswith("chancellery_")]
    if chan_logs:
        for cl in chan_logs:
            round_label = cl["stage_name"].replace("chancellery_", "第") + "轮审议"
            with st.expander(round_label, expanded=(cl == chan_logs[-1])):
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
                elif cl.get("error_message"):
                    st.error(cl["error_message"])
    else:
        st.caption("等待执行...")

# Tab 3: Dispatcher
with tabs[3]:
    log = log_map.get("dispatcher")
    if log and log.get("output_data"):
        render_stage_output(log["output_data"])
    elif log:
        st.info(f"状态：{log.get('status', 'pending')}")
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
        if log and log.get("output_data"):
            render_stage_output(log["output_data"])
        elif log:
            s = log.get("status", "pending")
            if s == "running":
                st.info("⏳ 执行中...")
            elif s == "failed":
                st.error(f"执行失败：{log.get('error_message', '')}")
            elif s == "skipped":
                st.warning("⏭️ 已跳过")
            else:
                st.caption(f"状态：{s}")
        else:
            st.caption("等待执行...")

    for i, mk in enumerate(ministry_keys):
        with ministry_tabs[i]:
            _render_stage_log(mk)
            # Works tab also shows builder logs
            if mk == "ministry_works":
                builder_logs = [l for l in stage_logs if l["stage_name"] == "ministry_works_builder"]
                if builder_logs:
                    st.divider()
                    st.markdown("**工部·构建（分批执行）**")
                    for bl in builder_logs:
                        with st.expander(f"构建批次 {builder_logs.index(bl) + 1}", expanded=False):
                            if bl.get("output_data"):
                                render_stage_output(bl["output_data"])
                            elif bl.get("error_message"):
                                st.error(bl["error_message"])

# Tab 5: Final Review
with tabs[5]:
    log = log_map.get("chancellery_final")
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
col_a, col_b, col_c = st.columns(3)

total_tokens = sum(l.get("tokens_used", 0) for l in stage_logs)
total_duration = sum(l.get("duration_seconds", 0) or 0 for l in stage_logs)
completed_stages = sum(1 for l in stage_logs if l.get("status") == "completed")

with col_a:
    st.metric("已完成环节", f"{completed_stages}/{len(PIPELINE_STAGES)}")
with col_b:
    st.metric("总 Token", f"{total_tokens:,}")
with col_c:
    st.metric("总耗时", f"{total_duration:.1f}s")

# ── Actions + auto-refresh ─────────────────────────────────────────────────

st.divider()
c1, c2, c3 = st.columns(3)
with c1:
    if status == "completed":
        if st.button("📦 查看产出"):
            st.session_state["current_project_id"] = project_id
            st.query_params["project_id"] = project_id
            st.switch_page("pages/4_output_center.py")
with c2:
    if status == "failed":
        if st.button("▶️ 继续执行", help="从失败处继续，跳过已完成的阶段"):
            from pipeline.orchestrator import resume_pipeline_in_background
            from pipeline.agents import init_api_config
            init_api_config()
            resume_pipeline_in_background(project_id, run_id, db)
            st.rerun()
with c3:
    if status == "completed" or status == "failed":
        if st.button("🔄 重跑流水线", help="完全从头开始"):
            from pipeline.orchestrator import start_pipeline_in_background
            from pipeline.agents import init_api_config
            new_run = db.create_pipeline_run(project_id)
            db.update_project(project_id, status="running")
            init_api_config()
            start_pipeline_in_background(project_id, new_run["id"], db)
            st.rerun()

# Auto-refresh when running or paused for review (waiting for user input)
if status in ("running", "paused_for_review") or run.get("status") in ("running", "paused_for_review"):
    time.sleep(POLL_INTERVAL_SECONDS)
    st.rerun()
