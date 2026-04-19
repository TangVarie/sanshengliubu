"""流水线详情 — Real-time pipeline progress and stage outputs."""

import base64
import json
import re
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


def _sanitize_freetext_for_display(text: str) -> str:
    """折叠 free_text 里的 base64 图片原文,便于人阅读。

    v0.26.0 之前的项目把整张图的 base64 直接塞进 free_text,单张图就能
    吃掉几百行"乱码"。新数据走 Gemini Vision OCR 不会有,但历史项目
    每次 re-run 都继承老 free_text → base64 一直在。
    这里只动显示,不动存储:实际喂给 agent 的 input_data 保持原样。
    """
    if not text:
        return text
    # [BASE64_IMAGE:xxxx...] 块:v0.26.0 前的原始 base64 占位
    text = re.sub(
        r"\[BASE64_IMAGE:([A-Za-z0-9+/=]{40,})\]",
        lambda m: f"[📎 图片 base64 已折叠 · {len(m.group(1)):,} 字符]",
        text,
    )
    # 极长的裸 base64(没有包装标签)也折叠 — 超过 500 字符的连续
    # base64 字符视为图片残留。
    text = re.sub(
        r"(?<![A-Za-z0-9+/=])([A-Za-z0-9+/=]{500,})(?![A-Za-z0-9+/=])",
        lambda m: f"[📎 未包装 base64 已折叠 · {len(m.group(1)):,} 字符]",
        text,
    )
    return text


def _extract_file_summary_from_freetext(text: str) -> list[dict]:
    """扫 free_text 找出所有已接收的文件及其状态。

    统一的外层包装是 page 2 的 process_uploaded_files 产的
    `[参考文件: name]...[/参考文件]` 块,包括 txt/md/json/pdf/docx/图片
    所有类型。从 body 内容推断具体 kind + status:
    - 内含 `[图片AI识别:` → Gemini OCR 成功的图片
    - 内含 `[图片占位符:` → Gemini 未配置/失败的图片占位
    - 内含 `[BASE64_IMAGE:` 或 `base64长度:` → v0.26 前的 base64 残留
    - 都不含 → 普通文本/Word/PDF 提取文本

    另外扫 `[历史样本: X]...[/历史样本]`(样本库插入,走另一条路径)。
    返回 [{filename, kind, status, size_hint, preview}]。
    """
    if not text:
        return []
    files: list[dict] = []

    # 主:外层 [参考文件:...] 块 — txt/md/pdf/docx/图片全走这条
    for m in re.finditer(
        r"\[参考文件:\s*([^\]]+?)\]\n(.*?)(?=\n\[/参考文件\]|$)",
        text, flags=re.DOTALL,
    ):
        filename = (m.group(1) or "").strip()
        body = (m.group(2) or "").strip()

        # 按扩展名 + body 内容推断类型
        _ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if "[图片AI识别:" in body:
            kind = "图片(Gemini OCR)"
            status = "transcribed"
        elif "[图片占位符:" in body:
            kind = "图片(占位符)"
            status = "placeholder_only"
        elif "[BASE64_IMAGE:" in body or "base64长度:" in body:
            kind = "图片(旧 base64)"
            status = "legacy_base64"
        elif _ext == "pdf":
            kind = "PDF"
            status = "extracted"
        elif _ext == "docx":
            kind = "Word"
            status = "extracted"
        elif _ext in ("txt", "md"):
            kind = "文本"
            status = "extracted"
        elif _ext == "json":
            kind = "JSON"
            status = "extracted"
        elif _ext in ("png", "jpg", "jpeg", "gif", "webp"):
            # 图片扩展名但没命中上面几种 body 标记 — 罕见,可能是
            # 早期未处理状态或新格式。
            kind = "图片(未知格式)"
            status = "unknown"
        else:
            kind = "其他"
            status = "extracted"

        # 空 body 提示(上传成功但解析失败也算一种状态)
        if not body and status == "extracted":
            status = "empty"

        files.append({
            "filename": filename,
            "kind": kind,
            "status": status,
            "size_hint": len(body),
            "preview": _sanitize_freetext_for_display(body)[:180],
        })

    # 历史样本(样本库插入,不经 process_uploaded_files 包装)
    for m in re.finditer(
        r"\[历史样本:\s*([^\]]+?)\]\n(.*?)(?=\n\[/历史样本\]|$)",
        text, flags=re.DOTALL,
    ):
        filename = (m.group(1) or "").strip()
        body = (m.group(2) or "").strip()
        files.append({
            "filename": filename,
            "kind": "历史样本",
            "status": "extracted",
            "size_hint": len(body),
            "preview": _sanitize_freetext_for_display(body)[:180],
        })

    return files


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

