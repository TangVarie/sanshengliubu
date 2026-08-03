"""设置 — API keys, model preferences, Supabase status."""

import streamlit as st
from pipeline.agents.kimi_client import resolve_assist_model
from pipeline.config import (
    ENABLE_KIMI_ASSIST,
    KIMI_ASSIST_MODEL,
    KIMI_ASSIST_MODEL_OVERRIDES,
    MODELS,
    PIPELINE_STAGES,
    VERSION,
    VERSION_DATE,
)
from utils.version_badge import show_version_badge

st.set_page_config(page_title="设置", page_icon="省")
show_version_badge()
st.title("设置")
st.caption(f"当前版本 `{VERSION}` · {VERSION_DATE}")

# ── Connection Status ──────────────────────────────────────────────────────

st.subheader("连接状态")

# Auto-detect active mode: presence of GCP_PROJECT_ID means Vertex; else
# fall back to direct/relay. Direct/relay mode is considered configured
# when EITHER a [claude_relay_presets.*] section exists OR the legacy
# top-level ANTHROPIC_API_KEY is set (backward compat path).
_gcp_project = st.secrets.get("GCP_PROJECT_ID", "")
_api_key = st.secrets.get("ANTHROPIC_API_KEY", "")
_base_url = st.secrets.get("ANTHROPIC_BASE_URL", "")
# Detect new preset format — any non-empty claude_relay_presets dict
# counts as "Claude configured" even without top-level ANTHROPIC_API_KEY.
try:
    _relay_presets_raw = st.secrets.get("claude_relay_presets") or {}
    _has_relay_presets = bool(dict(_relay_presets_raw)) if _relay_presets_raw else False
except Exception:
    _has_relay_presets = False
_active_mode = (
    "vertex" if _gcp_project
    else ("direct" if (_api_key or _has_relay_presets) else "none")
)

col1, col2 = st.columns(2)

with col1:
    st.markdown("**Claude 接入**")
    if _active_mode == "vertex":
        _region = st.secrets.get("GCP_REGION", "us-east5")
        _has_sa = "gcp_service_account" in st.secrets
        st.success(f"Vertex AI · `{_gcp_project}` @ `{_region}`")
        if _has_sa:
            st.caption("Service Account 凭证已就绪")
        else:
            st.caption("回退到 ADC（gcloud auth / env 变量）")
        if _api_key or _has_relay_presets:
            st.warning(
                "同时检测到 Anthropic 配置，但 Vertex 模式优先，它会被忽略。"
                "要切回 Anthropic 模式请删掉 `GCP_PROJECT_ID` 再重启。"
            )
    elif _active_mode == "direct":
        if _has_relay_presets:
            # Multi-preset mode — let the "preset switcher" section below
            # show the details. Here we just confirm the mode is active.
            try:
                _preset_count = len(dict(_relay_presets_raw))
            except Exception:
                _preset_count = "?"
            st.success(
                f"Anthropic 直连/中转（多 preset · 共 {_preset_count} 个）"
            )
            st.caption("下方「中转 Preset 切换」可以一键切换具体 preset。")
        else:
            # Legacy single top-level key mode
            st.success(f"Anthropic 直连/中转 (***{_api_key[-4:]})")
            if _base_url:
                st.info(f"中转：`{_base_url}`")
            else:
                st.caption("直连 Anthropic 官方 API")
    else:
        st.error("未配置")
        st.caption(
            "在 `.streamlit/secrets.toml` 中**二选一**：\n"
            "- 填 `[claude_relay_presets.xxx]` 段（直连/中转，支持多套一键切换）\n"
            "- 或填 `GCP_PROJECT_ID` + `gcp_service_account`（Vertex）"
        )

