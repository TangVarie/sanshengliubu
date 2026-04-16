"""新建项目 — 2-tab form: New Product / Iterate & Extend."""

import base64
import io

import streamlit as st
from utils.version_badge import show_version_badge

st.set_page_config(page_title="新建项目", page_icon="📝", layout="wide")
show_version_badge()
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
    # ── Prefill from "复制并修改" (page 3 → session_state) ────────────
    _prefill = st.session_state.pop("prefill_from_project", None) or {}
    if _prefill:
        st.info(
            "📋 **已从上一个项目复制了输入**——下面各字段已预填，改你想改的部分然后点「启动」。"
        )

    st.markdown("#### 基础信息")
    col1, col2 = st.columns(2)
    with col1:
        product_name = st.text_input(
            "产品名称 *", placeholder="例：XX精华液", key="new_name",
            value=_prefill.get("product_name", ""),
        )
        category = st.text_input(
            "品类", placeholder="例：护肤、食品、3C数码", key="new_cat",
            value=_prefill.get("product_category", ""),
        )
        _default_plat = _prefill.get("target_platforms") or ["小红书"]
        # Ensure prefill platforms are in the option list
        _plat_options = ["小红书", "抖音", "微博", "B站", "快手"]
        for _p in _default_plat:
            if _p not in _plat_options:
                _plat_options.append(_p)
        platforms = st.multiselect(
            "目标平台", _plat_options,
            default=_default_plat, key="new_plat",
        )
    with col2:
        _default_obj = _prefill.get("campaign_objective") or ["种草"]
        if isinstance(_default_obj, str):
            _default_obj = [_default_obj]
        _obj_options = ["种草", "搜索占位", "口碑扭转", "新品上市", "品牌认知", "其他"]
        for _o in _default_obj:
            if _o not in _obj_options:
                _obj_options.append(_o)
        objective = st.multiselect(
            "Campaign 目标（可多选）", _obj_options,
            default=_default_obj, key="new_obj",
        )
        competitor = st.text_input(
            "主要竞品（可选）", placeholder="例：竞品A、竞品B", key="new_comp",
            value=_prefill.get("competitive_context", ""),
        )
        constraints = st.text_input(
            "约束条件（可选）", placeholder="例：不提价格、不做对比", key="new_cons",
            value=_prefill.get("constraints", ""),
        )

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
        value=_prefill.get("free_text", ""),
    )

    # ── Reference samples (screenshot-first) ─────────────────────
    st.markdown("#### 📸 参考爆文（可选但强烈推荐）")
    st.caption(
        "上传小红书截图是**最靠谱**的参考方式——Gemini Vision 直接读图提取标题/正文/钩子结构。"
        "URL 因为鉴权问题大概率拿不到内容，所以截图优先。"
    )

    ref_screenshots = st.file_uploader(
        "上传小红书帖子截图（多选，最多 10 张）",
        accept_multiple_files=True,
        type=["png", "jpg", "jpeg", "webp"],
        key="new_ref_screenshots",
        help="在小红书 App 里截完整帖子页面，拖进来即可。Gemini 自动 OCR + 分析钩子结构。",
    )

    ref_pasted_text = st.text_area(
        "或者直接粘贴复制的正文（App 里长按→复制）",
        height=80,
        placeholder="从小红书复制的帖子正文，直接粘贴在这里……",
        key="new_ref_pasted",
    )

    # Library: select from previously analyzed samples
    try:
        from db.supabase_client import SupabaseClient as _SC
        _db_for_lib = _SC.get_instance()
        _existing_samples = _db_for_lib.list_reference_samples(limit=20)
    except Exception:
        _existing_samples = []

    _selected_sample_ids: list[str] = []
    if _existing_samples:
        with st.expander(f"📚 从样本库选（已有 {len(_existing_samples)} 条历史样本）"):
            for _s in _existing_samples:
                _s_title = _s.get("title", "未命名")
                _s_type = _s.get("source_type", "?")
                _s_date = str(_s.get("created_at", ""))[:10]
                _checked = st.checkbox(
                    f"{_s_title}（{_s_type}，{_s_date}）",
                    key=f"sample_{_s.get('id')}",
                )
                if _checked:
                    _selected_sample_ids.append(_s["id"])

    with st.expander("🔗 URL 输入（次要，大概率只能拿到标题）", expanded=False):
        _prefill_ref_urls = "\n".join(_prefill.get("reference_urls") or [])
        ref_urls_new = st.text_area(
            "贴小红书帖子 URL，每行一个。最多 10 条。",
            height=80,
            placeholder="https://www.xiaohongshu.com/explore/xxx\nhttps://www.xiaohongshu.com/explore/yyy",
            value=_prefill_ref_urls,
            key="new_ref_urls",
            help="小红书页面需要登录，Gemini 大概率只能拿到 og:title。截图更靠谱。",
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
            full_text += f"Campaign目标：{', '.join(objective)}\n"
            if competitor:
                full_text += f"竞品：{competitor}\n"
            if constraints:
                full_text += f"约束条件：{constraints}\n"
            full_text += f"\n{free_text_new}"
            full_text += process_uploaded_files(files_new)

            # Reference pasted text → append to full_text
            if ref_pasted_text and ref_pasted_text.strip():
                full_text += f"\n\n[用户粘贴的参考帖子正文]\n{ref_pasted_text.strip()}\n[/参考帖子正文]"

            # Parse user-pasted reference URLs.
            _ref_urls: list[str] = []
            try:
                for _raw in (ref_urls_new or "").splitlines():
                    _u = _raw.strip()
                    if "xiaohongshu.com" in _u and _u.startswith("http"):
                        _ref_urls.append(_u)
            except Exception:
                pass
            _ref_urls = _ref_urls[:10]

            # Collect screenshot images for Gemini Vision analysis.
            # Store raw bytes + mime so orchestrator can pass them to
            # the screenshot analyzer agent after project creation.
            _screenshot_data: list[tuple[bytes, str]] = []
            if ref_screenshots:
                for _f in ref_screenshots[:10]:
                    try:
                        _bytes = _f.read()
                        _name = _f.name.lower()
                        if _name.endswith(".png"):
                            _mime = "image/png"
                        elif _name.endswith((".jpg", ".jpeg")):
                            _mime = "image/jpeg"
                        elif _name.endswith(".webp"):
                            _mime = "image/webp"
                        else:
                            _mime = "image/png"
                        _screenshot_data.append((_bytes, _mime))
                    except Exception:
                        pass

            # Collect selected library samples
            _library_analyses: list[dict] = []
            if _selected_sample_ids:
                try:
                    _sel_samples = _db_for_lib.get_reference_samples_by_ids(
                        _selected_sample_ids
                    )
                    for _ss in _sel_samples:
                        _a = _ss.get("analysis")
                        if isinstance(_a, dict):
                            _library_analyses.append(_a)
                        # Also append content_text to full_text
                        _ct = _ss.get("content_text")
                        if _ct:
                            full_text += f"\n\n[历史样本: {_ss.get('title', '?')}]\n{_ct}\n[/历史样本]"
                except Exception:
                    pass

            try:
                from db.supabase_client import SupabaseClient
                from pipeline.orchestrator import start_pipeline_in_background
                from pipeline.agents import init_api_config
                db = SupabaseClient.get_instance()
                project = db.create_project(name=product_name, free_text=full_text, task_type="new_system")
                # Persist URLs + screenshot analysis + library analyses on brief
                _brief_init: dict = {}
                if _ref_urls:
                    _brief_init["_reference_post_urls"] = _ref_urls
                if _library_analyses:
                    _brief_init["_library_sample_analyses"] = _library_analyses

                # Run Gemini screenshot analysis if images were uploaded.
                # This happens SYNCHRONOUSLY before pipeline starts so the
                # results are available to crown_prince from the very first
                # stage. Advisory: if Gemini fails, we skip and proceed.
                _screenshot_analysis: dict = {}
                if _screenshot_data:
                    with st.spinner(
                        f"📸 Gemini 正在分析 {len(_screenshot_data)} 张截图..."
                    ):
                        try:
                            import asyncio
                            from pipeline.agents.gemini_screenshot_analyzer import (
                                analyze_screenshots,
                                format_analysis_for_brief,
                            )
                            init_api_config()
                            _screenshot_analysis = asyncio.run(
                                analyze_screenshots(
                                    _screenshot_data,
                                    supplementary_text=ref_pasted_text or "",
                                )
                            )
                            if _screenshot_analysis.get("verdict") == "ok":
                                _brief_init["_screenshot_analysis"] = _screenshot_analysis
                                _brief_init["_screenshot_analysis_text"] = (
                                    format_analysis_for_brief(_screenshot_analysis)
                                )
                                # Save each screenshot to library for reuse
                                for _ss in _screenshot_analysis.get("screenshots") or []:
                                    try:
                                        db.save_reference_sample(
                                            title=_ss.get("extracted_title", "截图样本"),
                                            source_type="screenshot",
                                            content_text=_ss.get("extracted_first_line", ""),
                                            analysis=_ss,
                                        )
                                    except Exception:
                                        pass  # library save is optional
                                st.success(
                                    f"📸 截图分析完成：{len(_screenshot_analysis.get('screenshots', []))} 张"
                                )
                            else:
                                st.warning(
                                    f"📸 截图分析跳过：{_screenshot_analysis.get('_skip_reason', '?')}"
                                )
                        except Exception as _err:
                            st.warning(f"📸 截图分析异常（不影响流水线）：{_err}")

                if _brief_init:
                    db.update_project(project["id"], brief=_brief_init)

                run = db.create_pipeline_run(project["id"])
                if not _screenshot_data:
                    # Only init if not already done during screenshot analysis
                    init_api_config()
                start_pipeline_in_background(project["id"], run["id"], db)

                _msgs = ["✅ 流水线已启动！"]
                if _ref_urls:
                    _msgs.append(f"已记录 {len(_ref_urls)} 条 URL。")
                if _screenshot_data:
                    _msgs.append(f"已分析 {len(_screenshot_data)} 张截图。")
                if _selected_sample_ids:
                    _msgs.append(f"已加载 {len(_selected_sample_ids)} 条历史样本。")
                st.success(" ".join(_msgs))
                st.session_state["current_project_id"] = project["id"]
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
                    f"目标：{', '.join(brief['campaign_objective']) if isinstance(brief.get('campaign_objective'), list) else brief.get('campaign_objective', '?')}"
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
                        from pipeline.agents import init_api_config
                        init_api_config()
                        start_pipeline_in_background(project["id"], run["id"], db)
                        st.success("✅ 流水线已启动！")
                        st.session_state["current_project_id"] = project["id"]
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
            m_obj = st.multiselect("Campaign 目标（可多选）", ["种草", "搜索占位", "口碑扭转", "新品上市", "品牌认知", "其他"], default=["种草"], key="mig_obj")
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
                full_text += f"Campaign目标：{', '.join(m_obj)}\n"
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
                    from pipeline.agents import init_api_config
                    db = SupabaseClient.get_instance()
                    project = db.create_project(name=m_name, free_text=full_text, task_type=task_type_iter)
                    run = db.create_pipeline_run(project["id"])
                    init_api_config()
                    start_pipeline_in_background(project["id"], run["id"], db)
                    st.success("✅ 流水线已启动！")
                    st.session_state["current_project_id"] = project["id"]
                    st.query_params["project_id"] = project["id"]
                    st.switch_page("pages/3_pipeline_detail.py")
                except Exception as e:
                    st.error(f"启动失败：{e}")