# ── A: Original input viewer ─────────────────────────────────────────────
# Let the user see exactly what this pipeline was built from — free_text,
# structured brief, platforms, reference URLs, etc. Solves "我当初填了什么".
_free_text = project.get("free_text", "")
_brief = project.get("brief") or {}
with st.expander("📋 原始输入（查看 / 复用）", expanded=False):
    if _free_text:
        st.markdown("**你当初提交的原文：**")
        st.text_area(
            "free_text（只读）",
            value=_free_text,
            height=150,
            disabled=True,
            label_visibility="collapsed",
        )
    else:
        st.caption("（free_text 为空——可能是通过 API 直接创建的项目）")

    if _brief:
        # Show key fields from structured brief in readable format
        _brief_highlights = []
        for _k in (
            "product_name", "product_category", "target_audience",
            "target_platforms", "campaign_objective", "core_claim",
            "competitive_context", "constraints", "task_type",
        ):
            _v = _brief.get(_k)
            if _v:
                _brief_highlights.append(f"- **{_k}**: {_v}")
        if _brief_highlights:
            st.markdown("**结构化 Brief（太子解析结果）：**")
            st.markdown("\n".join(_brief_highlights))

        # Reference posts / trend intel if present
        _ref_posts = (_brief.get("_reference_posts") or {}).get("posts") or []
        if _ref_posts:
            st.markdown(f"**参考帖子**（{len(_ref_posts)} 条）：")
            for _rp in _ref_posts[:5]:
                st.caption(f"- {_rp.get('title', '?')} → {_rp.get('url', '')}")

        _ref_urls = _brief.get("_reference_post_urls") or []
        if _ref_urls:
            st.markdown(f"**用户贴的参考 URL**（{len(_ref_urls)} 条）：")
            for _ru in _ref_urls:
                st.caption(f"- {_ru}")

        with st.expander("🔍 完整 Brief JSON", expanded=False):
            st.json(_brief)

    # B: Copy-and-modify button
    if st.button(
        "📋 复制并修改（基于此项目新建）",
        help="把这个项目的原始输入复制到「新建项目」页面，预填所有字段，你只需改你想改的部分。",
    ):
        st.session_state["prefill_from_project"] = {
            "product_name": _brief.get("product_name", project.get("name", "")),
            "product_category": _brief.get("product_category", ""),
            "target_platforms": _brief.get("target_platforms", []),
            "campaign_objective": _brief.get("campaign_objective", []),
            "competitive_context": _brief.get("competitive_context", ""),
            "constraints": _brief.get("constraints", ""),
            "free_text": _free_text,
            "reference_urls": _brief.get("_reference_post_urls", []),
        }
        st.switch_page("pages/2_new_project.py")

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
    "red_blue_refiner": "红蓝精炼",
    "persona_simulator": "画像模拟",
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
            # Size caps match pages/2_new_project.py::extract_file_content.
            # st.file_uploader's `type=` arg is a UI hint only; clients can
            # post anything, so the cap is enforced here.
            _MAX_IMAGE_BYTES = 2 * 1024 * 1024
            _MAX_TEXT_BYTES = 1 * 1024 * 1024
            if uploaded_files:
                # 图片走 Gemini Vision 预转写,与 pages/2_new_project.py 一致。
                # 旧版 base64 占位符让下游 LLM 看不见图,人工补充等于白补。
                from pipeline.agents.gemini_image_transcriber import (
                    transcribe_image_for_brief,
                )
                _ext_to_mime = {
                    ".png": "image/png", ".jpg": "image/jpeg",
                    ".jpeg": "image/jpeg", ".gif": "image/gif",
                    ".webp": "image/webp",
                }
                _has_imgs = any(
                    f.name.lower().endswith(tuple(_ext_to_mime))
                    for f in uploaded_files
                )

                def _process_one(f):
                    try:
                        name = f.name.lower()
                        is_image = name.endswith(tuple(_ext_to_mime))
                        cap = _MAX_IMAGE_BYTES if is_image else _MAX_TEXT_BYTES
                        size = getattr(f, "size", None)
                        if size is not None and size > cap:
                            return f"[补充文件: {f.name}] 过大跳过 > {cap // 1024 // 1024 or 1}MB"
                        data = f.read()
                        if len(data) > cap:
                            return f"[补充文件: {f.name}] 过大跳过 > {cap // 1024 // 1024 or 1}MB"
                        if is_image:
                            mime = next(
                                (m for ext, m in _ext_to_mime.items() if name.endswith(ext)),
                                "image/png",
                            )
                            return f"[补充图片: {f.name}]\n{transcribe_image_for_brief(data, mime, f.name)}"
                        return (
                            f"[补充文件: {f.name}]\n"
                            f"{data.decode('utf-8', errors='replace')}"
                        )
                    except Exception:
                        return f"[补充文件: {f.name}]（无法读取）"

                file_texts: list[str] = []
                if _has_imgs:
                    with st.spinner(
                        f"📷 正在用 Gemini Vision 转写补充图片(每张 ~5-15s)..."
                    ):
                        for f in uploaded_files:
                            file_texts.append(_process_one(f))
                else:
                    for f in uploaded_files:
                        file_texts.append(_process_one(f))
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

    # Strategy debate: secretariat and chancellery log under
    # strategy_debate_0 (secretariat), strategy_debate_1 (chancellery), etc.
    # Match the latest debate turn for each agent.
    if not log and stage_key == "secretariat":
        for k in sorted(log_map, reverse=True):
            if k.startswith("strategy_debate_") and int(k.rsplit("_", 1)[-1]) % 2 == 0:
                log = log_map[k]
                break
    if not log and stage_key == "chancellery":
        for k in sorted(log_map, reverse=True):
            if k.startswith("strategy_debate_") and int(k.rsplit("_", 1)[-1]) % 2 == 1:
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
# v0.29.9: 新增"中间精炼"tab(叙事导演/红蓝精炼/画像模拟),之前这三个
# 阶段跑了但 UI 完全没位置展示,图标染色了点进去也看不到内容。
tab_names = [
    "太子", "中书省", "门下省", "尚书省", "六部",
    "中间精炼", "网感", "终审",
]
tabs = st.tabs(tab_names)

