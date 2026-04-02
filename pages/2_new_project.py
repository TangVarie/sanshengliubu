"""新建项目 — Brief submission form to start a pipeline."""

import streamlit as st
from db.supabase_client import SupabaseClient
from pipeline.orchestrator import start_pipeline_in_background

st.set_page_config(page_title="新建项目", page_icon="📝", layout="wide")
st.title("📝 新建 Prompt 工程项目")

# Task type
task_type = st.radio(
    "任务类型",
    ["🆕 全新产品", "🔄 迭代现有 Prompt", "📐 扩展新方向"],
    horizontal=True,
)
task_type_map = {
    "🆕 全新产品": "new_system",
    "🔄 迭代现有 Prompt": "iteration",
    "📐 扩展新方向": "extension",
}
task_type_value = task_type_map[task_type]

# Structured fields
col1, col2 = st.columns(2)
with col1:
    product_name = st.text_input("产品名称 *", placeholder="例：XX精华液")
    category = st.text_input("品类", placeholder="例：护肤、食品、3C数码")
    platforms = st.multiselect(
        "目标平台",
        ["小红书", "抖音", "微博", "B站", "快手"],
        default=["小红书"],
    )
with col2:
    objective = st.selectbox(
        "Campaign 目标",
        ["种草", "搜索占位", "口碑扭转", "新品上市", "品牌认知", "其他"],
    )
    competitor = st.text_input("主要竞品（可选）", placeholder="例：竞品A、竞品B")
    constraints = st.text_input("约束条件（可选）", placeholder="例：不提价格、不做对比")

# Free text
free_text = st.text_area(
    "补充说明 *（产品卖点、目标人群、特殊要求等）",
    height=200,
    placeholder="自由描述你的产品和需求，写得越详细越好。\n\n例如：\n我们是一款针对25-35岁都市女性的抗衰精华，核心卖点是'28天可见紧致'...",
)

# Iteration mode
base_project_id = None
if task_type_value == "iteration":
    st.divider()
    st.subheader("迭代设置")
    try:
        db = SupabaseClient.get_instance()
        existing = db.list_projects(limit=20)
        completed = [p for p in existing if p["status"] == "completed"]
        if completed:
            options = {p["name"]: p["id"] for p in completed}
            selected = st.selectbox("基于哪个项目迭代", list(options.keys()))
            base_project_id = options[selected]
        else:
            st.warning("暂无已完成的项目可供迭代。")
    except Exception:
        st.warning("无法加载历史项目列表。")

    st.text_area("哪里不满意，需要改什么", key="iteration_notes", height=100)

# Submit
st.divider()
if st.button("🚀 启动流水线", type="primary", use_container_width=True):
    if not product_name:
        st.error("请填写产品名称。")
    elif not free_text:
        st.error("请填写补充说明。")
    else:
        # Build the full brief text
        full_text = f"产品名称：{product_name}\n"
        if category:
            full_text += f"品类：{category}\n"
        full_text += f"目标平台：{', '.join(platforms)}\n"
        full_text += f"Campaign目标：{objective}\n"
        if competitor:
            full_text += f"竞品：{competitor}\n"
        if constraints:
            full_text += f"约束条件：{constraints}\n"
        full_text += f"\n{free_text}"

        if task_type_value == "iteration" and "iteration_notes" in st.session_state:
            full_text += f"\n\n迭代说明：{st.session_state.iteration_notes}"

        try:
            db = SupabaseClient.get_instance()

            # Create project
            project = db.create_project(
                name=product_name,
                free_text=full_text,
                task_type=task_type_value,
                base_project_id=base_project_id,
            )
            project_id = project["id"]

            # Create pipeline run
            run = db.create_pipeline_run(project_id)
            run_id = run["id"]

            # Launch pipeline in background
            start_pipeline_in_background(project_id, run_id, db)

            st.success("✅ 流水线已启动！正在跳转...")
            st.query_params["project_id"] = project_id
            st.switch_page("pages/3_pipeline_detail.py")

        except Exception as e:
            st.error(f"启动失败：{e}")
