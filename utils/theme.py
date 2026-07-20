"""Global visual design system for 三省六部.

Single source of truth for the app's look & feel. `inject_global_styles()`
injects one comprehensive stylesheet (fonts + palette + component reset) and
is called once per page from `utils.version_badge.show_version_badge`, so
every page — existing and future — picks up the theme automatically.

Design language — flat editorial / Swiss-imperial
--------------------------------------------------
Deliberately NOT the generic "AI" look. No rounded corners, no emoji, no
drop shadows or faux-3D. Instead:

  · 宣纸 warm paper canvas, 墨 ink text, a single 朱砂 vermilion accent,
    鎏金 gold used only as hairlines.
  · Right angles everywhere. Structure comes from 1px rules, not shadows.
  · Flat color fills. No gradients-for-depth.
  · Typography carries the hierarchy: Noto Serif SC for display, a mono
    face for indices/labels (technical, editorial), Noto Sans SC for body.
  · The 印章 (seal) motif — a flat vermilion square with a character —
    stands in for iconography instead of emoji.
"""

from __future__ import annotations

import streamlit as st

# ── Palette ────────────────────────────────────────────────────────────────
# Mirrored into .streamlit/config.toml so native chrome we don't override
# still matches (no color flash on load).
PALETTE = {
    "vermilion": "#9E2B25",       # 朱砂 — the one accent
    "vermilion_deep": "#7C1E1A",
    "gold": "#B58A4B",            # 鎏金 — hairlines only
    "ink": "#1E1A15",            # 墨 — sidebar, headings
    "paper": "#F4EEE1",          # 宣纸 — canvas
    "paper_raised": "#FBF7EE",   # cards / inputs
    "paper_sunken": "#E9E0CE",
    "line": "#D9CDB4",           # hairline rules
    "line_strong": "#C3B491",
    "text": "#2A251E",
    "text_muted": "#6B6250",
    "text_faint": "#988C77",
}

_FONT_IMPORT = (
    "@import url('https://fonts.googleapis.com/css2?"
    "family=Noto+Sans+SC:wght@300;400;500;700&"
    "family=Noto+Serif+SC:wght@500;600;700;900&"
    "family=JetBrains+Mono:wght@400;500;600&display=swap');"
)

_SANS = ('"Noto Sans SC", -apple-system, BlinkMacSystemFont, "Segoe UI", '
         '"PingFang SC", "Microsoft YaHei", sans-serif')
_SERIF = '"Noto Serif SC", "Songti SC", "SimSun", Georgia, serif'
_MONO = '"JetBrains Mono", "SFMono-Regular", ui-monospace, Menlo, monospace'