# Tab 0: Crown Prince
with tabs[0]:
    log = log_map.get("crown_prince")
    render_stage_meta(log)

    # v0.29.9: 上传文件清单 — 不管太子跑没跑,先把 project.free_text 里
    # 识别到的参考文件列出来,让用户一眼知道"我传的 N 个文件系统收到没、
    # 解析出了什么"。解决原来要翻几百行 base64 才知道有没有收到的问题。
    try:
        _proj_for_files = db.get_project(project_id) or {}
        _proj_free_text = _proj_for_files.get("free_text", "") or ""
        _files_received = _extract_file_summary_from_freetext(_proj_free_text)
        if _files_received:
            _status_icons = {
                "extracted": "✅",
                "transcribed": "🔍",
                "placeholder_only": "⚠️",
                "legacy_base64": "📦",
                "empty": "❌",
                "unknown": "❓",
            }
            _status_labels = {
                "extracted": "已提取文本",
                "transcribed": "Gemini 已转写",
                "placeholder_only": "仅占位符(未转写)",
                "legacy_base64": "老版 base64 残留",
                "empty": "body 为空(提取失败?)",
                "unknown": "未知格式",
            }
            with st.expander(
                f"📎 接收到的参考文件({len(_files_received)} 个)",
                expanded=True,
            ):
                st.caption(
                    "这里列出 project.free_text 里识别到的所有文件及其处理状态。"
                    "base64 残留来自 v0.26 之前的老数据,每次 re-run 会继承。"
                    "只想看『收到没』就看这个表,不用翻下面的正文。"
                )
                for _fi in _files_received:
                    _icon = _status_icons.get(_fi["status"], "❓")
                    _label = _status_labels.get(_fi["status"], _fi["status"])
                    st.markdown(
                        f"{_icon} **{_fi['filename']}** · _{_fi['kind']}_ · "
                        f"`{_label}` · {_fi['size_hint']:,} 字"
                    )
                    if _fi["preview"]:
                        st.caption(f"预览:{_fi['preview']}…")
    except Exception as _fs_err:
        st.caption(f"(文件清单解析失败:{type(_fs_err).__name__})")

    if log and log.get("output_data"):
        # ── 留存率诊断(v0.26.0)──
        # 用户喂的 raw 内容(free_text 里的 [参考文件: ...] 块)在
        # 太子 raw_materials + reference_summary 里实际保留了多少。
        # 留存率太低意味着模型在偷懒概括——见 crown_prince.md 第 4 条
        # 硬约束。这一行单独显示,让丢内容问题不再隐藏。
        try:
            import re as _re_diag
            _input_data = log.get("input_data") or {}
            _input_free_text = (
                _input_data.get("free_text")
                or (_input_data.get("brief") or {}).get("free_text")
                or ""
            )
            _ref_blocks = _re_diag.findall(
                r"\[参考文件:\s*[^\]]*\]\n(.*?)\n\[/参考文件\]",
                _input_free_text,
                flags=_re_diag.DOTALL,
            )
            _ref_chars = sum(len(b) for b in _ref_blocks)
            _out = log["output_data"] or {}
            _kept_chars = (
                len(str(_out.get("raw_materials") or ""))
                + len(str(_out.get("reference_summary") or ""))
            )
            if _ref_chars > 0:
                _ratio = _kept_chars / _ref_chars
                _pct = f"{_ratio * 100:.0f}%"
                if _ratio >= 0.6:
                    st.success(
                        f"📊 参考文件留存率:**{_pct}** "
                        f"(原始 {_ref_chars:,} 字 → 太子保留 {_kept_chars:,} 字,"
                        f"≥60% 阈值,健康)"
                    )
                elif _ratio >= 0.3:
                    st.warning(
                        f"📊 参考文件留存率:**{_pct}** "
                        f"(原始 {_ref_chars:,} 字 → 太子保留 {_kept_chars:,} 字,"
                        f"低于 60% 阈值——下游六部可能拿不到完整素材)"
                    )
                else:
                    st.error(
                        f"📊 参考文件留存率:**{_pct}** "
                        f"(原始 {_ref_chars:,} 字 → 太子只保留 {_kept_chars:,} 字,"
                        f"严重偏低——大量素材已被概括丢失)"
                    )
            elif _input_free_text:
                st.caption(
                    f"📊 太子接收 free_text {len(_input_free_text):,} 字 → "
                    f"raw_materials + reference_summary 输出 {_kept_chars:,} 字"
                )
        except Exception as _diag_err:
            st.caption(f"(留存率诊断失败:{_diag_err})")

        render_stage_output(log["output_data"])

        # ── 完整 input_data 折叠面板(诊断丢内容用)──
        # 让用户能直接对比"我喂了什么"vs"模型看到了什么"vs"模型输出了什么"。
        # v0.29.8: db.get_stage_logs() 为了减 payload 显式不 SELECT input_data
        # 列,所以 log.get("input_data") 永远是 None,UI 之前一直显示"为空"。
        # 这里点开面板时按 log_id 单独 SELECT("*") 把 input_data 拉回来,
        # 结果缓存在 session_state 避免重复查。
        with st.expander("📥 太子接收的完整输入 input_data(诊断用)", expanded=False):
            _log_id = log.get("id")
            _cache_key = f"_cp_input_data_cache_{_log_id}"
            _id = st.session_state.get(_cache_key)
            if _id is None and _log_id:
                try:
                    _full_log = db.get_stage_log_by_id(_log_id) or {}
                    _id = _full_log.get("input_data") or {}
                    st.session_state[_cache_key] = _id
                except Exception as _fetch_err:
                    st.warning(
                        f"(input_data 拉取失败:{type(_fetch_err).__name__}"
                        f"——这通常是 DB 连接抖动,刷新重试即可)"
                    )
                    _id = {}
            _id = _id or {}

            if not _id:
                st.caption(
                    "(input_data 为空 — 极少数情况:老版本写入时 input_data 字段"
                    "被跳过;这一般不影响执行,因为 agent 的调用参数是从内存构造的)"
                )
            else:
                _free_text_in = (
                    _id.get("free_text")
                    or (_id.get("brief") or {}).get("free_text")
                    or ""
                )
                if _free_text_in:
                    # v0.29.9: 折叠 base64 图片块,让用户能真的看懂内容。
                    # 原始字数仍然显示(那是真实传给 agent 的体量)。
                    _sanitized_ft = _sanitize_freetext_for_display(_free_text_in)
                    _orig_len = len(_free_text_in)
                    _sanit_len = len(_sanitized_ft)
                    if _sanit_len < _orig_len:
                        st.markdown(
                            f"**free_text 长度:** {_orig_len:,} 字 "
                            f"(base64 折叠后显示 {_sanit_len:,} 字 — "
                            f"实际喂给 agent 的是完整原文)"
                        )
                    else:
                        st.markdown(
                            f"**free_text 长度:** {_orig_len:,} 字"
                        )
                    st.text_area(
                        "free_text(只读,base64 已折叠)",
                        value=_sanitized_ft,
                        height=300,
                        disabled=True,
                        label_visibility="collapsed",
                        key=f"cp_input_freetext_diag_{_log_id}",
                    )
                _other = {k: v for k, v in _id.items() if k != "free_text"}
                if _other:
                    st.markdown("**其他 input 字段:**")
                    st.json(_other)
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
    # May have multiple secretariat runs — legacy stage_name "secretariat"
    # or strategy_debate_N where N is even (0, 2, 4...)
    sec_logs = [l for l in stage_logs if l.get("stage_name") == "secretariat"]
    debate_sec_logs = sorted(
        [l for l in stage_logs
         if l.get("stage_name", "").startswith("strategy_debate_")
         and int(l["stage_name"].rsplit("_", 1)[-1]) % 2 == 0],
        key=lambda l: int(l["stage_name"].rsplit("_", 1)[-1]),
    )
    all_sec_logs = sec_logs + debate_sec_logs
    if all_sec_logs:
        for sl in all_sec_logs:
            sname = sl.get("stage_name", "")
            if sname.startswith("strategy_debate_"):
                turn = int(sname.rsplit("_", 1)[-1])
                label = f"策略辩论 第 {turn // 2 + 1} 轮（中书省发言）"
            else:
                label = f"方案 (轮次 {all_sec_logs.index(sl) + 1})"
            with st.expander(label, expanded=(sl == all_sec_logs[-1])):
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
    # Legacy chancellery_N logs + strategy_debate_N where N is odd (1, 3, 5...)
    chan_logs = [l for l in stage_logs if l.get("stage_name", "").startswith("chancellery_")]
    debate_chan_logs = sorted(
        [l for l in stage_logs
         if l.get("stage_name", "").startswith("strategy_debate_")
         and int(l["stage_name"].rsplit("_", 1)[-1]) % 2 == 1],
        key=lambda l: int(l["stage_name"].rsplit("_", 1)[-1]),
    )
    all_chan_logs = chan_logs + debate_chan_logs
    if all_chan_logs:
        for cl in all_chan_logs:
            sname = cl.get("stage_name", "")
            if sname.startswith("strategy_debate_"):
                turn = int(sname.rsplit("_", 1)[-1])
                round_label = f"策略辩论 第 {turn // 2 + 1} 轮（门下省质疑）"
            else:
                round_label = sname.replace("chancellery_", "第") + "轮审议"
            with st.expander(round_label, expanded=(cl == all_chan_logs[-1])):
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

