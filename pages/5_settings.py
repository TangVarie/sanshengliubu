"""设置 — API keys, model preferences, Supabase status."""

import streamlit as st
from pipeline.config import (
    ENABLE_GEMINI_ASSIST,
    GEMINI_MODEL,
    MODELS,
    PIPELINE_STAGES,
    VERSION,
    VERSION_DATE,
    VERSION_NOTES,
)
from utils.version_badge import show_version_badge

st.set_page_config(page_title="设置", page_icon="⚙️")
show_version_badge()
st.title("⚙️ 设置")
st.info(f"**当前版本** `{VERSION}` · {VERSION_DATE} — {VERSION_NOTES}")

# ── Connection Status ──────────────────────────────────────────────────────

st.subheader("连接状态")

# Auto-detect active mode: presence of GCP_PROJECT_ID means Vertex; else
# fall back to direct/relay. This matches init_api_config()'s logic in
# pipeline/agents/__init__.py so the page never lies about which backend
# will actually be used at runtime.
_gcp_project = st.secrets.get("GCP_PROJECT_ID", "")
_api_key = st.secrets.get("ANTHROPIC_API_KEY", "")
_base_url = st.secrets.get("ANTHROPIC_BASE_URL", "")
_active_mode = "vertex" if _gcp_project else ("direct" if _api_key else "none")

col1, col2 = st.columns(2)

with col1:
    st.markdown("**Claude 接入**")
    if _active_mode == "vertex":
        _region = st.secrets.get("GCP_REGION", "us-east5")
        _has_sa = "gcp_service_account" in st.secrets
        st.success(f"✅ Vertex AI · `{_gcp_project}` @ `{_region}`")
        if _has_sa:
            st.caption("🔑 Service Account 凭证已就绪")
        else:
            st.caption("🔑 回退到 ADC（gcloud auth / env 变量）")
        if _api_key:
            st.warning(
                "⚠️ 同时检测到 `ANTHROPIC_API_KEY`，但 Vertex 模式优先，它会被忽略。"
                "要切回 Anthropic 模式请删掉 `GCP_PROJECT_ID` 再重启。"
            )
    elif _active_mode == "direct":
        st.success(f"✅ Anthropic 直连/中转 (***{_api_key[-4:]})")
        if _base_url:
            st.info(f"🔀 中转：`{_base_url}`")
        else:
            st.caption("直连 Anthropic 官方 API")
    else:
        st.error("❌ 未配置")
        st.caption(
            "在 `.streamlit/secrets.toml` 中**二选一**：\n"
            "- 填 `ANTHROPIC_API_KEY`（直连/中转）\n"
            "- 或填 `GCP_PROJECT_ID` + `gcp_service_account`（Vertex）"
        )

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

# ── Gemini auxiliary ────────────────────────────────────────────────────────

st.divider()
st.subheader("Gemini 辅助检查（可选）")

_gemini_key = st.secrets.get("VERTEX_EXPRESS_API_KEY", "").strip()
if not ENABLE_GEMINI_ASSIST:
    st.info(
        "ℹ️ 已在 `pipeline/config.py` 里禁用（`ENABLE_GEMINI_ASSIST=False`）。"
        "如需开启：改成 `True` 并在 secrets.toml 配 `VERTEX_EXPRESS_API_KEY`。"
    )
elif not _gemini_key:
    st.warning(
        "⚠️ 已在 config 启用但未配 `VERTEX_EXPRESS_API_KEY`。"
        "Gemini 辅助检查本轮会被跳过（advisory-only，不影响主流程）。"
        "在 GCP Console → Vertex AI → Settings → API keys 生成一个 Key 填入 secrets.toml。"
    )
