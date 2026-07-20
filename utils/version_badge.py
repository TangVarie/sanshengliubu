"""Shared sidebar chrome. Call show_version_badge() at the top of each page.

Besides the version badge, this now injects the global design system and
renders a localized icon navigation (replacing Streamlit's auto English,
filename-derived page list, which is hidden via CSS in utils.theme). Because
every page already calls this at the top, the whole app picks up the theme
and nav with no per-page wiring.
"""

import streamlit as st

from pipeline.config import VERSION, VERSION_DATE
from utils.theme import inject_global_styles

# (target, label). Order defines sidebar nav order. No emoji by design —
# hierarchy comes from typography + the vermilion active bar.
_NAV = [
    ("app.py", "首页"),
    ("pages/1_dashboard.py", "项目总览"),
    ("pages/2_new_project.py", "新建项目"),
    ("pages/3_pipeline_detail.py", "流水线详情"),
    ("pages/4_output_center.py", "产出中心"),
    ("pages/6_reference_library.py", "参考样本库"),
    ("pages/5_settings.py", "设置"),
]

_BRAND_HTML = """
<div class="ssb-brand">
  <div class="ssb-brand-seal">省</div>
  <div>
    <div class="ssb-brand-name">三省六部</div>
    <div class="ssb-brand-sub">Prompt Engineering</div>
  </div>
</div>
"""


def show_version_badge() -> None:
    """Inject theme + render the branded sidebar (nav, tenant note, version)."""
    inject_global_styles()

    with st.sidebar:
        st.markdown(_BRAND_HTML, unsafe_allow_html=True)
        st.markdown('<div class="ssb-nav-label">导航 · Navigation</div>',
                    unsafe_allow_html=True)
        for target, label in _NAV:
            try:
                st.page_link(target, label=label)
            except Exception:
                # A missing page target should never take the whole app down.
                pass

        st.divider()
        # R-019: single-tenant operating reminder (RLS disabled). Kept visible
        # on every page so a future multi-brand deploy can't forget to flip it.
        st.caption("单租户模式 · RLS 关闭")
        st.caption(f"**{VERSION}** · {VERSION_DATE}")