# Tab 5: 中间精炼 (v0.29.9 新增 — 叙事导演/红蓝精炼/画像模拟)
# 这三个阶段在 orchestrator 里 builder 之后 vibe 之前跑,之前 UI 没
# 对应 tab 所以用户点不到。每个 stage 的 status 图标 + 简要摘要 +
# 点开看完整 output。
with tabs[5]:
    _mid_layout = [
        ("narrative_director", "🎬 叙事导演",
         "看完整矩阵,诊断钩子类型是否重复 / 人设是否立住 / 正反比例"),
        ("red_blue_refiner", "⚔️ 红蓝精炼",
         "每个 cell 独立:Red Team 攻 AI 腔,Blue Team 做最小修复"),
        ("persona_simulator", "👥 画像模拟",
         "3 个画像 + v0.29.1 起新增 consumer_simulation 目标用户二层校验"),
    ]
    for _mid_stage_name, _mid_label, _mid_desc in _mid_layout:
        _mid_log = log_map.get(_mid_stage_name)
        with st.expander(
            f"{_mid_label} — {_mid_desc}",
            expanded=(_mid_log and _mid_log.get("status") == "completed"),
        ):
            if not _mid_log:
                st.caption("(此阶段未执行,或已被清理等待重跑)")
                continue
            render_stage_meta(_mid_log)
            _mid_status = _mid_log.get("status", "pending")
            _mid_out = _mid_log.get("output_data") or {}

            if _mid_status == "failed":
                render_stage_error(_mid_log)
                continue
            if not _mid_out:
                st.caption(f"状态:{_mid_status}")
                continue

            # 按阶段类型给一个简要摘要 + 完整 output expander
            if _mid_stage_name == "narrative_director":
                _issues = _mid_out.get("issues") or []
                _rebuild = _mid_out.get("cells_to_rebuild") or []
                if _rebuild:
                    st.warning(
                        f"诊断出 {len(_issues)} 个问题,需要重建 "
                        f"{len(_rebuild)} 个 cell:{_rebuild}"
                    )
                else:
                    st.success(
                        f"矩阵一致性诊断通过"
                        f"({len(_issues)} 个 minor issue,无需重建)"
                    )
            elif _mid_stage_name == "red_blue_refiner":
                # Per-cell agent (并发跑),每次调用产一条 log。
                # final_system._red_blue_stats 汇总在 output_center 显示,
                # 这里只显示本条 log 的 attacks/fixes 摘要。
                _attacks = _mid_out.get("attacks") or []
                _fixes = _mid_out.get("fixes") or []
                _ref_demo = bool((_mid_out.get("refined_demo_output") or "").strip())
                if _ref_demo:
                    st.success(
                        f"Red Team 找到 {len(_attacks)} 个点 → "
                        f"Blue Team 做了 {len(_fixes)} 处替换"
                    )
                elif _attacks:
                    st.warning(
                        f"Red Team 找到 {len(_attacks)} 个点但未产出 refined demo"
                    )
                else:
                    st.info("Red Team 检查通过,无需精修")
            elif _mid_stage_name == "persona_simulator":
                _mode = _mid_out.get("mode", "persona_spectrum")
                if _mode == "consumer_simulation":
                    _sumc = _mid_out.get("summary") or {}
                    _stop = _sumc.get("stop_count", 0)
                    _scroll = _sumc.get("scroll_count", 0)
                    st.info(
                        f"消费者模拟:{_stop} stop · {_scroll} scroll "
                        f"({_mode})"
                    )
                else:
                    _sump = _mid_out.get("summary") or {}
                    _strong = _sump.get("strong_cells") or []
                    _weak = _sump.get("weak_cells") or []
                    if _weak:
                        st.warning(
                            f"强 cell:{len(_strong)} · 弱 cell:{len(_weak)} "
                            f"({_weak})"
                        )
                    else:
                        st.success(
                            f"强 cell:{len(_strong)} · 无弱 cell"
                        )

            with st.expander("🔍 完整 output_data", expanded=False):
                st.json(_mid_out)

