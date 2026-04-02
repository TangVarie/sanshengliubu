"""流水线详情 — Real-time pipeline progress and stage outputs."""

import json
import time

import streamlit as st
from db.supabase_client import SupabaseClient
from pipeline.config import PIPELINE_STAGES, POLL_INTERVAL_SECONDS

st.set_page_config(page_title="流水线详情", page_icon="🏛️", layout="wide")

# ── Get project ID ─────────────────────────────────────────────────────────

project_id = st.query_params.get("project_id")
if not project_id:
    st.warning("请从「项目总览」选择一个项目查看。")
    st.stop()

try:
    db = SupabaseClient.get_instance()
    project = db.get_project(project_id)
    runs = db.get_runs_for_project(project_id)
except Exception as e:
    st.error(f"无法加载项目数据：{e}")
    st.stop()

# ── Header ─────────────────────────────────────────────────────────────────

STATUS_EMOJI = {"draft": "📝", "running": "🔄", "completed": "✅", "failed": "❌"}
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
        st.json(log["output_data"])
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
                    st.json(sl["output_data"])
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
        st.json(log["output_data"])
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
    for i, mk in enumerate(ministry_keys):
        with ministry_tabs[i]:
            log = log_map.get(mk)
            if log and log.get("output_data"):
                st.json(log["output_data"])
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
        st.json(output)
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
            st.query_params["project_id"] = project_id
            st.switch_page("pages/4_output_center.py")
with c2:
    if status == "completed" or status == "failed":
        if st.button("🔄 重跑流水线"):
            from pipeline.orchestrator import start_pipeline_in_background
            new_run = db.create_pipeline_run(project_id)
            db.update_project(project_id, status="running")
            start_pipeline_in_background(project_id, new_run["id"], db)
            st.rerun()

# Auto-refresh when running
if status == "running" or run.get("status") == "running":
    time.sleep(POLL_INTERVAL_SECONDS)
    st.rerun()