def _build_css() -> str:
    p = PALETTE
    return f"""
<style>
{_FONT_IMPORT}

:root {{
  --ssb-vermilion: {p['vermilion']};
  --ssb-vermilion-deep: {p['vermilion_deep']};
  --ssb-gold: {p['gold']};
  --ssb-ink: {p['ink']};
  --ssb-paper: {p['paper']};
  --ssb-paper-raised: {p['paper_raised']};
  --ssb-paper-sunken: {p['paper_sunken']};
  --ssb-line: {p['line']};
  --ssb-line-strong: {p['line_strong']};
  --ssb-text: {p['text']};
  --ssb-text-muted: {p['text_muted']};
  --ssb-text-faint: {p['text_faint']};
  --ssb-sans: {_SANS};
  --ssb-serif: {_SERIF};
  --ssb-mono: {_MONO};
}}

/* Kill rounded corners app-wide. Structure comes from rules, not radius. */
[data-testid="stApp"] *, [data-testid="stSidebar"] * {{
  border-radius: 0 !important;
}}
/* Exception: radio markers must stay circular (a square radio reads as a
   checkbox). Checkboxes/toggles staying square is on-brand. */
[data-testid="stRadio"] [data-baseweb="radio"] div,
[data-testid="stRadio"] [role="radio"],
[data-testid="stRadio"] [role="radio"] * {{ border-radius: 50% !important; }}

/* ── Canvas ─────────────────────────────────────────────────────────── */
html, body, [data-testid="stApp"], .stApp {{
  font-family: var(--ssb-sans);
  color: var(--ssb-text);
  background: var(--ssb-paper);
}}
[data-testid="stAppViewContainer"] {{ background: transparent; }}
[data-testid="stHeader"] {{ background: transparent; height: 0; }}
[data-testid="stToolbar"] {{ right: .6rem; }}

[data-testid="stMainBlockContainer"], .block-container {{
  padding-top: 2.4rem;
  padding-bottom: 5rem;
  max-width: 1220px;
}}

/* ── Typography ─────────────────────────────────────────────────────── */
.stApp h1, .stApp h2, .stApp h3, .stApp h4 {{
  font-family: var(--ssb-serif);
  color: var(--ssb-ink);
  letter-spacing: .01em;
  font-weight: 700;
}}
.stApp h1 {{ font-size: 1.95rem; line-height: 1.22; font-weight: 900; }}
.stApp h2 {{ font-size: 1.42rem; }}
.stApp h3 {{ font-size: 1.16rem; }}
/* Page title: flat vermilion rule above it (a printer's rule, not an icon) */
[data-testid="stHeading"] h1 {{
  padding-top: .55rem;
  border-top: 2px solid var(--ssb-vermilion);
  display: inline-block;
}}
.stApp p, .stApp li, [data-testid="stMarkdownContainer"] {{ line-height: 1.7; }}
.stApp a {{ color: var(--ssb-vermilion); text-decoration: none; }}
.stApp a:hover {{ color: var(--ssb-vermilion-deep); text-decoration: underline; text-underline-offset: 3px; }}
[data-testid="stCaptionContainer"], .stCaption, small {{ color: var(--ssb-text-muted) !important; }}
hr, [data-testid="stDivider"] > div {{
  border: none !important;
  border-top: 1px solid var(--ssb-line) !important;
  background: none !important;
}}

/* ── Sidebar: flat ink panel ────────────────────────────────────────── */
[data-testid="stSidebar"] {{
  background: var(--ssb-ink);
  border-right: 1px solid var(--ssb-gold);
}}
[data-testid="stSidebar"] > div {{ background: transparent; }}
[data-testid="stSidebar"] * {{ color: #E7DCC6; }}
[data-testid="stSidebar"] [data-testid="stCaptionContainer"],
[data-testid="stSidebar"] .stCaption {{ color: #A79A80 !important; }}
[data-testid="stSidebar"] h1, [data-testid="stSidebar"] h2,
[data-testid="stSidebar"] h3, [data-testid="stSidebar"] h4 {{ color: #F1E7CF; }}
[data-testid="stSidebar"] hr, [data-testid="stSidebar"] [data-testid="stDivider"] > div {{
  border-top: 1px solid rgba(181,138,75,.35) !important;
}}
/* Hide Streamlit's auto (English, filename-derived) nav — we render our own. */
[data-testid="stSidebarNav"] {{ display: none; }}
[data-testid="stSidebarCollapseButton"] *, [data-baseweb="tooltip"] * {{ color: #C7BA9E !important; }}

.ssb-brand {{ display: flex; align-items: center; gap: .7rem; padding: .1rem .1rem 1rem; }}
.ssb-brand-seal {{
  flex: 0 0 auto; width: 40px; height: 40px;
  display: grid; place-items: center;
  font-family: var(--ssb-serif); font-weight: 900; font-size: 1.2rem;
  color: #F7EFDD; background: var(--ssb-vermilion);
  border: 1px solid var(--ssb-gold);
}}
.ssb-brand-name {{
  font-family: var(--ssb-serif); font-weight: 800; font-size: 1.08rem;
  color: #F3E9D1; line-height: 1.1; letter-spacing: .16em;
}}
.ssb-brand-sub {{
  font-family: var(--ssb-mono); font-size: .62rem; letter-spacing: .22em;
  text-transform: uppercase; color: var(--ssb-gold); margin-top: .3rem;
}}
.ssb-nav-label {{
  font-family: var(--ssb-mono); font-size: .62rem; letter-spacing: .24em;
  text-transform: uppercase; color: #857A64; margin: .2rem .1rem .4rem;
}}

/* Sidebar nav items — flat, sharp, vermilion active bar */
[data-testid="stSidebar"] [data-testid="stPageLink"] a {{
  padding: .5rem .7rem !important;
  border-left: 3px solid transparent;
  transition: background .14s ease, border-color .14s ease;
}}
[data-testid="stSidebar"] [data-testid="stPageLink"] a:hover {{ background: rgba(181,138,75,.12); }}
[data-testid="stSidebar"] [data-testid="stPageLink"] a p {{
  font-weight: 500; font-size: .93rem; color: #E7DCC6; letter-spacing: .04em;
}}
[data-testid="stSidebar"] [data-testid="stPageLink"] a[aria-current="page"] {{
  background: rgba(158,43,37,.30);
  border-left-color: var(--ssb-vermilion);
}}
[data-testid="stSidebar"] [data-testid="stPageLink"] a[aria-current="page"] p {{ color: #FBF3E2; font-weight: 700; }}

/* ── Buttons: flat ──────────────────────────────────────────────────── */
.stButton > button, .stDownloadButton > button, .stFormSubmitButton > button {{
  font-family: var(--ssb-sans); font-weight: 600;
  border-radius: 0 !important;
  transition: background .14s ease, color .14s ease, border-color .14s ease;
  box-shadow: none !important;
}}
/* secondary / default */
.stButton > button, .stDownloadButton > button,
[data-testid="stBaseButton-secondary"], [data-testid="stBaseButton-secondaryFormSubmit"] {{
  background: transparent;
  color: var(--ssb-ink);
  border: 1px solid var(--ssb-line-strong);
}}
.stButton > button:hover, .stDownloadButton > button:hover,
[data-testid="stBaseButton-secondary"]:hover {{
  background: var(--ssb-ink); color: #F4EEE1; border-color: var(--ssb-ink);
}}
/* primary — flat cinnabar */
[data-testid="stBaseButton-primary"], [data-testid="stBaseButton-primaryFormSubmit"],
.stButton > button[kind="primary"], .stFormSubmitButton > button[kind="primary"] {{
  background: var(--ssb-vermilion); color: #FBF3E2;
  border: 1px solid var(--ssb-vermilion-deep);
}}
[data-testid="stBaseButton-primary"]:hover, [data-testid="stBaseButton-primaryFormSubmit"]:hover,
.stButton > button[kind="primary"]:hover {{ background: var(--ssb-vermilion-deep); color: #fff; }}
.stButton > button:focus-visible, [data-testid="stBaseButton-primary"]:focus-visible {{
  outline: 2px solid var(--ssb-gold); outline-offset: 2px;
}}

/* ── Cards: st.container(border=True) → flat framed panel ───────────── */
[data-testid="stVerticalBlock"][data-test-scroll-behavior] {{
  background: var(--ssb-paper-raised);
  border: 1px solid var(--ssb-line) !important;
  border-radius: 0 !important;
  transition: border-color .16s ease;
}}
[data-testid="stVerticalBlock"][data-test-scroll-behavior]:hover {{
  border-color: var(--ssb-line-strong) !important;
}}

/* ── Metrics as flat stat plates ────────────────────────────────────── */
[data-testid="stMetric"] {{
  background: var(--ssb-paper-raised);
  border: 1px solid var(--ssb-line);
  border-left: 3px solid var(--ssb-vermilion);
  padding: .95rem 1.1rem;
}}
[data-testid="stMetricLabel"] {{
  color: var(--ssb-text-muted) !important;
  font-family: var(--ssb-mono); font-size: .72rem; letter-spacing: .1em; text-transform: uppercase;
}}
[data-testid="stMetricValue"] {{
  font-family: var(--ssb-serif); font-weight: 800; color: var(--ssb-ink);
  font-variant-numeric: tabular-nums;
}}

/* ── Tabs: flat, underline active ───────────────────────────────────── */
[data-testid="stTabs"] [data-baseweb="tab-list"] {{ gap: .1rem; border-bottom: 1px solid var(--ssb-line); }}
[data-testid="stTabs"] [data-baseweb="tab"] {{
  padding: .4rem .9rem; color: var(--ssb-text-muted); font-weight: 500;
  border-radius: 0 !important;
}}
[data-testid="stTabs"] [data-baseweb="tab"]:hover {{ color: var(--ssb-ink); background: var(--ssb-paper-sunken); }}
[data-testid="stTabs"] [aria-selected="true"] {{ color: var(--ssb-vermilion-deep) !important; font-weight: 700; }}
[data-testid="stTabs"] [data-baseweb="tab-highlight"] {{ background: var(--ssb-vermilion) !important; height: 2px; }}
[data-testid="stTabs"] [data-baseweb="tab-border"] {{ background: var(--ssb-line) !important; }}

/* ── Expanders: flat framed ─────────────────────────────────────────── */
[data-testid="stExpander"] {{
  border: 1px solid var(--ssb-line);
  background: var(--ssb-paper-raised);
}}
[data-testid="stExpander"] summary {{ font-weight: 600; }}
[data-testid="stExpander"] summary:hover {{ color: var(--ssb-vermilion-deep); background: var(--ssb-paper-sunken); }}

/* ── Inputs: flat, vermilion focus border ───────────────────────────── */
[data-baseweb="input"], [data-baseweb="textarea"], [data-baseweb="select"] > div {{
  border: 1px solid var(--ssb-line-strong) !important;
  border-radius: 0 !important;
  background: var(--ssb-paper-raised) !important;
  transition: border-color .14s ease;
}}
[data-baseweb="input"]:focus-within, [data-baseweb="textarea"]:focus-within,
[data-baseweb="select"] > div:focus-within {{
  border-color: var(--ssb-vermilion) !important;
  outline: 1px solid var(--ssb-vermilion);
}}
.stTextInput label, .stTextArea label, .stSelectbox label, .stMultiSelect label,
.stRadio label, .stFileUploader label, .stCheckbox label, .stToggle label {{
  font-weight: 600; color: var(--ssb-text);
}}
[data-testid="stFileUploaderDropzone"] {{
  background: var(--ssb-paper-sunken);
  border: 1px dashed var(--ssb-line-strong);
  border-radius: 0 !important;
}}
[data-testid="stFileUploaderDropzone"]:hover {{ border-color: var(--ssb-vermilion); }}
[data-baseweb="tag"] {{ background: var(--ssb-vermilion) !important; border-radius: 0 !important; }}

/* ── Alerts: flat, left rule ────────────────────────────────────────── */
[data-testid="stAlert"], [data-testid="stAlertContainer"] {{
  border-radius: 0 !important; box-shadow: none !important;
}}
[data-testid="stAlert"] {{ font-size: .92rem; }}

/* ── Code: flat framed ──────────────────────────────────────────────── */
[data-testid="stCode"], pre {{ border-radius: 0 !important; border: 1px solid var(--ssb-line); }}
code, kbd, [data-testid="stCode"] * {{ font-family: var(--ssb-mono) !important; }}
[data-testid="stMarkdownContainer"] code {{
  background: var(--ssb-paper-sunken); color: var(--ssb-vermilion-deep);
  padding: .08em .36em; border-radius: 0 !important; font-size: .88em;
}}

/* ── Progress: flat vermilion ───────────────────────────────────────── */
[data-testid="stProgress"] > div > div > div {{ background: var(--ssb-vermilion) !important; }}
[data-testid="stProgress"] > div > div {{ background: var(--ssb-paper-sunken) !important; }}

[data-testid="stDataFrame"], [data-testid="stTable"] {{ border: 1px solid var(--ssb-line); }}

::selection {{ background: rgba(181,138,75,.30); }}

/* ── Custom building blocks ─────────────────────────────────────────── */
/* Hero — flat ink block, gold hairline frame, seal-character watermark */
.ssb-hero {{
  position: relative; overflow: hidden;
  margin: .1rem 0 2rem; padding: 2.6rem 2.6rem 2.3rem;
  background: var(--ssb-ink);
  border: 1px solid var(--ssb-gold);
  color: #F1E7CF;
}}
.ssb-hero::after {{
  content: "省"; position: absolute; right: 1.2rem; bottom: -3.2rem;
  font-family: var(--ssb-serif); font-weight: 900; font-size: 15rem;
  color: rgba(181,138,75,.10); line-height: 1; pointer-events: none; user-select: none;
}}
.ssb-hero-kicker {{
  display: inline-flex; align-items: center; gap: .7rem;
  font-family: var(--ssb-mono); font-size: .68rem; letter-spacing: .3em;
  text-transform: uppercase; color: var(--ssb-gold); margin-bottom: 1rem;
}}
.ssb-hero-kicker::before {{ content: ""; width: 30px; height: 1px; background: var(--ssb-gold); }}
.ssb-hero-title {{
  font-family: var(--ssb-serif); font-weight: 900;
  font-size: clamp(2rem, 4.2vw, 2.95rem); line-height: 1.14;
  color: #FBF5E6; margin: 0 0 .8rem; letter-spacing: .05em;
}}
.ssb-hero-title .ssb-accent {{ color: var(--ssb-gold); font-weight: 400; }}
.ssb-hero-lead {{ font-size: 1rem; line-height: 1.8; color: #D2C6AC; max-width: 62ch; margin: 0; }}
.ssb-io {{ display: flex; flex-wrap: wrap; gap: .5rem 2rem; margin-top: 1.5rem;
  padding-top: 1.2rem; border-top: 1px solid rgba(181,138,75,.3); }}
.ssb-io-item {{ display: flex; align-items: baseline; gap: .6rem; }}
.ssb-io-tag {{ font-family: var(--ssb-mono); font-size: .64rem; letter-spacing: .18em;
  text-transform: uppercase; color: var(--ssb-gold); font-weight: 600; }}
.ssb-io-text {{ color: #E4D9BF; font-size: .92rem; }}

/* Pipeline flow — flat framed cells, hairline connectors, numeric index */
.ssb-flow {{ display: flex; flex-wrap: wrap; align-items: stretch; margin: .1rem 0 2.2rem; border: 1px solid var(--ssb-line); }}
.ssb-flow-node {{
  flex: 1 1 0; min-width: 92px; padding: .8rem .6rem .75rem; text-align: center;
  background: var(--ssb-paper-raised); border-right: 1px solid var(--ssb-line);
}}
.ssb-flow-node:last-child {{ border-right: none; }}
.ssb-flow-node .n-idx {{ font-family: var(--ssb-mono); font-size: .62rem; letter-spacing: .12em; color: var(--ssb-text-faint); }}
.ssb-flow-node .n-name {{ font-family: var(--ssb-serif); font-weight: 700; font-size: .96rem; color: var(--ssb-ink); margin-top: .3rem; }}
.ssb-flow-node .n-role {{ font-size: .68rem; color: var(--ssb-text-muted); margin-top: .15rem; }}
.ssb-flow-node.is-accent {{ background: var(--ssb-vermilion); }}
.ssb-flow-node.is-accent .n-idx {{ color: rgba(251,243,226,.6); }}
.ssb-flow-node.is-accent .n-name {{ color: #FBF3E2; }}
.ssb-flow-node.is-accent .n-role {{ color: rgba(251,243,226,.8); }}

/* Section eyebrow */
.ssb-eyebrow {{
  display: flex; align-items: baseline; gap: .7rem;
  font-family: var(--ssb-serif); font-weight: 700; font-size: 1.16rem;
  color: var(--ssb-ink); margin: .3rem 0 1rem; padding-bottom: .55rem;
  border-bottom: 1px solid var(--ssb-line);
}}
.ssb-eyebrow::before {{
  content: ""; align-self: center; width: 10px; height: 10px; flex: 0 0 auto;
  background: var(--ssb-vermilion); transform: translateY(1px);
}}
.ssb-eyebrow small {{ color: var(--ssb-text-muted); font-family: var(--ssb-mono);
  font-weight: 400; font-size: .72rem; letter-spacing: .06em; text-transform: uppercase; }}

/* Nav-card interior */
.ssb-card-head {{ display: flex; align-items: baseline; gap: .6rem; margin-bottom: .5rem; }}
.ssb-card-idx {{ font-family: var(--ssb-mono); font-size: .74rem; font-weight: 600;
  color: var(--ssb-vermilion); letter-spacing: .06em; }}
.ssb-card-title {{ font-family: var(--ssb-serif); font-weight: 700; font-size: 1.08rem; color: var(--ssb-ink); }}
.ssb-card-desc {{ color: var(--ssb-text-muted); font-size: .86rem; line-height: 1.6; margin: .1rem 0 .3rem; min-height: 3.1em; }}

/* Main-area page links → text CTA with underline motion */
[data-testid="stMainBlockContainer"] [data-testid="stPageLink"] a {{ padding: .2rem 0 !important; }}
[data-testid="stMainBlockContainer"] [data-testid="stPageLink"] a p {{
  font-family: var(--ssb-mono); font-weight: 600; color: var(--ssb-vermilion);
  font-size: .8rem; letter-spacing: .06em;
  border-bottom: 1px solid transparent; transition: border-color .14s ease;
}}
[data-testid="stMainBlockContainer"] [data-testid="stPageLink"] a:hover p {{ border-bottom-color: var(--ssb-vermilion); }}

/* Status tag — flat square swatch + label */
.ssb-tag {{ display: inline-flex; align-items: center; gap: .45rem;
  font-family: var(--ssb-mono); font-size: .72rem; font-weight: 600; letter-spacing: .04em;
  padding: .18rem .5rem; border: 1px solid var(--ssb-line-strong); background: var(--ssb-paper-raised);
  color: var(--ssb-text); }}
.ssb-tag .sw {{ width: .55em; height: .55em; flex: 0 0 auto; }}
.ssb-tag.is-running {{ border-color: #C99A3E; color: #8A5A12; }}
.ssb-tag.is-running .sw {{ background: #C99A3E; }}
.ssb-tag.is-completed {{ border-color: #4E8A63; color: #2E6B4E; }}
.ssb-tag.is-completed .sw {{ background: #4E8A63; }}
.ssb-tag.is-failed {{ border-color: var(--ssb-vermilion); color: var(--ssb-vermilion); }}
.ssb-tag.is-failed .sw {{ background: var(--ssb-vermilion); }}
.ssb-tag.is-draft .sw {{ background: var(--ssb-text-faint); }}
.ssb-tag.is-review {{ border-color: #7A5FA6; color: #5B4A78; }}
.ssb-tag.is-review .sw {{ background: #7A5FA6; }}

@media (max-width: 640px) {{ .ssb-hero {{ padding: 1.6rem 1.3rem; }} }}
</style>
"""