# ── Claude relay preset switcher ──────────────────────────────────────────
# 只在 Direct/Relay 模式下出现。Vertex 模式没这个概念。
if _active_mode == "direct":
    try:
        from pipeline.agents import (
            get_active_claude_relay_name,
            get_available_claude_relay_presets,
            init_api_config,
        )

        _presets = get_available_claude_relay_presets()
        if _presets:
            st.markdown("##### 中转 Preset 切换")
            _active_now = get_active_claude_relay_name()

            # Check project running to avoid switching mid-run
            try:
                from db.supabase_client import SupabaseClient as _SC
                _db = _SC.get_instance()
                _any_running = any(
                    p.get("status") == "running"
                    for p in (_db.list_projects(limit=50) or [])
                )
            except Exception:
                _any_running = False

            if _any_running:
                st.warning(
                    "有项目正在跑流水线，现在切换可能让进行中的调用换到新后端"
                    "（当前调用用旧配置，下一个调用才用新配置）。建议等任务结束再切。"
                )

            _options = list(_presets.keys())
            _current_idx = (
                _options.index(_active_now) if _active_now in _options else 0
            )
            _picked = st.radio(
                "选择使用哪个中转 preset",
                options=_options,
                index=_current_idx,
                format_func=lambda k: f"`{k}` — {_presets[k].get('label', k)}",
                key="relay_preset_radio",
                help="点下面 Apply 按钮才真正切换。",
            )

            # Show preset details side-by-side for transparency
            _picked_cfg = _presets.get(_picked, {})
            _bits = []
            if _picked_cfg.get("rpm_limit"):
                _bits.append(f"RPM {_picked_cfg['rpm_limit']}")
            if _picked_cfg.get("max_concurrent"):
                _bits.append(f"并发 {_picked_cfg['max_concurrent']}")
            if _picked_cfg.get("supports_cache") is not None:
                _bits.append(
                    "缓存 ✓" if _picked_cfg["supports_cache"] else "缓存 ✗"
                )
            if _picked_cfg.get("supports_adaptive_thinking") is not None:
                _bits.append(
                    "adaptive-thinking ✓"
                    if _picked_cfg["supports_adaptive_thinking"]
                    else "adaptive-thinking ✗ (用 budget_tokens)"
                )
            if _bits:
                st.caption(" · ".join(_bits))

            _col_apply, _col_current = st.columns([1, 2])
            with _col_apply:
                if st.button(
                    "Apply",
                    disabled=(_picked == _active_now),
                    help=(
                        "立即切换到所选 preset。只在当前 Streamlit 进程内生效——"
                        "Cloud Reboot 后会回到 secrets 里的 ACTIVE_CLAUDE_RELAY 默认。"
                    ),
                ):
                    st.session_state["_active_claude_relay_override"] = _picked
                    try:
                        init_api_config()
                        st.success(
                            f"已切换到 `{_picked}` ({_presets[_picked].get('label', _picked)})。"
                            "下一个 API 调用会用新配置。"
                        )
                    except Exception as _err:
                        st.error(f"切换失败：{type(_err).__name__}: {_err}")
            with _col_current:
                st.info(
                    f"**当前生效**：`{_active_now}` — "
                    f"{_presets.get(_active_now, {}).get('label', _active_now)}"
                )

            # Show per-stage model overrides for the SELECTED preset (not
            # necessarily the active one) so the user sees how stages map
            # to model names. Useful for relays like tdyun.ai that use
            # the model-name suffix for thinking tier.
            _picked_overrides = _picked_cfg.get("model_overrides") or {}
            if _picked_overrides:
                with st.expander(
                    f"`{_picked}` 的逐阶段模型映射"
                    f"（{len(_picked_overrides)} 个 stage 被覆盖）",
                    expanded=False,
                ):
                    st.caption(
                        "下面列出的 stage 用 preset 里指定的模型；其他 stage 用 "
                        "`pipeline/config.py` 里 MODELS dict 的默认值。"
                    )
                    _override_rows = [
                        {"stage": k, "model_used": v}
                        for k, v in _picked_overrides.items()
                    ]
                    st.dataframe(_override_rows, use_container_width=True)
                if _picked_cfg.get("thinking_via_model_suffix"):
                    st.caption(
                        "此 preset `thinking_via_model_suffix=true`：不发 JSON "
                        "thinking 参数，靠模型名后缀（-thinking / -high / -medium / "
                        "-low / -max）传递思考档位。"
                    )

            with st.expander("所有 preset 配置对比（只读）"):
                _rows = [
                    {
                        "preset": name,
                        "label": cfg.get("label", ""),
                        "base_url": cfg.get("base_url", "")[:50] or "(默认)",
                        "RPM": cfg.get("rpm_limit") or "默认",
                        "并发": cfg.get("max_concurrent") or "默认",
                        "缓存": cfg.get("supports_cache"),
                        "adaptive": cfg.get("supports_adaptive_thinking"),
                        "key 末 4 位": ("***" + str(cfg.get("api_key", ""))[-4:])
                        if cfg.get("api_key")
                        else "(未填)",
                    }
                    for name, cfg in _presets.items()
                ]
                st.dataframe(_rows, use_container_width=True)
    except Exception as _e:
        st.warning(f"读取 preset 列表异常：{_e}")

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
            st.success(f"已连接 ({supa_url[:30]}...)")
        except Exception as e:
            st.error(f"连接失败：{e}")
    else:
        st.error("未配置")
        st.caption("在 `.streamlit/secrets.toml` 中设置 `SUPABASE_URL` 和 `SUPABASE_KEY`")