# Tab 6: Vibe (网感复检 + Gemini 仲裁 + 重写 + 结构重写)
with tabs[6]:
    # Vibe critic
    _vc_logs = [l for l in stage_logs if l.get("stage_name") == "vibe_critic"]
    _vr_logs = [l for l in stage_logs if l.get("stage_name") == "vibe_rewriter"]

    if _vc_logs:
        for idx, vcl in enumerate(_vc_logs):
            with st.expander(
                f"网感复检 轮次 {idx + 1}",
                expanded=(vcl == _vc_logs[-1]),
            ):
                render_stage_meta(vcl)
                _vc_out = vcl.get("output_data") or {}
                if _vc_out:
                    _vc_verdict = _vc_out.get("verdict", "unknown")
                    _vc_failed = _vc_out.get("failed_cells") or []
                    if _vc_verdict == "all_pass":
                        st.success(f"✅ 全部通过（{len(_vc_out.get('cell_reviews', []))} 个 cell）")
                    else:
                        st.warning(
                            f"⚠️ {_vc_verdict}：{len(_vc_failed)} 个 cell 需要重写"
                        )
                    # Show per-cell gut_call summary
                    _reviews = _vc_out.get("cell_reviews") or []
                    if _reviews:
                        for _cr in _reviews:
                            _cid = _cr.get("cell_id", "?")
                            _gut = _cr.get("gut_call", "?")
                            _gut_word = _cr.get("gut_word", "")
                            _sev = _cr.get("severity", "")
                            _emoji = {"pass": "✅", "borderline": "🟡", "fail": "🔴"}.get(
                                _sev, "❓"
                            )
                            st.caption(
                                f"{_emoji} **{_cid}** — gut: {_gut} · {_gut_word} · severity: {_sev}"
                            )

                    # Gemini arbitration results (if any)
                    _ga = _vc_out.get("_gemini_arbitration") or {}
                    if _ga and _ga.get("verdict") != "skipped":
                        st.divider()
                        st.markdown("**🟢 Gemini 二审（仲裁）**")
                        _ga_failed = _ga.get("failed_cells") or []
                        _ga_verdict = _ga.get("verdict", "unknown")
                        _ga_usage = _ga.get("_gemini_usage") or {}
                        if _ga_verdict == "all_pass" or not _ga_failed:
                            st.success(
                                "✅ Gemini 也判全部通过——Claude 和 Gemini 意见一致。"
                            )
                        else:
                            st.warning(
                                f"⚠️ Gemini 额外 flag 了 {len(_ga_failed)} 个 cell "
                                f"（Claude 判 pass 但 Gemini 判 fail）："
                            )
                            for _gf in _ga_failed:
                                _g_cid = _gf.get("cell_id", "?")
                                _g_dir = _gf.get("rewrite_directives", "")
                                st.caption(f"🔴 **{_g_cid}**：{_g_dir[:200]}")
                        if _ga_usage:
                            st.caption(
                                f"Gemini 费用：${_ga_usage.get('cost_usd', 0):.4f} · "
                                f"tokens {_ga_usage.get('input_tokens', 0)}+{_ga_usage.get('output_tokens', 0)}"
                            )
                    elif _ga and _ga.get("verdict") == "skipped":
                        st.caption(
                            f"ℹ️ Gemini 二审跳过：{_ga.get('_skip_reason', 'unknown')}"
                        )

                    with st.expander("🔍 完整 critic 输出", expanded=False):
                        st.json(_vc_out)
                elif vcl.get("status") == "failed":
                    render_stage_error(vcl)
                else:
                    st.caption(f"状态：{vcl.get('status', 'pending')}")
    else:
        st.caption("等待执行...")

    # Vibe rewriter logs (if any)
    if _vr_logs:
        st.divider()
        for idx, vrl in enumerate(_vr_logs):
            with st.expander(
                f"网感重写 轮次 {idx + 1}",
                expanded=False,
            ):
                render_stage_meta(vrl)
                _vr_out = vrl.get("output_data") or {}
                if _vr_out:
                    _rewritten = _vr_out.get("prompt_cells") or []
                    st.info(f"重写了 {len(_rewritten)} 个 cell")
                    for _rc in _rewritten:
                        _rc_cid = _rc.get("cell_id", "?")
                        _rc_mode = _rc.get("rewrite_mode", "?")
                        _rc_summary = _rc.get("rewrite_summary", "")
                        st.caption(
                            f"📝 **{_rc_cid}** ({_rc_mode})：{_rc_summary[:150]}"
                        )
                elif vrl.get("status") == "failed":
                    render_stage_error(vrl)

    # v0.29.9: 叙事结构重写(structural_rewriter)— v0.29.0 新 agent,
    # 专门处理 identity_consistency/gap_tension fail 的结构性手术。
    # 不是每次 vibe_loop 都触发(看 critic 的 root_cause_kind 分流),
    # 有 log 才显示。
    _sr_logs = [
        l for l in stage_logs if l.get("stage_name") == "structural_rewriter"
    ]
    if _sr_logs:
        st.divider()
        for idx, srl in enumerate(_sr_logs):
            with st.expander(
                f"🧱 叙事结构重写 轮次 {idx + 1}",
                expanded=False,
            ):
                render_stage_meta(srl)
                _sr_out = srl.get("output_data") or {}
                if _sr_out:
                    _sr_cells = _sr_out.get("prompt_cells") or []
                    st.info(f"结构手术 {len(_sr_cells)} 个 cell")
                    for _sc in _sr_cells:
                        _sc_cid = _sc.get("cell_id", "?")
                        _sc_mode = _sc.get("rewrite_mode", "?")
                        _sc_summary = _sc.get("rewrite_summary", "")
                        _mode_icon = {
                            "identity_restructure": "🎭",
                            "gap_restructure": "🔀",
                        }.get(_sc_mode, "🧱")
                        st.caption(
                            f"{_mode_icon} **{_sc_cid}** ({_sc_mode})："
                            f"{_sc_summary[:150]}"
                        )
                elif srl.get("status") == "failed":
                    render_stage_error(srl)

