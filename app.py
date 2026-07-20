import streamlit as st

from pipeline.config import VERSION, VERSION_DATE
from pipeline.logger_utils import install_secret_masking_on_root_logger
from utils.theme import eyebrow
from utils.version_badge import show_version_badge

# R-023: 任何 logger.exception / traceback 输出到 stderr 之前,
# 都要先剥掉 API key/JWT/Supabase token,防止运维看日志、截图、
# 转发到第三方平台时凭据外泄。Idempotent,多次 import 安全。
install_secret_masking_on_root_logger()

# Collect zombie 'running' runs left behind by a killed/recycled process
# (their heartbeat has gone stale). Throttled + best-effort — see
# utils/liveness.py.
from utils.liveness import maybe_reap_stale_runs

maybe_reap_stale_runs()

st.set_page_config(
    page_title="三省六部 · Prompt Engineering",
    page_icon="省",
    layout="wide",
    initial_sidebar_state="expanded",
)

show_version_badge()

# ── Hero ────────────────────────────────────────────────────────────────────
st.markdown(
    f"""
    <div class="ssb-hero">
      <div class="ssb-hero-kicker">多智能体 · Prompt 工程生产线</div>
      <div class="ssb-hero-title">三省六部 <span class="ssb-accent">·</span> 把散乱需求<br>炼成可交付的 Prompt 系统</div>
      <p class="ssb-hero-lead">
        模拟中国古代「三省六部」政务运作 —— 太子分拣、中书省定策、门下省审议、
        尚书省派发、六部并行、工部组装、终审把关。一份产品 brief 进，一套可直接
        投产的 Prompt 系统出。
      </p>
      <div class="ssb-io">
        <div class="ssb-io-item"><span class="ssb-io-tag">输入</span>
          <span class="ssb-io-text">产品 brief / 场景需求 / 现有 prompt 迭代诉求</span></div>
        <div class="ssb-io-item"><span class="ssb-io-tag">输出</span>
          <span class="ssb-io-text">完整、可直接投入内容生产线的 Prompt 系统</span></div>
      </div>
    </div>
    """,
    unsafe_allow_html=True,
)

# ── Pipeline flow ribbon ─────────────────────────────────────────────────────
eyebrow("流水线一览", "brief 从左到右流经的七道关")

_FLOW = [
    ("皇上", "提交需求", False),
    ("太子", "结构化分拣", False),
    ("中书省", "策略规划", False),
    ("门下省", "独立审议", False),
    ("尚书省", "任务派发", False),
    ("六部", "并行执行", True),
    ("工部", "组装终审", False),
    ("产出", "可交付系统", True),
]
_nodes = []
for i, (name, role, accent) in enumerate(_FLOW):
    cls = "ssb-flow-node is-accent" if accent else "ssb-flow-node"
    _nodes.append(
        f'<div class="{cls}"><div class="n-idx">{i + 1:02d}</div>'
        f'<div class="n-name">{name}</div><div class="n-role">{role}</div></div>'
    )
st.markdown(f'<div class="ssb-flow">{"".join(_nodes)}</div>', unsafe_allow_html=True)

# ── Navigation cards ─────────────────────────────────────────────────────────
eyebrow("从这里开始", "点击进入任意工作区")

_CARDS = [
    ("pages/2_new_project.py", "新建项目",
     "提交产品 brief、上传参考资料与爆文截图，一键启动流水线。", "开始创建 →"),
    ("pages/1_dashboard.py", "项目总览",
     "所有项目的状态、进度与快捷入口，运行中项目自动刷新。", "查看项目 →"),
    ("pages/3_pipeline_detail.py", "流水线详情",
     "实时追踪各环节运行、逐阶段产出与诊断信息。", "进入详情 →"),
    ("pages/4_output_center.py", "产出中心",
     "查看成品提示词清单，导出 Markdown / JSON / 完整数据。", "查看产出 →"),
    ("pages/6_reference_library.py", "参考样本库",
     "录入「封面+标题+正文+评论」四位一体证据包，供网感复检检索。", "管理样本 →"),
    ("pages/5_settings.py", "设置",
     "配置 Claude 接入、Supabase、Gemini 辅助与逐阶段模型。", "打开设置 →"),
]

for row in range(0, len(_CARDS), 3):
    cols = st.columns(3, gap="medium")
    for i, (col, card) in enumerate(zip(cols, _CARDS[row:row + 3])):
        target, title, desc, cta = card
        with col:
            with st.container(border=True):
                st.markdown(
                    f'<div class="ssb-card-head">'
                    f'<span class="ssb-card-idx">{row + i + 1:02d}</span>'
                    f'<span class="ssb-card-title">{title}</span></div>'
                    f'<div class="ssb-card-desc">{desc}</div>',
                    unsafe_allow_html=True,
                )
                st.page_link(target, label=cta)

st.markdown(
    f'<p style="text-align:center;color:var(--ssb-text-faint);font-size:.8rem;'
    f'margin-top:2.4rem;letter-spacing:.03em">三省六部 · Prompt Engineering System '
    f'&nbsp;·&nbsp; {VERSION} &nbsp;·&nbsp; {VERSION_DATE}</p>',
    unsafe_allow_html=True,
)