# ── SocialDataX trend sampling ─────────────────────────────────────────────

st.divider()
st.subheader("SocialDataX 趋势取样")

_sdx_key = (
    st.secrets.get("SOCIALDATAX_API_KEY", "")
    or st.secrets.get("SOCIAL_MEDIA_MCP_API_KEY", "")
).strip()
try:
    from pipeline.agents.socialdatax_client import is_available as _sdx_ok
    from pipeline.config import (
        ENABLE_SOCIALDATAX_TREND_SCOUT_PRE,
        SOCIALDATAX_TREND_SCOUT_PRE_REQUIRED,
    )

    if _sdx_ok():
        st.success(f"已配置 (***{_sdx_key[-4:]}) · 直连小红书一手数据")
        st.caption(
            "趋势取样(A1)在中书省之前拉真实爆款(原文+互动量)做策略校准。"
            + (
                "当前为 **REQUIRED** 模式:取样失败且无可复用数据时 run 会"
                "提前终止(fail-fast,只消耗太子阶段)。"
                if SOCIALDATAX_TREND_SCOUT_PRE_REQUIRED
                else "当前为 advisory 模式:失败跳过不阻塞。"
            )
        )
    elif ENABLE_SOCIALDATAX_TREND_SCOUT_PRE and \
            SOCIALDATAX_TREND_SCOUT_PRE_REQUIRED:
        st.error(
            "未配置 `SOCIALDATAX_API_KEY`,而趋势取样当前是 **REQUIRED** "
            "模式——**每次 run 都会在取样阶段提前失败**。"
            "去 https://socialdatax.com/?from=npm 申请 Key 填入 "
            "`.streamlit/secrets.toml` 顶层;或把 `pipeline/config.py` 的 "
            "`SOCIALDATAX_TREND_SCOUT_PRE_REQUIRED` 设为 `False` 恢复"
            "跳过不阻塞。"
        )
    else:
        st.warning(
            "未配置 `SOCIALDATAX_API_KEY`(或依赖缺失)。趋势取样将被"
            "跳过。申请:https://socialdatax.com/?from=npm"
        )
except Exception as _sdx_e:  # noqa: BLE001 — settings 页永不因状态区崩
    st.warning(f"SocialDataX 状态检查失败:{_sdx_e}")

# ── Kimi auxiliary ──────────────────────────────────────────────────────────

st.divider()
st.subheader("辅助层（Kimi · 可选）")

_kimi_key = st.secrets.get("MOONSHOT_API_KEY", "").strip()
if not ENABLE_KIMI_ASSIST:
    st.info(
        "已在 `pipeline/config.py` 里禁用（`ENABLE_KIMI_ASSIST=False`）。"
        "如需开启：改成 `True` 并确认 secrets.toml 里配了 `MOONSHOT_API_KEY`。"
    )
elif not _kimi_key:
    st.warning(
        "已在 config 启用但未配 `MOONSHOT_API_KEY`。"
        "辅助层本轮会被跳过（advisory-only，不影响主流程）——但注意主链路"
        "也靠这个 key，没配的话整条流水线都跑不起来。"
    )
