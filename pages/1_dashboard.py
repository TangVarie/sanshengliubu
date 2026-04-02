"""项目总览 — Dashboard page showing all projects and their status."""

import streamlit as st
from db.supabase_client import SupabaseClient

st.set_page_config(page_title="项目总览", page_icon="📋", layout="wide")
st.title("📋 项目总览")

STATUS_EMOJI = {
    "draft": "📝",
    "running": "🔄",
    "completed": "✅",
    "failed": "❌",
}

TASK_TYPE_LABEL = {
    "new_system": "🆕 全新系统",
    "iteration": "🔄 迭代优化",
    "extension": "📐 扩展方向",
}

try:
    db = SupabaseClient.get_instance()
    projects = db.list_projects(limit=50)

    if not projects:
        st.info("暂无项目，点击左侧「新建项目」开始。")
    else:
        # Quick stats
        col1, col2, col3, col4 = st.columns(4)
        total = len(projects)
        running = sum(1 for p in projects if p["status"] == "running")
        completed = sum(1 for p in projects if p["status"] == "completed")
        failed = sum(1 for p in projects if p["status"] == "failed")

        col1.metric("总项目数", total)
        col2.metric("运行中", running)
        col3.metric("已完成", completed)
        col4.metric("失败", failed)

        st.divider()

        # Project list
        for project in projects:
            pid = project["id"]
            status = project.get("status", "draft")
            emoji = STATUS_EMOJI.get(status, "❓")
            task_label = TASK_TYPE_LABEL.get(project.get("task_type", ""), "")
            created = project.get("created_at", "")[:16].replace("T", " ")

            with st.container(border=True):
                c1, c2, c3, c4 = st.columns([3, 2, 2, 1])
                with c1:
                    st.markdown(f"### {emoji} {project['name']}")
                with c2:
                    st.caption(f"类型：{task_label}")
                    st.caption(f"创建：{created}")
                with c3:
                    st.caption(f"状态：**{status}**")
                with c4:
                    if st.button("查看详情", key=f"view_{pid}"):
                        st.session_state["current_project_id"] = pid
                        st.query_params["project_id"] = pid
                        st.switch_page("pages/3_pipeline_detail.py")

        # Auto-refresh if any project is running
        if running > 0:
            import time
            time.sleep(5)
            st.rerun()

except Exception as e:
    st.error(f"无法连接数据库。请在「设置」页面配置 Supabase 连接。\n\n错误：{e}")
    st.info("如果还没有设置 Supabase，请参考 README.md 中的设置指南。")
