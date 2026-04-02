"""新建项目 — 2-tab form: New Product / Iterate & Extend."""

import base64
import io

import streamlit as st

st.set_page_config(page_title="新建项目", page_icon="📝", layout="wide")
st.title("📝 新建 Prompt 工程项目")


# ── File processing helpers ────────────────────────────────────────────────

def extract_file_content(uploaded_file) -> str:
    """Extract text content from an uploaded file."""
    name = uploaded_file.name.lower()

    if name.endswith((".txt", ".md", ".json")):
        return uploaded_file.read().decode("utf-8", errors="replace")

    if name.endswith(".pdf"):
        try:
            from PyPDF2 import PdfReader
            reader = PdfReader(io.BytesIO(uploaded_file.read()))
            return "\n".join(page.extract_text() or "" for page in reader.pages)
        except Exception as e:
            return f"[PDF 解析失败: {e}]"

    if name.endswith(".docx"):
        try:
            from docx import Document
            doc = Document(io.BytesIO(uploaded_file.read()))
            return "\n".join(p.text for p in doc.paragraphs)
        except Exception as e:
            return f"[DOCX 解析失败: {e}]"

    if name.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
        b64 = base64.b64encode(uploaded_file.read()).decode("utf-8")
        return f"[图片文件: {uploaded_file.name}, base64长度: {len(b64)}]\n[BASE64_IMAGE:{b64}]"

    return f"[不支持的文件类型: {uploaded_file.name}]"


def process_uploaded_files(files) -> str:
    """Process multiple uploaded files into a tagged text block."""
    if not files:
        return ""
    parts = []
    for f in files:
        content = extract_file_content(f)
        parts.append(f"\n[参考文件: {f.name}]\n{content}\n[/参考文件]")
    return "\n".join(parts)


# ── Tabs ───────────────────────────────────────────────────────────────────

tab_new, tab_iterate = st.tabs(["🆕 全新产品", "🔄 迭代 / 扩展"])

# ════════════════════════════════════════════════════════════════════════════
# Tab 1: 全新产品
# ════════════════════════════════════════════════════════════════════════════

with tab_new:
    st.markdown("#### 基础信息")
    col1, col2 = st.columns(2)
    with col1:
        product_name = st.text_input("产品名称 *", placeholder="例：XX精华液", key="new_name")
        category = st.text_input("品类", placeholder="例：护肤、食品、3C数码", key="new_cat")
        platforms = st.multiselect(
            "目标平台", ["小红书", "抖音", "微博", "B站", "快手"],
            default=["小红书"], key="new_plat",
        )
    with col2:
        objective = st.selectbox(
            "Campaign 目标",
            ["种草", "搜索占位", "口碑扭转", "新品上市", "品牌认知", "其他"],
            key="new_obj",
        )
        competitor = st.text_input("主要竞品（可选）", placeholder="例：竞品A、竞品B", key="new_comp")
        constraints = st.text_input("约束条件（可选）", placeholder="例：不提价格、不做对比", key="new_cons")

    st.markdown("#### 参考资料")
    files_new = st.file_uploader(
        "上传参考文件（产品资料、竞品分析、聊天截图、老板的语音转文字……什么都行）",
        accept_multiple_files=True,
        type=["pdf", "txt", "md", "docx", "png", "jpg", "jpeg", "gif", "webp", "json"],
        key="new_files",
    )

    free_text_new = st.text_area(
        "补充说明 *",
        height=250,
        placeholder="把所有散乱的信息都丢进来——产品卖点、目标人群、特殊要求、老板的原话、"
                    "聊天记录片段……写得越多越好，太子会帮你整理成结构化 brief。\n\n"
                    "不用担心格式，想到什么写什么。",
        key="new_text",
    )

    if st.button("🚀 启动流水线", type="primary", use_container_width=True, key="btn_new"):
        if not product_name:
            st.error("请填写产品名称。")
        elif not free_text_new:
            st.error("请填写补充说明。")
        else:
            full_text = f"产品名称：{product_name}\n"
            if category:
                full_text += f"品类：{category}\n"
            full_text += f"目标平台：{', '.join(platforms)}\n"
            full_text += f"Campaign目标：{objective}\n"
            if competitor:
                full_text += f"竞品：{competitor}\n"
            if constraints:
                full_text += f"约束条件：{constraints}\n"
            full_text += f"\n{free_text_new}"
            full_text += process_uploaded_files(files_new)

            try:
                from db.supabase_client import SupabaseClient
                from pipeline.orchestrator import start_pipeline_in_background
                db = SupabaseClient.get_instance()
                project = db.create_project(name=product_name, free_text=full_text, task_type="new_system")
                run = db.create_pipeline_run(project["id"])
                start_pipeline_in_background(project["id"], run["id"], db)
                st.success("✅ 流水线已启动！")
                st.query_params["project_id"] = project["id"]
                st.switch_page("pages/3_pipeline_detail.py")
            except Exception as e:
                st.error(f"启动失败：{e}")

# ════════════════════════════════════════════════════════════════════════════
# Tab 2: 迭代 / 扩展
# ════════════════════════════════════════════════════════════════════════════