else:
    try:
        from pipeline.agents.kimi_client import is_available
        if is_available():
            st.success(
                f"已配置 (***{_kimi_key[-4:]}) · 默认模型 `{KIMI_ASSIST_MODEL}`"
            )
            st.caption(
                "辅助层负责 (1) 网感二审：主链路 critic 判 pass 的 cell 再复核一遍；"
                "(2) 结构审：工部构建后审查 system_prompt 的完整性；"
                "(3) 图片预转写 / 截图分析（Vision）。调用失败一律降级跳过，不阻塞流水线。"
            )

            _role_descriptions: dict[str, str] = {
                "critic":              "网感二审（烟火气判断）",
                "structure_reviewer":  "结构审（system_prompt 完整性）",
                "image_transcriber":   "图片预转写（Vision）",
                "screenshot_analyzer": "截图分析（Vision）",
            }
            with st.expander(
                "按岗位模型分配（pipeline/config.py · KIMI_ASSIST_MODEL_OVERRIDES）",
                expanded=False,
            ):
                _rows = []
                for role, desc in _role_descriptions.items():
                    pinned = KIMI_ASSIST_MODEL_OVERRIDES.get(role)
                    _rows.append(
                        {
                            "岗位 (role)": role,
                            "用途": desc,
                            "实际模型": pinned or f"{KIMI_ASSIST_MODEL} (默认)",
                            "是否 override": "[是]" if pinned else "—",
                        }
                    )
                st.dataframe(_rows, hide_index=True, use_container_width=True)
                st.caption(
                    "`critic` 默认钉在 DeepSeek 而不是 Kimi —— 主链路的 vibe_critic "
                    "已经是 kimi-k2.6，二审再用同一个模型等于自己复核自己，"
                    "分歧仲裁就失去意义了。两个 Vision 岗位必须留在 Kimi："
                    "DeepSeek 这两档不接受图片输入。"
                )

            # Live ping — 实发一条最小请求，验证模型名 / key / 网络三件事。
            if st.button(
                "测试辅助层连接",
                help="实发一条最小请求到 Moonshot，验证模型名/API key/网络都 OK",
            ):
                import time as _time
                with st.spinner(f"正在调用 `{KIMI_ASSIST_MODEL}` ..."):
                    t0 = _time.time()
                    try:
                        from pipeline.agents.kimi_client import (
                            KimiCallFailed,
                            KimiNotConfigured,
                            call_kimi_json,
                        )
                        result = call_kimi_json(
                            system_prompt=(
                                "You are a terse JSON emitter. Output exactly "
                                '{"ok": true, "model": "<model id>"}'
                            ),
                            user_message='Please reply with {"ok": true}.',
                            max_output_tokens=128,
                        )
                        elapsed = _time.time() - t0
                        st.success(
                            f"调用成功 · {elapsed:.2f}s · "
                            f"输入 {result['input_tokens']} tok / "
                            f"输出 {result['output_tokens']} tok · "
                            f"费用 ${result['cost_usd']:.4f}"
                        )
                        st.json(result["data"])
                    except KimiNotConfigured as err:
                        st.error(f"未配置：{err}")
                    except KimiCallFailed as err:
                        st.error(
                            f"调用失败：\n\n```\n{err}\n```\n\n"
                            "**常见原因**：\n"
                            f"- 模型名 `{KIMI_ASSIST_MODEL}` 拼错或你的账号还没开通"
                            " → 去 `pipeline/config.py` 改 `KIMI_ASSIST_MODEL`\n"
                            "- 站点搞错了：`MOONSHOT_API_KEY` 是 platform.moonshot.cn "
                            "（国内站）的 key，但 `MOONSHOT_BASE_URL` 指向了 "
                            "api.moonshot.ai（国际站），或者反过来。两站账号体系独立、"
                            "key 不通用。\n"
                            "- 余额不足 / 该模型的并发配额为 0"
                        )
                    except Exception as err:
                        st.error(f"未预期的错误：{type(err).__name__}: {err}")
        else:
            st.error("辅助层不可用：模型路由不通。")
            st.caption(
                "检查 secrets.toml 里的 `MOONSHOT_API_KEY`，以及 "
                "`MOONSHOT_BASE_URL`（不填默认 https://api.moonshot.cn/anthropic）。"
            )
    except Exception as e:
        st.error(f"状态检测异常：{e}")

# ── Model Configuration ───────────────────────────────────────────────────