else:
    # Don't actually make a network call here — just confirm the client
    # can be constructed (catches missing `google-genai` install).
    try:
        from pipeline.agents.gemini_client import is_available
        if is_available():
            st.success(
                f"✅ 已配置 (***{_gemini_key[-4:]}) · 模型 `{GEMINI_MODEL}`"
            )
            st.caption(
                "Gemini 会作为 (1) 网感二审：Claude critic 判 pass 的 cell 由 Gemini "
                "二次复核；(2) 结构审：工部构建后审查 system_prompt 的完整性。"
                "调用失败会降级为只用 Claude 结果。"
            )

            # Live ping — sends a trivial request to verify the model ID,
            # API key, and network path all work end-to-end. Crucial for
            # diagnosing "why does Gemini always show as skipped".
            if st.button(
                "🧪 测试 Gemini 连接",
                help="实发一条最小请求到 Vertex，验证模型名/API key/网络都 OK",
            ):
                import time as _time
                with st.spinner(f"正在调用 `{GEMINI_MODEL}` ..."):
                    t0 = _time.time()
                    try:
                        from pipeline.agents.gemini_client import (
                            GeminiCallFailed,
                            GeminiNotConfigured,
                            call_gemini_json,
                        )
                        result = call_gemini_json(
                            system_prompt=(
                                "You are a terse JSON emitter. Output exactly "
                                '{"ok": true, "model": "<model id>"}'
                            ),
                            user_message='Please reply with {"ok": true}.',
                            max_output_tokens=128,
                        )
                        elapsed = _time.time() - t0
                        st.success(
                            f"✅ 调用成功 · {elapsed:.2f}s · "
                            f"输入 {result['input_tokens']} tok / "
                            f"输出 {result['output_tokens']} tok · "
                            f"费用 ${result['cost_usd']:.4f}"
                        )
                        st.json(result["data"])
                    except GeminiNotConfigured as err:
                        st.error(f"❌ 未配置：{err}")
                    except GeminiCallFailed as err:
                        st.error(
                            f"❌ 调用失败：\n\n```\n{err}\n```\n\n"
                            "**常见原因**：\n"
                            f"- 模型名 `{GEMINI_MODEL}` 你的 Vertex 账户不可用"
                            " → 去 `pipeline/config.py` 改 `GEMINI_MODEL`"
                            "（推荐先试 `gemini-2.5-pro` 或 `gemini-2.5-flash`）\n"
                            "- API key 对应的项目没开 Vertex AI API\n"
                            "- 地区配额问题"
                        )
                    except Exception as err:
                        st.error(f"❌ 未预期的错误：{type(err).__name__}: {err}")

            # List models — answers "what models can this API key actually
            # call" without needing to trial-and-error through pipeline runs.
            if st.button(
                "📋 列出可用 Gemini 模型",
                help=(
                    "调用 ListModels 接口，显示你这个 API key 能访问的所有"
                    "模型。复制其中一个模型 ID 填到 pipeline/config.py 的 "
                    "GEMINI_MODEL 即可切换。"
                ),
            ):
                with st.spinner("正在拉取模型列表 ..."):
                    try:
                        from pipeline.agents.gemini_client import (
                            GeminiCallFailed,
                            GeminiNotConfigured,
                            list_available_models,
                        )
                        models = list_available_models()
                        if not models:
                            st.warning("⚠️ ListModels 返回空列表——key 可能有权限问题。")
                        else:
                            # Flag whether the currently configured GEMINI_MODEL
                            # is actually in the list.
                            available_ids = {m["id"] for m in models}
                            current_ok = GEMINI_MODEL in available_ids
                            if current_ok:
                                st.success(
                                    f"✅ 当前配置的 `{GEMINI_MODEL}` "
                                    f"在可用列表里（共 {len(models)} 个模型）"
                                )
                            else:
                                st.warning(
                                    f"⚠️ 当前配置 `{GEMINI_MODEL}` **不在**可用列表里——"
                                    f"这就是为什么 Gemini 总是 skipped。"
                                    f"从下表挑一个支持 `generateContent` 的模型 ID，"
                                    f"改 `pipeline/config.py` 里的 `GEMINI_MODEL`。"
                                )
                            # Render compact table — most relevant columns only
                            rows = [
                                {
                                    "Model ID (填到 GEMINI_MODEL)": m["id"],
                                    "Display Name": m["display_name"],
                                    "Input tokens": f"{m['input_token_limit']:,}"
                                    if m["input_token_limit"]
                                    else "?",
                                    "Output tokens": f"{m['output_token_limit']:,}"
                                    if m["output_token_limit"]
                                    else "?",
                                    "Methods": ", ".join(m["supported_methods"])
                                    if m["supported_methods"]
                                    else "(unknown)",
                                }
                                for m in models
                            ]
                            st.dataframe(rows, use_container_width=True)
                    except GeminiNotConfigured as err:
                        st.error(f"❌ 未配置：{err}")
                    except GeminiCallFailed as err:
                        st.error(f"❌ ListModels 失败：\n\n```\n{err}\n```")
                    except Exception as err:
                        st.error(f"❌ 未预期的错误：{type(err).__name__}: {err}")
        else:
            st.error("❌ Gemini 客户端初始化失败（`google-genai` 未装？）")
            st.caption(
                "`pip install google-genai>=0.3.0`（或重新跑 "
                "`pip install -r requirements.txt`）后重启 Streamlit。"
            )
    except Exception as e:
        st.error(f"❌ 状态检测异常：{e}")

