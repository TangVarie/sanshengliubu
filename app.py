import streamlit as st

from pipeline.config import VERSION, VERSION_DATE, VERSION_NOTES
from pipeline.logger_utils import install_secret_masking_on_root_logger
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
    page_icon="🏛️",
    layout="wide",
    initial_sidebar_state="expanded",
)

show_version_badge()

# R-019: 单租户运营提醒。schema.sql 对所有主表 DISABLE RLS,
# 这是单租户 MVP 的有意选择。让运营每次进系统看到一眼,万一
# 接第二个客户忘了切换会立即想起来。多租户改造见 README 顶部说明。
st.sidebar.caption(
    "🔓 单租户模式(RLS 关闭)。多客户/多品牌部署需先跑 005 多租户迁移。"
)

st.title(f"🏛️ 三省六部 · Prompt Engineering System  `{VERSION}`")
st.caption(f"Build {VERSION_DATE} — {VERSION_NOTES}")
st.markdown(
    """
    **输入**：产品 brief / 场景需求 / 现有 prompt 迭代诉求
    **输出**：一套完整的、可直接投入生产线使用的 Prompt 系统

    ---

    👈 使用左侧导航开始：
    - **项目总览** — 查看所有项目状态
    - **新建项目** — 提交新的 brief 启动流水线
    - **流水线详情** — 实时查看 pipeline 运行进度
    - **产出中心** — 查看和导出 Prompt 系统
    - **设置** — 配置 API Key 和模型偏好
    """
)
