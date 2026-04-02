"""设置 — API keys, model preferences, Supabase status."""

import streamlit as st
from pipeline.config import MODELS, PIPELINE_STAGES

st.set_page_config(page_title="设置", page_icon="⚙️")
st.title("⚙️ 设置")

# ── Connection Status ──────────────────────────────────────────────────────

st.subheader("连接状态")

col1, col2 = st.columns(2)

with col1:
    st.markdown("**Anthropic API**")
    api_key = st.secrets.get("ANTHROPIC_API_KEY", "")
    base_url = st.secrets.get("ANTHROPIC_BASE_URL", "")
    if api_key and api_key != "sk-ant-...":
        st.success(f"✅ 已配置 (***{api_key[-4:]})")
        if base_url:
            st.info(f"🔀 中转：`{base_url}`")
        else:
            st.caption("直连 Anthropic 官方 API")
    else:
        st.error("❌ 未配置")
        st.caption("在 `.streamlit/secrets.toml` 中设置 `ANTHROPIC_API_KEY`")

with col2:
    st.markdown("**Supabase**")
    supa_url = st.secrets.get("SUPABASE_URL", "")
    supa_key = st.secrets.get("SUPABASE_KEY", "")
    if supa_url and supa_url != "https://xxx.supabase.co":
        try:
            from db.supabase_client import SupabaseClient
            db = SupabaseClient.get_instance()
            # Quick connectivity check
            db.list_projects(limit=1)
            st.success(f"✅ 已连接 ({supa_url[:30]}...)")
        except Exception as e:
            st.error(f"❌ 连接失败：{e}")
    else:
        st.error("❌ 未配置")
        st.caption("在 `.streamlit/secrets.toml` 中设置 `SUPABASE_URL` 和 `SUPABASE_KEY`")

# ── Model Configuration ───────────────────────────────────────────────────

st.divider()
st.subheader("模型配置")
st.caption("当前各环节使用的模型（修改需编辑 `pipeline/config.py`）：")

for stage_key, stage_label, stage_icon in PIPELINE_STAGES:
    model = MODELS.get(stage_key, "未配置")
    tier = "🟣 Opus" if "opus" in model else "🔵 Sonnet"
    st.markdown(f"{stage_icon} **{stage_label}** ({stage_key}) → `{model}` {tier}")

# ── Setup Guide ────────────────────────────────────────────────────────────

st.divider()
st.subheader("📖 设置指南")

with st.expander("Supabase 设置步骤"):
    st.markdown(
        """
1. 访问 [supabase.com](https://supabase.com) 创建免费账户
2. 新建一个 Project，记下 **Project URL** 和 **anon public key**
3. 进入 SQL Editor，粘贴 `db/schema.sql` 中的内容并运行
4. 将 URL 和 Key 填入 `.streamlit/secrets.toml`：
   ```toml
   SUPABASE_URL = "https://your-project.supabase.co"
   SUPABASE_KEY = "eyJhbGciOi..."
   ```
5. 重启 Streamlit 应用
"""
    )

with st.expander("Anthropic API 设置步骤"):
    st.markdown(
        """
1. 访问 [console.anthropic.com](https://console.anthropic.com) 创建 API Key
2. 填入 `.streamlit/secrets.toml`：
   ```toml
   ANTHROPIC_API_KEY = "sk-ant-..."
   ```
3. 重启 Streamlit 应用

**注意**：本系统使用 Opus 和 Sonnet 模型，请确保你的 API 额度支持这些模型。
"""
    )