# ── Model Configuration ───────────────────────────────────────────────────

st.divider()
st.subheader("模型配置")
st.caption(
    "当前各环节使用的模型（修改需编辑 `pipeline/config.py`）。"
    "🟣 Opus / 🔵 Sonnet 都是 Claude；🟢 Gemini 是辅助（advisory），失败不阻塞。"
)

# Gemini 参与的阶段——跟 orchestrator.run() 的实际调用保持一致。
# 这里列出"Gemini 会介入"的阶段，而不是"Gemini 是主判"的阶段。
GEMINI_ASSIST_STAGES: dict[str, str] = {
    "gemini_trend_scout_pre": "主判（Gemini-only，Google 搜小红书原文）",
    "vibe_critic": "二审（Claude 判 pass 的 cell 再过 Gemini）",
    "ministry_works_structure_review": "主判（Gemini-only，结构完整性）",
}

for stage_key, stage_label, stage_icon in PIPELINE_STAGES:
    model = MODELS.get(stage_key)
    gemini_role = GEMINI_ASSIST_STAGES.get(stage_key)

    # Assemble the right-hand-side badges based on which backends run here
    parts: list[str] = []
    if model:
        tier = "🟣 Opus" if "opus" in model else "🔵 Sonnet"
        parts.append(f"`{model}` {tier}")

    if gemini_role and ENABLE_GEMINI_ASSIST and _gemini_key:
        # Gemini actually available
        parts.append(f"+ `{GEMINI_MODEL}` 🟢 Gemini · {gemini_role}")
    elif gemini_role:
        # Gemini role designed but not configured — annotate so user
        # understands why this stage may look "only Claude" in logs
        parts.append(f"+ 🟢 Gemini · {gemini_role} _(未配置 · 跳过)_")

    if not parts:
        # e.g. structure_review when Gemini disabled + unconfigured
        parts = ["_未配置 · 跳过_"]

    st.markdown(
        f"{stage_icon} **{stage_label}** ({stage_key}) → " + " ".join(parts)
    )

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

st.markdown(
    "**Claude 接入模式二选一**。系统启动时自动探测：只要有 `GCP_PROJECT_ID`"
    "就走 Vertex，否则走 Anthropic 直连/中转。两块**不要同时填**，"
    "避免误判或浪费 Key。"
)

with st.expander("方式 A · Anthropic 直连/中转 设置步骤"):
    st.markdown(
        """
1. 访问 [console.anthropic.com](https://console.anthropic.com) 创建 API Key
2. 填入 `.streamlit/secrets.toml`：
   ```toml
   ANTHROPIC_API_KEY = "sk-ant-..."
   # 可选，走代理/中转时填，不填就是直连官方
   ANTHROPIC_BASE_URL = ""
   ```
3. 重启 Streamlit 应用

**适用**：个人开发、小量调用。
**注意**：本系统使用 Opus 和 Opus-thinking 模型，请确保你的 API 额度
支持这些模型。如果走中转站，请确认它支持：
- 模型名后缀 `-thinking`（思考型阶段靠它路由）
- `cache_control: ephemeral` 字段（不支持就把 `ENABLE_PROMPT_CACHING` 关掉）
"""
    )

with st.expander("方式 B · Vertex AI 设置步骤"):
    st.markdown(
        """
1. 在 Google Cloud Console 开通 **Vertex AI API** 并申请 Claude 模型访问权限
   （Claude on Vertex 需要白名单）
2. 为项目创建一个 Service Account，授予 `Vertex AI User` 角色，下载 JSON Key
3. 填入 `.streamlit/secrets.toml`：
   ```toml
   GCP_PROJECT_ID = "your-project-id"
   GCP_REGION     = "asia-southeast1"  # 或其他支持 Claude 的 region

   [gcp_service_account]
   type = "service_account"
   project_id = "..."
   private_key_id = "..."
   private_key = "-----BEGIN PRIVATE KEY-----\\n...\\n-----END PRIVATE KEY-----\\n"
   client_email = "..."
   client_id = "..."
   # ...其余字段按 JSON 原样铺平
   ```
4. 重启 Streamlit 应用

**适用**：生产部署、企业合规、高配额需求。
**注意**：adaptive thinking（让模型自行决定思考深度）只有 Vertex 原生支持；
走 Anthropic 直连/中转则退化为固定 `budget_tokens=THINKING_BUDGET_TOKENS`。
"""
    )