st.divider()
st.subheader("模型配置")
st.caption(
    "当前各环节使用的模型（修改需编辑 `pipeline/config.py`）。"
    "`kimi-k3` 是旗舰档（只给策略核心 4 个阶段）、`kimi-k2.6` 是主力档、"
    "`deepseek-v4-flash` 是跨厂家对抗/廉价批量档；辅助层是 advisory，失败不阻塞；"
    "SocialDataX 是趋势取样 + 参考帖抓取的数据源（趋势取样在 REQUIRED 模式下"
    "失败会终止 run，参考帖抓取失败只跳过）。"
)

# 辅助层参与的阶段——跟 orchestrator.run() 的实际调用保持一致。
# 这里列出"辅助层会介入"的阶段，而不是"辅助层是主判"的阶段。
# Tuple shape: (role_key for resolve_assist_model, human description).
#
# NOTE: gemini_reference_analyzer 和 gemini_trend_scout_* 都不在这里 ——
# 前者 v0.32.0 改成纯 SocialDataX 抓取（确定性代码，无 LLM 调用），
# 后者 v0.31.0 就已经迁到 SocialDataX 了。两个 stage key 保留 "gemini_"
# 前缀纯粹是为了 DB 里历史 stage_log 的兼容性。
ASSIST_STAGES: dict[str, tuple[str, str]] = {
    "vibe_critic": (
        "critic",
        "二审（主判 pass 的 cell 再过一遍）",
    ),
    "ministry_works_structure_review": (
        "structure_reviewer",
        "主判（辅助层独跑，结构完整性）",
    ),
}

for stage_key, stage_label, stage_icon in PIPELINE_STAGES:
    model = MODELS.get(stage_key)
    assist = ASSIST_STAGES.get(stage_key)
    assist_role: str | None = None
    assist_desc: str | None = None
    if assist:
        assist_role, assist_desc = assist

    # Assemble the right-hand-side badges based on which backends run here
    parts: list[str] = []
    if model:
        # 档位标签按模型名判断 —— 让"这一步花的是哪一档的钱"在列表里一眼可见。
        if model.startswith("kimi-k3"):
            tier = "旗舰"
        elif model.startswith("kimi-"):
            tier = "主力"
        elif model.startswith("deepseek-"):
            tier = "廉价/异厂家"
        else:
            tier = "其它"
        parts.append(f"`{model}` {tier}")

    # SocialDataX-backed trend sampling — 不是辅助层阶段。
    # Badge must reflect the REQUIRED/fail-fast reality, not the generic
    # "未配置 · 跳过" fallback (which would misdirect a user whose run
    # just died here for lack of an API key).
    if stage_key == "gemini_reference_analyzer":
        parts.append("SocialDataX · 参考帖抓取(用户粘贴的 URL，失败只跳过)")

    if stage_key == "gemini_trend_scout_pre":
        try:
            _available = _sdx_ok()
        except Exception:
            _available = False
        if _available:
            parts.append("SocialDataX · 趋势取样(直连小红书)")
        elif SOCIALDATAX_TREND_SCOUT_PRE_REQUIRED and \
                ENABLE_SOCIALDATAX_TREND_SCOUT_PRE:
            parts.append(
                "SocialDataX · _(未配置 — REQUIRED 模式下 run 会在此"
                "阶段失败,见上方 SocialDataX 区)_"
            )
        else:
            parts.append("SocialDataX · _(未配置 · 跳过)_")

    if assist_desc and ENABLE_KIMI_ASSIST and _kimi_key:
        # 辅助层可用 —— 显示按岗位解析后的实际模型，让 override vs 默认
        # 一眼可见。
        _resolved = resolve_assist_model(assist_role)
        parts.append(f"+ `{_resolved}` 辅助 · {assist_desc}")
    elif assist_desc:
        # 岗位设计了但没配 —— 标注出来，省得用户纳闷这一步为什么日志里
        # 只有主判。
        parts.append(f"+ 辅助 · {assist_desc} _(未配置 · 跳过)_")

    if not parts:
        # e.g. structure_review when 辅助层 disabled + unconfigured
        parts = ["_未配置 · 跳过_"]

    st.markdown(
        f"{stage_icon} **{stage_label}** ({stage_key}) → " + " ".join(parts)
    )

# ── Setup Guide ────────────────────────────────────────────────────────────

st.divider()
st.subheader("设置指南")

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
