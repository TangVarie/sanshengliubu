"""参考样本库 · Reference Library

录入"四位一体证据包"(封面 + 标题 + 正文 + top 评论),AI 做"太子式"
结构化分析后存库。vibe_loop 按 platform + category 检索命中样本。

设计取舍:
  - URL 抓取不可靠(小红书等 bot 挡),改走人工手工录入。2 分钟/条,
    换来评论区等 AI 抓不到的 vibe DNA。
  - AI 分析只在"AI 分析并保存"那一步发生,不在每次 pipeline 运行时
    重复分析——上游结构化、下游廉价检索。
"""

from __future__ import annotations

import base64
import io
import json

import streamlit as st

from db.supabase_client import SupabaseClient
from pipeline.agents import init_api_config
from pipeline.agents.reference_pack_analyzer import (
    ReferencePackAnalysisFailed,
    analyze_reference_pack,
)
from utils.version_badge import show_version_badge

st.set_page_config(page_title="参考样本库", page_icon="省", layout="wide")
show_version_badge()
st.title("参考样本库")

st.caption(
    '录入"四位一体证据包"(封面 + 标题 + 正文 + top 评论)。'
    '保存时 AI 会做一次"太子式"结构化分析,提取 vibe / 钩子 / 评论区 DNA。'
    "流水线网感复检阶段按 平台 + 品类 自动检索匹配样本。"
)

try:
    db = SupabaseClient.get_instance()
except Exception as e:
    st.error(f"无法连接数据库,请先在「设置」里配置 Supabase。\n\n{e}")
    st.stop()


# ── Size caps (mirror pages/2_new_project.py) ─────────────────────────────
_MAX_IMAGE_BYTES = 2 * 1024 * 1024


# ── Tab 1: 录入 ═══════════════════════════════════════════════════════════
# ── Tab 2: 浏览 / 删除 ═════════════════════════════════════════════════════

tab_new, tab_browse = st.tabs(["新建证据包", "浏览已有"])


# ════════════════════════════════════════════════════════════════════════════
# Tab 1: 录入
# ════════════════════════════════════════════════════════════════════════════