with tab_iterate:
    source = st.radio(
        "数据来源",
        ["基于已有项目迭代", "上传已有 Prompt 文件（从其他平台迁移）"],
        horizontal=True, key="iter_source",
    )

    is_extend = st.checkbox(
        "📐 扩展新方向（保留原有方向，新增内容角度）", key="iter_extend",
    )
    task_type_iter = "extension" if is_extend else "iteration"

    if source == "基于已有项目迭代":
        # ── Load existing project ──────────────────────────────────────
        try:
            from db.supabase_client import SupabaseClient
            db = SupabaseClient.get_instance()
            projects = db.list_projects(limit=50)
            completed = [p for p in projects if p["status"] == "completed"]
        except Exception:
            completed = []
            st.warning("无法加载历史项目。请检查数据库连接。")

        if completed:
            options = {f"{p['name']} ({p['created_at'][:10]})": p for p in completed}
            selected_label = st.selectbox("选择基础项目", list(options.keys()), key="iter_proj")
            base_project = options[selected_label]
            base_project_id = base_project["id"]

            # Show inherited brief as read-only
            brief = base_project.get("brief") or {}
            if brief:
                st.info(
                    f"**继承信息** — 产品：{brief.get('product_name', '?')} · "
                    f"品类：{brief.get('product_category', '?')} · "
                    f"平台：{', '.join(brief.get('target_platforms', []))} · "
                    f"目标：{brief.get('campaign_objective', '?')}"
                )
                with st.expander("查看完整 brief"):
                    st.json(brief)
            else:
                st.warning("该项目没有结构化 brief 数据。")

            iter_notes = st.text_area(
                "需要改什么 *",
                height=200,
                placeholder="描述你对现有 prompt 不满意的点、需要调整的方向、新增的要求……",
                key="iter_notes",
            )

            files_iter = st.file_uploader(
                "上传补充参考文件（可选）",
                accept_multiple_files=True,
                type=["pdf", "txt", "md", "docx", "png", "jpg", "jpeg", "json"],
                key="iter_files",
            )

            if st.button("🚀 启动迭代流水线", type="primary", use_container_width=True, key="btn_iter"):
                if not iter_notes:
                    st.error("请填写需要改什么。")
                else:
                    full_text = f"[继承自项目: {base_project['name']}]\n"
                    if brief:
                        full_text += f"[已有Brief]\n{__import__('json').dumps(brief, ensure_ascii=False, indent=2)}\n[/已有Brief]\n\n"
                    action = "扩展新方向" if is_extend else "迭代优化"
                    full_text += f"[{action}说明]\n{iter_notes}\n"
                    full_text += process_uploaded_files(files_iter)

                    try:
                        project = db.create_project(
                            name=f"{base_project['name']}（{'扩展' if is_extend else '迭代'}）",
                            free_text=full_text,
                            task_type=task_type_iter,
                            base_project_id=base_project_id,
                        )
                        run = db.create_pipeline_run(project["id"])
                        from pipeline.orchestrator import start_pipeline_in_background
                        start_pipeline_in_background(project["id"], run["id"], db)
                        st.success("✅ 流水线已启动！")
                        st.query_params["project_id"] = project["id"]
                        st.switch_page("pages/3_pipeline_detail.py")
                    except Exception as e:
                        st.error(f"启动失败：{e}")
        else:
            st.info("暂无已完成的项目可供迭代。请先创建一个全新项目。")

    else:
        # ── Upload existing prompt ─────────────────────────────────────
        st.markdown("#### 上传已有 Prompt")
        prompt_files = st.file_uploader(
            "上传你的 Prompt 文件",
            accept_multiple_files=True,
            type=["txt", "md", "json"],
            key="prompt_files",
        )

        st.markdown("#### 基础信息（迁移项目需要填写）")
        col1, col2 = st.columns(2)
        with col1:
            m_name = st.text_input("产品名称 *", key="mig_name")
            m_cat = st.text_input("品类", key="mig_cat")
            m_plat = st.multiselect("目标平台", ["小红书", "抖音", "微博", "B站", "快手"], key="mig_plat")
        with col2:
            m_obj = st.selectbox("Campaign 目标", ["种草", "搜索占位", "口碑扭转", "新品上市", "其他"], key="mig_obj")
            m_comp = st.text_input("竞品（可选）", key="mig_comp")

        m_notes = st.text_area(
            "这套 Prompt 的问题和改进方向 *",
            height=200,
            placeholder="描述现有 prompt 存在的问题、需要优化的方面、你期望的改进方向……",
            key="mig_notes",
        )

        if st.button("🚀 启动迭代流水线", type="primary", use_container_width=True, key="btn_mig"):
            if not m_name:
                st.error("请填写产品名称。")
            elif not prompt_files:
                st.error("请上传 Prompt 文件。")
            elif not m_notes:
                st.error("请描述问题和改进方向。")
            else:
                full_text = f"产品名称：{m_name}\n"
                if m_cat:
                    full_text += f"品类：{m_cat}\n"
                if m_plat:
                    full_text += f"目标平台：{', '.join(m_plat)}\n"
                full_text += f"Campaign目标：{m_obj}\n"
                if m_comp:
                    full_text += f"竞品：{m_comp}\n"

                # Attach prompt files
                for pf in prompt_files:
                    content = pf.read().decode("utf-8", errors="replace")
                    full_text += f"\n[已有Prompt文件: {pf.name}]\n{content}\n[/已有Prompt文件]\n"

                full_text += f"\n[改进方向]\n{m_notes}\n"

                try:
                    from db.supabase_client import SupabaseClient
                    from pipeline.orchestrator import start_pipeline_in_background
                    db = SupabaseClient.get_instance()
                    project = db.create_project(name=m_name, free_text=full_text, task_type=task_type_iter)
                    run = db.create_pipeline_run(project["id"])
                    start_pipeline_in_background(project["id"], run["id"], db)
                    st.success("✅ 流水线已启动！")
                    st.query_params["project_id"] = project["id"]
                    st.switch_page("pages/3_pipeline_detail.py")
                except Exception as e:
                    st.error(f"启动失败：{e}")
