import streamlit as st

from pipeline.config import VERSION, VERSION_DATE, VERSION_NOTES
from utils.version_badge import show_version_badge

st.set_page_config(
    page_title="三省六部 · Prompt Engineering",
    page_icon="🏛️",
    layout="wide",
    initial_sidebar_state="expanded",
)

show_version_badge()

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