with tab_new:
    with st.container(border=True):
        st.markdown("#### 基础信息")
        c1, c2, c3 = st.columns([2, 2, 2])
        with c1:
            platform = st.selectbox(
                "平台 *",
                ["小红书", "抖音", "微博", "B站", "知乎", "快手"],
                key="pack_platform",
            )
        with c2:
            category = st.text_input(
                "品类 *",
                placeholder="例:护肤 / 食品 / 3C数码 / 母婴 ...",
                key="pack_category",
            )
        with c3:
            title_hint = st.text_input(
                "备注名(可选)",
                placeholder="留空会自动截取帖子标题前 80 字",
                key="pack_title_hint",
            )

    with st.container(border=True):
        st.markdown("#### 四位一体证据包")

        col_l, col_r = st.columns([1, 2])
        with col_l:
            st.markdown("**封面图(可选)**")
            cover_file = st.file_uploader(
                "拖入或点击上传封面截图",
                type=["png", "jpg", "jpeg", "webp"],
                accept_multiple_files=False,
                key="pack_cover",
            )
            cover_b64 = ""
            if cover_file is not None:
                size = getattr(cover_file, "size", None) or 0
                if size > _MAX_IMAGE_BYTES:
                    st.warning(
                        f"封面过大({size // 1024}KB),"
                        f"需 < {_MAX_IMAGE_BYTES // 1024 // 1024}MB"
                    )
                else:
                    raw = cover_file.read()
                    cover_b64 = base64.b64encode(raw).decode("utf-8")
                    st.image(io.BytesIO(raw), caption=cover_file.name, width=220)

        with col_r:
            post_title = st.text_input(
                "帖子标题 *",
                placeholder="直接从 App 里复制过来",
                key="pack_post_title",
            )
            post_body = st.text_area(
                "帖子正文 *",
                placeholder="直接粘贴全文。emoji / 换行 / 话题标签都原样保留,越真越好。",
                height=220,
                key="pack_post_body",
            )
            st.markdown("**Top 评论(推荐 3-10 条)**")
            st.caption(
                "一行一条,按「数字」前缀可记录点赞数(可选)。格式:"
                "`123 | 评论内容` 或直接一行一条评论不含点赞数。"
            )
            comments_raw = st.text_area(
                "评论区",
                placeholder="例:\n512 | 姐你这波让我原地拔草\n203 | 真的不踩雷吗\n真的假的 我也买试试",
                height=180,
                key="pack_comments",
            )

    with st.container(border=True):
        st.markdown("#### AI 分析并保存")
        st.caption(
            "点下面按钮会调用 Claude 做一次结构化分析(参考 "
            "`pipeline/prompts/reference_pack_analyzer.md`),输出会作为 "
            "`ai_analysis` JSON 存库。封面图若上传,会一并走视觉分析。"
        )

        _save_clicked = st.button(
            "AI 分析并保存",
            type="primary",
            use_container_width=True,
            key="pack_save_btn",
        )

        if _save_clicked:
            # Basic validation
            if not platform or not category:
                st.error("平台 和 品类 是必填项。")
                st.stop()
            if not post_title.strip() and not post_body.strip():
                st.error("帖子标题 和 正文 至少填一个。")
                st.stop()

            # Parse comments
            top_comments: list[dict] = []
            for line in (comments_raw or "").splitlines():
                line = line.strip()
                if not line:
                    continue
                if "|" in line:
                    left, _, right = line.partition("|")
                    left = left.strip()
                    likes = None
                    if left.isdigit():
                        likes = int(left)
                    comment_text = right.strip()
                    if comment_text:
                        item = {"text": comment_text}
                        if likes is not None:
                            item["likes"] = likes
                        top_comments.append(item)
                else:
                    top_comments.append({"text": line})

            pack = {
                "title": title_hint.strip() or post_title.strip()[:80] or "未命名样本",
                "platform": platform,
                "category": category.strip(),
                "post_title": post_title.strip(),
                "post_body": post_body.strip(),
                "top_comments": top_comments,
                "cover_image_b64": cover_b64 or None,
            }

            # Init API config for standalone analyzer call
            try:
                init_api_config()
            except Exception as e:
                st.error(f"无法初始化 API 配置:{e}\n\n请到「设置」检查 Claude 接入。")
                st.stop()

            with st.status("AI 正在分析证据包...", expanded=True) as status:
                st.write("封面视觉 + 标题/正文 + 评论区 → 结构化分析中")
                try:
                    analysis = analyze_reference_pack(pack)
                except ReferencePackAnalysisFailed as e:
                    status.update(label="分析失败", state="error")
                    st.error(f"AI 分析失败,未入库:\n\n{e}")
                    st.stop()

                st.write("分析完成,写入数据库...")
                pack["ai_analysis"] = analysis
                try:
                    row = db.save_reference_pack(pack)
                except Exception as e:
                    status.update(label="入库失败", state="error")
                    st.error(f"入库失败:{e}")
                    st.stop()

                status.update(label="已保存", state="complete")

            st.success(f"证据包 #{row['id']} 已入库")
            with st.expander("查看 AI 分析结果", expanded=True):
                st.json(analysis)

            # Clear form for the next entry — avoid accidental duplicates
            for k in [
                "pack_post_title", "pack_post_body", "pack_comments",
                "pack_title_hint", "pack_category",
            ]:
                st.session_state.pop(k, None)


# ════════════════════════════════════════════════════════════════════════════
# Tab 2: 浏览
# ════════════════════════════════════════════════════════════════════════════