def inject_global_styles() -> None:
    """Inject the global stylesheet. Idempotent per rerun; cheap to call."""
    st.markdown(_build_css(), unsafe_allow_html=True)


# ── Small render helpers ────────────────────────────────────────────────────

def eyebrow(title: str, sub: str = "") -> None:
    """A serif section header with a flat vermilion square + hairline rule."""
    sub_html = f" <small>{sub}</small>" if sub else ""
    st.markdown(f'<div class="ssb-eyebrow">{title}{sub_html}</div>', unsafe_allow_html=True)


_STATUS_META = {
    "running": ("is-running", "运行中"),
    "completed": ("is-completed", "已完成"),
    "failed": ("is-failed", "失败"),
    "draft": ("is-draft", "草稿"),
    "needs_revision": ("is-review", "待修订"),
    "paused_for_review": ("is-review", "待确认"),
}


def status_tag_html(status: str) -> str:
    """Return HTML for a flat status tag; safe for unknown statuses."""
    cls, label = _STATUS_META.get(status, ("is-draft", status or "未知"))
    return f'<span class="ssb-tag {cls}"><span class="sw"></span>{label}</span>'


# Backwards-compatible alias (older name).
def status_pill_html(status: str) -> str:  # noqa: D401
    return status_tag_html(status)
