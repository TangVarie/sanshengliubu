"""产出中心 — View and export the generated prompt system."""

import json

import streamlit as st
from db.supabase_client import SupabaseClient
from utils.export import export_as_markdown, export_as_json

st.set_page_config(page_title="产出中心", page_icon="📦", layout="wide")
st.title("📦 产出中心")

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
    completed = [p for p in projects if p["status"] == "completed"]
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

st.subheader(f"📄 {project['name']}")

# Final review summary
if final_review:
    verdict = final_review.get("verdict", "unknown")
    if verdict == "approved":
        st.success("✅ 终审通过")
    else:
        st.warning(f"⚠️ 终审状态：{verdict}")

# Display prompt matrix (platform → direction) or legacy templates
matrix = prompt_system.get("prompt_matrix", [])
templates = prompt_system.get("prompt_templates", [])

if matrix:
    st.subheader("Prompt 矩阵")
    platforms = list(dict.fromkeys(c.get("platform", "") for c in matrix))
    platform_tabs = st.tabs(platforms)

    for p_idx, platform in enumerate(platforms):
        with platform_tabs[p_idx]:
            cells = [c for c in matrix if c.get("platform") == platform]
            for cell in cells:
                label = f"{cell.get('direction_id', '')}: {cell.get('direction_name', '')}"
                with st.expander(label):
                    with st.expander("System Prompt", expanded=True):
                        st.code(cell.get("system_prompt", ""), language=None)

                    with st.expander("User Prompt Template"):
                        st.code(cell.get("user_prompt_template", ""), language=None)

                    variables = cell.get("variables", {})
                    if variables:
                        with st.expander("变量说明"):
                            for var_name, var_desc in variables.items():
                                st.markdown(f"- `{{{{{var_name}}}}}`: {var_desc}")

                    rules = cell.get("persona_adaptation_rules", {})
                    if rules:
                        with st.expander("人设适配规则"):
                            st.json(rules)

                    demo = cell.get("demo_output", "")
                    if demo:
                        with st.expander("示例输出"):
                            st.markdown(demo)

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

# Demo outputs section
demos = prompt_system.get("demo_outputs", [])
if demos:
    st.subheader("示例输出")
    for demo in demos:
        with st.expander(f"{demo.get('direction_id', '')} — {demo.get('platform', '')}"):
            st.markdown(f"**人设**: {demo.get('persona_used', '')}")
            st.markdown(demo.get("output_content", ""))

# Usage guide
guide = prompt_system.get("usage_guide", "")
if guide:
    with st.expander("📖 使用指南"):
        st.markdown(guide)

# Uncertainty summary (low-impact residuals — high-impact resolved via clarification upstream)
uncertainty_summary = prompt_system.get("_uncertainty_summary", {})
if uncertainty_summary:
    items = uncertainty_summary.get("items", [])
    checklist = uncertainty_summary.get("data_checklist", [])
    if items or checklist:
        with st.expander("💡 可选优化项 — 补充数据可进一步提升产出质量", expanded=True):
            if items:
                for item in items:
                    st.warning(
                        f"**{item.get('source', '')}** → {item.get('field', '')}\n\n"
                        f"{item.get('reason', '')}\n\n"
                        f"📋 {item.get('data_suggestion', '')}"
                    )
            if checklist:
                st.markdown("**建议补充的数据源：**")
                for c in checklist:
                    st.markdown(f"- {c}")

            st.divider()
            st.markdown("### 📤 补充数据并自动优化")
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
                submitted = st.form_submit_button("🔄 补充并重跑优化")

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

                db.update_project(
                    project_id,
                    free_text=new_free_text,
                    task_type="iteration",
                    status="running",
                )

                from pipeline.orchestrator import start_pipeline_in_background
                from pipeline.agents import init_api_config
                new_run = db.create_pipeline_run(project_id)
                init_api_config()
                start_pipeline_in_background(project_id, new_run["id"], db)

                st.success("已提交补充数据，流水线正在重新优化...")
                st.session_state["current_project_id"] = project_id
                st.query_params["project_id"] = project_id
                st.switch_page("pages/3_pipeline_detail.py")

# Batch rules
batch_rules = prompt_system.get("batch_rules", {})
if batch_rules:
    with st.expander("⚙️ 批量管理规则"):
        st.json(batch_rules)

# ── Export ──────────────────────────────────────────────────────────────────

st.divider()
st.subheader("导出")

col1, col2, col3 = st.columns(3)
with col1:
    md_content = export_as_markdown(prompt_system, project["name"])
    st.download_button(
        "📥 导出 Markdown",
        data=md_content,
        file_name=f"{project['name']}_prompt_system.md",
        mime="text/markdown",
    )
with col2:
    json_content = export_as_json(prompt_system)
    st.download_button(
        "📥 导出 JSON",
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
        "📥 导出完整数据",
        data=json.dumps(full_data, ensure_ascii=False, indent=2),
        file_name=f"{project['name']}_full_output.json",
        mime="application/json",
    )