with tab_browse:
    with st.container(border=True):
        fc1, fc2, fc3 = st.columns([2, 2, 1])
        with fc1:
            filter_platform = st.selectbox(
                "按平台过滤",
                ["(全部)", "小红书", "抖音", "微博", "B站", "知乎", "快手"],
                key="browse_platform",
            )
        with fc2:
            filter_category = st.text_input(
                "按品类过滤(精确匹配,留空显示全部)",
                key="browse_category",
            )
        with fc3:
            st.write("")
            refresh_clicked = st.button("刷新", use_container_width=True)

    try:
        packs = db.list_reference_packs(
            platform=(None if filter_platform == "(全部)" else filter_platform),
            category=(filter_category.strip() or None),
            limit=200,
        )
    except Exception as e:
        st.error(f"读取数据库失败:{e}")
        st.stop()

    if not packs:
        st.info("还没有证据包匹配。到「新建证据包」录入第一条。")
    else:
        st.caption(f"共 {len(packs)} 条。quality_score 降序 + 创建时间降序。")
        for p in packs:
            pid = p["id"]
            ai = p.get("ai_analysis") or {}
            summary = ai.get("one_line_summary") or "(未分析)"
            tags = (ai.get("vibe_tags") or {})
            tone = " / ".join(tags.get("tone") or [])
            hook = " / ".join(tags.get("hook_type") or [])
            struct = " / ".join(tags.get("structure") or [])
            comments_n = len(p.get("top_comments") or [])

            with st.container(border=True):
                header = (
                    f"**[{p.get('platform') or '?'} · "
                    f"{p.get('category') or '?'}]** "
                    f"{p.get('post_title') or p.get('title') or '未命名'}"
                )
                st.markdown(header)
                st.caption(f"{summary}")
                meta_bits = []
                if tone:
                    meta_bits.append(f"语气: {tone}")
                if hook:
                    meta_bits.append(f"钩子: {hook}")
                if struct:
                    meta_bits.append(f"结构: {struct}")
                meta_bits.append(f"评论 {comments_n} 条")
                meta_bits.append(f"质量分 {p.get('quality_score') or 0}")
                st.caption(" · ".join(meta_bits))

                cA, cB, cC, cD = st.columns([1, 1, 1, 3])
                with cA:
                    if st.button("查看", key=f"view_{pid}"):
                        st.session_state[f"show_detail_{pid}"] = True
                with cB:
                    if st.button("+1 分", key=f"up_{pid}", help="人工加分,优先检索"):
                        try:
                            db.update_reference_pack(
                                pid,
                                quality_score=int(p.get("quality_score") or 0) + 1,
                            )
                            st.rerun()
                        except Exception as e:
                            st.error(f"更新失败:{e}")
                with cC:
                    if st.button("删除", key=f"del_{pid}"):
                        st.session_state[f"confirm_del_{pid}"] = True

                if st.session_state.get(f"confirm_del_{pid}"):
                    st.warning("确认删除?不可恢复。")
                    dc1, dc2 = st.columns([1, 5])
                    with dc1:
                        if st.button("确认", key=f"yes_{pid}", type="primary"):
                            try:
                                db.delete_reference_sample(pid)
                                st.session_state.pop(f"confirm_del_{pid}", None)
                                st.rerun()
                            except Exception as e:
                                st.error(f"删除失败:{e}")

                if st.session_state.get(f"show_detail_{pid}"):
                    st.markdown("---")
                    if p.get("cover_image_b64"):
                        try:
                            st.image(
                                io.BytesIO(base64.b64decode(p["cover_image_b64"])),
                                caption="封面",
                                width=220,
                            )
                        except Exception:
                            st.caption("(封面解码失败)")
                    st.markdown("**原帖标题**")
                    st.write(p.get("post_title") or "—")
                    st.markdown("**原帖正文**")
                    st.text(p.get("post_body") or "—")
                    st.markdown("**Top 评论**")
                    comments = p.get("top_comments") or []
                    if comments:
                        for c in comments:
                            prefix = f"{c.get('likes')} " if c.get("likes") else ""
                            st.markdown(f"- {prefix}{c.get('text', '')}")
                    else:
                        st.caption("(无评论)")
                    st.markdown("**AI 分析(ai_analysis)**")
                    st.json(ai)
                    if st.button("收起", key=f"hide_{pid}"):
                        st.session_state.pop(f"show_detail_{pid}", None)
                        st.rerun()