# Tab 7: Final Review
with tabs[7]:
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

# ── C: Mid-run supplement panel ────────────────────────────────────────────
# Let the user add new ideas / references / corrections and re-run from
# a chosen stage without going back to page 2. Works for completed,
# failed, and needs_revision states — basically any time the pipeline
# isn't actively running.
if status not in ("running",):
    with st.expander("📝 补充信息 / 追加参考 / 部分重跑", expanded=False):
        st.caption(
            "在当前项目的基础上追加新信息，选择从哪个阶段开始重跑。"
            "已完成的上游阶段会保留（不花重复 token），只有你选的阶段及其下游会被重新执行。"
        )

        _supp_text = st.text_area(
            "补充文字（追加到原始输入，不覆盖）",
            height=120,
            placeholder="新想法、新卖点、对之前产出的修改意见、老板最新反馈……",
            key="supp_text",
        )

        _supp_urls = st.text_area(
            "补充参考帖子 URL（每行一条，追加到已有的参考列表）",
            height=80,
            placeholder="https://www.xiaohongshu.com/explore/xxx",
            key="supp_urls",
        )

        _supp_files = st.file_uploader(
            "补充文件",
            accept_multiple_files=True,
            type=["pdf", "txt", "md", "docx", "png", "jpg", "jpeg", "gif", "webp", "json"],
            key="supp_files",
        )

        # Stage selector — which point to re-run from. Everything at
        # and below the selected stage gets deleted + re-executed.
        _rerun_stages = {
            "crown_prince": "太子（从头解析 brief，最彻底）",
            "secretariat": "中书省（重新制定策略方向）",
            "ministry_works": "工部·架构（重新设计骨架，保留五部）",
            "ministry_works_builder": "工部·构建（只重新生成 prompt）",
            "vibe_critic": "网感复检（只重新检查味道）",
        }
        _selected_stage = st.selectbox(
            "从哪个阶段开始重跑？",
            options=list(_rerun_stages.keys()),
            format_func=lambda k: _rerun_stages[k],
            index=0,
            key="supp_rerun_stage",
        )

        # Downstream stage names for deletion. Order matters — we delete
        # everything at and below the selected stage. 必须和 orchestrator
        # 实际跑的阶段全集对齐,否则上一轮的 stage_log 会残留,UI 把它们
        # 染色成"已完成/失败",误导用户以为部分阶段没重跑。
        #
        # v0.29.6: 补漏 — narrative_director / red_blue_refiner /
        # persona_simulator / structural_rewriter 四个阶段之前没列进来,
        # 用户反馈"补完重跑后叙事导演 + 红蓝 + 画像还带颜色"就是这个 bug。
        _stage_order = [
            "crown_prince",
            "gemini_reference_analyzer",
            "gemini_trend_scout_pre",
            "secretariat",
            "chancellery_1", "chancellery_2", "chancellery_3",
            "dispatcher",
            "ministry_personnel", "ministry_revenue", "ministry_rites",
            "ministry_war", "ministry_justice",
            "ministry_works",
            "ministry_works_cell_planner",
            "ministry_works_builder",
            # v0.29.6 补:cross-cell + 精炼 + 画像 + 结构审
            "narrative_director",
            "red_blue_refiner",
            "persona_simulator",
            "ministry_works_structure_review",
            "vibe_critic",
            "vibe_rewriter",
            # v0.29.6 补:结构手术(v0.29.0 新 agent,和 vibe_rewriter 并列)
            "structural_rewriter",
            "chancellery_final",
        ]

        if st.button(
            "🚀 追加并重跑",
            type="primary",
            key="supp_submit",
            help="追加信息到项目 + 删除下游 stage_logs + 从选定阶段继续执行",
        ):
            if not _supp_text.strip() and not _supp_urls.strip() and not _supp_files:
                st.warning("请至少填一项补充内容。")
            else:
                try:
                    # 1. Append supplement to project
                    _proj = db.get_project(project_id) or {}
                    _old_text = _proj.get("free_text", "") or ""
                    _new_text = _old_text
                    if _supp_text.strip():
                        _new_text += (
                            f"\n\n--- 补充信息 ({time.strftime('%Y-%m-%d %H:%M')}) ---\n"
                            + _supp_text.strip()
                        )

                    # Process uploaded files — import the helper from page 2
                    # at call time (avoid circular import at module level).
                    if _supp_files:
                        try:
                            from importlib import import_module as _im
                            _p2 = _im("pages.2_new_project")
                            _new_text += _p2.process_uploaded_files(_supp_files)
                        except Exception:
                            # Fallback: just list filenames
                            for _f in _supp_files:
                                _new_text += f"\n[补充文件: {_f.name}]"

                    _brief_update = dict(_proj.get("brief") or {})
                    # Append reference URLs
                    _existing_refs = _brief_update.get("_reference_post_urls") or []
                    for _line in (_supp_urls or "").splitlines():
                        _u = _line.strip()
                        if _u and "xiaohongshu.com" in _u and _u.startswith("http"):
                            _existing_refs.append(_u)
                    if _existing_refs:
                        _brief_update["_reference_post_urls"] = _existing_refs[:20]
                    # Clear stale revision context so the re-run is clean
                    _brief_update.pop("_revision_context", None)

                    db.update_project(
                        project_id,
                        free_text=_new_text,
                        brief=_brief_update,
                    )

                    # 2. Delete stage_logs from selected stage downward
                    _idx = (
                        _stage_order.index(_selected_stage)
                        if _selected_stage in _stage_order
                        else 0
                    )
                    _to_delete = set(_stage_order[_idx:])

                    # v0.29.7: 中书省 + 门下省实际以 strategy_debate_{turn}
                    # 命名(多轮辩论,每轮一条 stage_log),不是 "secretariat"
                    # 或 "chancellery_1..3"。之前 _stage_order 里的那些名字
                    # 其实从不存在于 DB,所以上轮这些 debate log 永远删不掉,
                    # UI 把 中书省+门下省 染成绿色。
                    # MAX_DEBATE_TURNS 默认 8,这里给 20 的 buffer 防未来涨。
                    if "secretariat" in _to_delete:
                        _to_delete.update(
                            f"strategy_debate_{i}" for i in range(20)
                        )

                    # Legacy chancellery_1..3 naming(debate 之前的老版本),
                    # 保留以清理历史 run 的残留。
                    _to_delete.update(f"chancellery_{i}" for i in range(1, 10))

                    # v0.29.7: gemini_trend_scout_post_{direction_id} 是动态名
                    # (每个 direction 一条),不在 _stage_order 里。delete 用
                    # exact match,无法前缀匹配,所以要查一遍 run 的所有 log
                    # 名字,把 post_* 都挑出来塞进 _to_delete。
                    # 只有当 chancellery_final 及之前的阶段要重跑时,post 才
                    # 需要跟着删(它在 final 之后才触发)。
                    if "chancellery_final" in _to_delete:
                        try:
                            _all_logs = db.get_stage_logs(run_id) or []
                            _to_delete.update(
                                l.get("stage_name", "")
                                for l in _all_logs
                                if (l.get("stage_name") or "")
                                   .startswith("gemini_trend_scout_post_")
                            )
                        except Exception:
                            # 非致命:拿不到也只是 post 残留不影响主流程
                            pass

                    # Gemini pre 阶段依附 crown_prince,crown_prince 重跑时
                    # 它们也要删(之前就有逻辑,保留)。
                    for _gs in ("gemini_trend_scout_pre", "gemini_reference_analyzer"):
                        if _gs in _stage_order and _idx <= _stage_order.index(_gs):
                            _to_delete.add(_gs)

                    _deleted = db.delete_stage_logs_by_names(
                        run_id, sorted(_to_delete)
                    )

                    # Reset project status so resume is allowed. "failed" is
                    # used as a "needs-resume" marker here — resume_pipeline's
                    # guard only refuses when status == "running", and the
                    # 继续执行 button requires status != "completed" (page 3
                    # line ~1424). We need both conditions lifted.
                    # v0.29.7: 同时重置累计 totals(total_tokens / total_cost_usd /
                    # completed_at),避免 header 继续展示上一轮的
                    # 18/22 · $7.74 · 419K tokens 这种误导性数据。
                    # 新的 totals 会在重跑过程中从 0 重新累加。
                    db.update_project(project_id, status="failed")
                    try:
                        db.update_pipeline_run(
                            run_id,
                            status="failed",
                            completed_at=None,
                            total_tokens=0,
                            total_cost_usd=0.0,
                        )
                    except Exception:
                        # 老 schema 可能没 total_* 列,回退到只改 status。
                        db.update_pipeline_run(
                            run_id, status="failed", completed_at=None,
                        )

                    # v0.29.5: 方案 B — 自动触发 resume,不再让用户找「继续执行」
                    # 按钮。之前的做法要用户点两次 + 还把 status 显示成 ❌ failed,
                    # 看起来像真失败。现在"补完即跑",出错回退到老提示。
                    try:
                        from pipeline.orchestrator import (
                            PipelineAlreadyRunningError,
                            resume_pipeline_in_background,
                        )
                        from pipeline.agents import init_api_config
                        init_api_config()
                        resume_pipeline_in_background(project_id, run_id, db)
                        st.success(
                            f"✅ 补充信息已追加,{_deleted} 个 stage_log 已清除。"
                            f"已自动从 {_rerun_stages[_selected_stage]} 开始重跑——"
                            f"页面会自动刷新显示进度。"
                        )
                    except PipelineAlreadyRunningError as _resume_err:
                        # 旁路 running 冲突(罕见:刚好另一个 tab 也在启动)。
                        # 回退到原提示让用户手动处理。
                        st.warning(
                            f"✅ 补充信息已追加,但自动重跑失败:{_resume_err}\n\n"
                            f"请手动点下方「▶️ 继续执行」从 "
                            f"{_rerun_stages[_selected_stage]} 重跑。"
                        )
                    except Exception as _resume_err:
                        # 其他启动错误(API 配置 / 线程起不来等)。提示后回退。
                        st.warning(
                            f"✅ 补充信息已追加,但自动重跑启动失败:"
                            f"{type(_resume_err).__name__}: {_resume_err}\n\n"
                            f"请手动点下方「▶️ 继续执行」从 "
                            f"{_rerun_stages[_selected_stage]} 重跑。"
                        )
                    time.sleep(1)
                    st.rerun()
                except Exception as _err:
                    st.error(f"补充失败：{type(_err).__name__}: {_err}")

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

# Auto-refresh when running or paused for review (waiting for user input).
# Streamlit has no server-push, so live progress requires periodic reruns.
# Gated by a user toggle so the page doesn't yank out while someone's reading
# a stage log mid-run.
if status in ("running", "paused_for_review") or run.get("status") in ("running", "paused_for_review"):
    _auto = st.toggle(
        f"🔄 自动刷新（每 {POLL_INTERVAL_SECONDS} 秒）",
        value=st.session_state.get("detail_auto_refresh", True),
        key="detail_auto_refresh",
    )
    if _auto:
        time.sleep(POLL_INTERVAL_SECONDS)
        st.rerun()
