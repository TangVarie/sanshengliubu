"""Base agent class for all pipeline agents."""

from __future__ import annotations

import asyncio
import collections
import contextlib
import json
import re
import threading
import time
import traceback
from pathlib import Path
from typing import Any

import logging

import anthropic
import streamlit as st

logger = logging.getLogger(__name__)

from db.supabase_client import SupabaseClient
from pipeline.config import (
    MODEL_FALLBACK_CHAIN,
    CLAUDE_MAX_CONCURRENT,
    CLAUDE_RPM_ADAPTIVE,
    CLAUDE_RPM_BACKOFF_FACTOR,
    CLAUDE_RPM_CEILING,
    CLAUDE_RPM_LIMIT,
    CLAUDE_RPM_RECOVERY_SECONDS,
    CLAUDE_RPM_RECOVERY_STEP,
    COST_PER_1M_INPUT,
    COST_PER_1M_OUTPUT,
    ENABLE_PROMPT_CACHING,
    MAX_RETRIES,
    MAX_TOKENS_DEFAULT,
    MAX_TOKENS_PER_RUN,
    MODELS,
    RETRY_BASE_DELAY_SECONDS,
    STAGE_MAX_TOKENS,
    THINKING_BUDGET_TOKENS,
    THINKING_STAGES,
)
from pipeline.logger_utils import mask_secrets

PROMPTS_DIR = Path(__file__).parent.parent / "prompts"


# ── Vendor routing ────────────────────────────────────────────────────────
# Every backend we talk to speaks the Anthropic Messages API, so the only
# thing that varies per vendor is (base_url, api_key) + which optional
# request fields the endpoint actually honors. One helper resolves the
# family from the model name; everything else keys off its return value.
#
#   "kimi"     — Moonshot,          api.moonshot.cn/anthropic
#   "deepseek" — DeepSeek 官方,      api.deepseek.com/anthropic
#   "gpt"      — OpenAI-compat 中转, 走 _call_openai_chat(不是 anthropic SDK)
#   "claude"   — 历史 relay / Vertex preset。v0.32.0 起没有任何 stage 默认
#                指向它,但 secrets 里配了 claude_relay_presets 的老部署
#                仍然能用 model_overrides 把某个 stage 钉回 Claude。
def _model_vendor(model: str) -> str:
    m = (model or "").lower()
    if m.startswith("kimi-") or m.startswith("moonshot-"):
        return "kimi"
    if m.startswith("deepseek-"):
        return "deepseek"
    if m.startswith("gpt-"):
        return "gpt"
    return "claude"


# Which vendors accept the standard Anthropic JSON `thinking` field.
#
# Kimi: Moonshot's /anthropic endpoint documents thinking.type and maps the
#   enabled levels onto adaptive reasoning, so K2.6/K3 get real extended
#   thinking through the standard parameter.
# DeepSeek: the anthropic-compat endpoint silently ignores both
#   thinking.budget_tokens and cache_control (reported repeatedly against
#   several Anthropic-compat shims). Sending it isn't an error, it's just
#   dead weight in every request — so we don't. DeepSeek stages therefore
#   log "thinking ✗", which is honest rather than a silent no-op.
_THINKING_CAPABLE_VENDORS = frozenset({"kimi", "claude"})

# Vendor → (secrets 字段名, 人话名字)。仅用于拼报错文案。
_VENDOR_SECRET_HINT = {
    "kimi": ("MOONSHOT_API_KEY", "Moonshot (Kimi)"),
    "deepseek": ("DEEPSEEK_API_KEY", "DeepSeek"),
}


def backend_configured(model: str) -> bool:
    """这个模型的 backend 配齐了吗?(不构造 client,不发请求)

    fallback 链会跨厂家降级,所以在真正尝试之前得能问一句"下一个候选有 key
    吗" —— 否则 "DEEPSEEK_API_KEY 没配" 会被 fallback 循环当成一次普通失败,
    链走完后抛出的错跟真实原因对不上。
    """
    if not _api_config:
        init_api_config()
    vendor = _model_vendor(model)
    if vendor in ("kimi", "deepseek"):
        cfg = _api_config.get(vendor)
        return bool(cfg and cfg.get("api_key"))
    if vendor == "gpt":
        cfg = _api_config.get("vectorengine")
        return bool(cfg and cfg.get("api_key"))
    # claude:Vertex 模式恒可用;relay 模式要有 preset key
    if _api_config.get("mode") == "vertex":
        return True
    return bool(_api_config.get("api_key"))


def get_client_for_model(model: str) -> anthropic.Anthropic:
    """Select the right Anthropic-compatible client based on model name.

    多 vendor 路由(v0.30.0 引入,v0.32.0 加 Kimi):
    - `kimi-*`     → Moonshot 官方 anthropic-compat 端点
    - `deepseek-*` → DeepSeek 官方 anthropic-compat 端点
    - `gpt-*`      → 不走这里(_call_model 提前 dispatch 到
                     _call_openai_chat,OpenAI SDK)
    - `claude-*`   → 当前激活 relay preset 的 base_url,或 Vertex

    每家都是独立 base_url + 独立 api_key,所以 client 必须按 effective_model
    挑,不能全局复用一个。

    模块级函数(不是 BaseAgent 方法),因为辅助层 kimi_client.py 不走
    BaseAgent 那套 stage_log/预算/重试体系,但需要完全相同的路由规则 ——
    复制一份迟早会漂移。
    """
    if not _api_config:
        init_api_config()

    _mlow = (model or "").lower()
    vendor = _model_vendor(model)

    # Kimi / DeepSeek:同构的"独立端点 + 独立 key",走同一段。
    if vendor in ("kimi", "deepseek"):
        cfg = _api_config.get(vendor)
        if not cfg or not cfg.get("api_key"):
            _field, _name = _VENDOR_SECRET_HINT[vendor]
            raise RuntimeError(
                f"模型 {model!r} 需要 {_name} backend,但 "
                f"secrets.toml 里没配 {_field}。"
                "去 .streamlit/secrets.toml 加上 "
                f"`{_field} = \"sk-...\"` 后重启。"
            )
        return anthropic.Anthropic(
            api_key=cfg["api_key"],
            base_url=cfg["base_url"],
            timeout=900.0,
        )

    # Vertex 模式只能跑 Claude(GPT 也不行,Vertex AI Anthropic 是
    # Anthropic-only endpoint)
    if _api_config.get("mode") == "vertex":
        if _mlow.startswith("gpt-"):
            raise RuntimeError(
                f"Vertex 模式不支持 GPT 模型 {model!r}。"
                "需要切换到 relay preset(支持多模型路由的中转)"
                "或在 GCP_PROJECT_ID 之外另配 ANTHROPIC_API_KEY。"
            )
        from anthropic import AnthropicVertex
        vertex_kwargs: dict[str, Any] = {
            "project_id": _api_config["project_id"],
            "region": _api_config["region"],
            "timeout": 900.0,
        }
        if "credentials" in _api_config:
            vertex_kwargs["credentials"] = _api_config["credentials"]
        return AnthropicVertex(**vertex_kwargs)

    # Direct relay/Anthropic 模式 — 历史 Claude 中转路径。
    # v0.32.0 起默认配置里没有任何 stage 指向 claude-*,所以走到这里说明是
    # 老部署,或者有人手动把某个 stage override 回了 Claude。
    _relay_key = _api_config.get("api_key") or ""
    if not _relay_key:
        raise RuntimeError(
            f"模型 {model!r} 需要 Claude 接入(中转或官方),但 secrets.toml "
            "里既没有 [claude_relay_presets.*] 也没有 ANTHROPIC_API_KEY。"
            "v0.32.0 起主链路默认全是 kimi-* / deepseek-*,不需要 Claude —— "
            "如果你没有刻意配 model_overrides 把这个 stage 钉回 Claude，"
            "多半是哪里还留着旧的模型名，检查 secrets.toml 的 "
            "model_overrides 和 pipeline/config.py 的 MODELS。"
        )
    kwargs = {"api_key": _relay_key}
    if _api_config.get("base_url"):
        kwargs["base_url"] = _api_config["base_url"]
    kwargs["timeout"] = 900.0
    return anthropic.Anthropic(**kwargs)




# Relay "no upstream channel" phrasings, English + common Chinese-relay
# variants (tdyun/vectorengine/等国内中转 return Chinese). Matching these
# routes the model through the Claude fallback chain instead of failing the
# whole stage. Kept specific enough to NOT swallow generic auth/quota errors.
_NO_CHANNEL_MARKERS = (
    "no available channel",
    "no channel for model",
    "无可用渠道",
    "无可用通道",
    "没有可用渠道",
    "当前分组下对于模型",   # tdyun: "当前分组下对于模型 X 无可用渠道"
    "上游负载已饱和",
    "无可用的渠道",
)


def _is_no_channel_error(exc: BaseException) -> bool:
    """True if the error looks like a relay "no upstream channel for this
    model" rejection (English or Chinese-relay phrasing). Such errors are
    routing failures, not model failures — they should trigger the Claude
    fallback chain in _call_model, not fail the stage."""
    msg = str(exc).lower()
    return any(m.lower() in msg for m in _NO_CHANNEL_MARKERS)


# Rate-limit phrasings across vendors. Used to (a) trip the adaptive-RPM
# safety valve and (b) classify the error as transient with long backoff.
_RATE_LIMIT_MARKERS = (
    "429", "rate limit", "rate_limit", "too many request",
    "请求过于频繁", "请求频率", "限流", "quota", "overloaded",
)


def _is_rate_limit_error(exc: BaseException) -> bool:
    """True for 429 / rate-limit / overloaded errors from any vendor."""
    try:
        import anthropic as _anthropic
        if isinstance(exc, _anthropic.RateLimitError):
            return True
    except Exception:
        pass
    try:
        import openai as _openai
        if isinstance(exc, getattr(_openai, "RateLimitError", ())):
            return True
    except Exception:
        pass
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if status == 429:
        return True
    msg = str(exc).lower()
    return any(m in msg for m in _RATE_LIMIT_MARKERS)


def _classify_llm_error(exc: BaseException, attempt: int) -> tuple[bool, bool]:
    """Vendor-agnostic error classification. Returns (retryable, is_5xx).

    Works for anthropic.* AND openai.* AND relay errors surfaced as plain
    strings — the old code only isinstance'd anthropic.*, so every GPT
    (openai.*) error fell through to the default 'retryable=True', wasting
    retries on auth/400 and using the wrong backoff for 5xx.
    """
    # Non-retryable: auth / permission / malformed request (except the
    # cache_control fault, which _call_model self-heals by retrying).
    import anthropic as _anthropic
    _low = str(exc).lower()
    _status = getattr(exc, "status_code", None) or getattr(exc, "status", None)

    _auth_types: tuple = (
        _anthropic.AuthenticationError,
        _anthropic.PermissionDeniedError,
    )
    _badreq_types: tuple = (_anthropic.BadRequestError,)
    try:
        import openai as _openai
        _auth_types += (
            getattr(_openai, "AuthenticationError", ()),
            getattr(_openai, "PermissionDeniedError", ()),
        )
        _badreq_types += (getattr(_openai, "BadRequestError", ()),)
    except Exception:
        pass

    if isinstance(exc, _auth_types) or _status in (401, 403):
        return (False, False)
    if _status == 400 or isinstance(exc, _badreq_types):
        # A 400 that's a cache_control fault IS retryable (self-heal path).
        return ("cache_control" in _low, False)
    # No-channel / rate-limit / 5xx / timeout / connection = transient.
    _is_5xx = bool(
        (_status is not None and 500 <= int(_status) < 600)
        or isinstance(
            exc,
            (_anthropic.InternalServerError, _anthropic.APIConnectionError),
        )
        or any(s in _low for s in ("500", "502", "503", "504", "bad gateway",
                                   "service unavailable", "gateway timeout",
                                   "internal server", "connection", "timed out",
                                   "timeout", "eof occurred", "broken pipe"))
    )
    # JSON parse failures: allow at most one retry (transient model weirdness).
    if isinstance(exc, (ValueError, json.JSONDecodeError)):
        return (attempt < 1, False)
    return (True, _is_5xx)


def note_rate_limit_hit(stage_name: str = "") -> None:
    """Module-level hook: tell the active limiter a 429 was observed so its
    adaptive safety valve backs the effective RPM off toward the floor."""
    lim = _get_active_limiter()
    if lim is not None:
        lim.note_rate_limited(stage_name)


# ── API config cache (populated in main thread, used by background threads) ──
_api_config: dict[str, str] = {}
# Guard the narrow write-during-run path in _call_model: when the backend
# rejects cache_control with 400, multiple concurrent agent threads can all
# hit the same fault simultaneously. Without this lock N threads each do a
# redundant fallback retry before any of them flips the "cache disabled" bit
# in _api_config. Init-time writes to _api_config are single-threaded so they
# don't need the lock.
_api_config_lock = threading.Lock()


def _list_claude_relay_presets() -> dict[str, dict]:
    """Read all [claude_relay_presets.xxx] sections from secrets.toml
    into a plain dict keyed by preset name. Empty if none defined.

    Falls back to a single synthesized preset named "default" when the
    user has only the legacy top-level ANTHROPIC_API_KEY / BASE_URL set
    — keeps backward compat with secrets.toml files written before the
    multi-preset feature.
    """
    raw = st.secrets.get("claude_relay_presets")
    presets: dict[str, dict] = {}
    if raw:
        for name, cfg in dict(raw).items():
            cfg_d = dict(cfg)
            presets[str(name)] = cfg_d

    if not presets:
        # Legacy fallback: if user hasn't migrated to multi-preset yet,
        # wrap their top-level ANTHROPIC_API_KEY into a synthetic preset
        # so the rest of the plumbing (init_api_config / settings page)
        # has a uniform structure to work with.
        legacy_key = st.secrets.get("ANTHROPIC_API_KEY", "")
        if legacy_key:
            presets["default"] = {
                "label": "默认（来自顶层 ANTHROPIC_API_KEY）",
                "api_key": legacy_key,
                "base_url": st.secrets.get("ANTHROPIC_BASE_URL", ""),
                "rpm_limit": None,  # use global defaults
                "max_concurrent": None,
                "supports_cache": None,  # use ENABLE_PROMPT_CACHING
                "supports_adaptive_thinking": None,  # infer from model
            }
    return presets


def get_available_claude_relay_presets() -> dict[str, dict]:
    """Public accessor for UI callers. Same as internal helper."""
    return _list_claude_relay_presets()


def get_active_claude_relay_name() -> str:
    """Return the preset name currently in effect. Priority:
      1. Streamlit session override (set by the Settings page's switch
         button; only persists within the current Streamlit process)
      2. secrets.toml top-level ACTIVE_CLAUDE_RELAY
      3. First preset key in secrets.toml alphabetical order
      4. "" if no presets at all (Vertex mode or totally unconfigured)
    """
    # Session override takes precedence so the UI switch button works
    # without needing a restart.
    try:
        override = st.session_state.get("_active_claude_relay_override")
        if override:
            return str(override)
    except Exception:
        # Not in a Streamlit context (e.g. called from a background
        # thread before init_api_config has cached anything). Fall
        # through to the secrets lookup.
        pass

    presets = _list_claude_relay_presets()
    if not presets:
        return ""
    default = st.secrets.get("ACTIVE_CLAUDE_RELAY", "")
    if default and default in presets:
        return str(default)
    return next(iter(presets.keys()))


def init_api_config():
    """Call from Streamlit main thread to cache API secrets for background use.

    Two backends (auto-detected):
      - Vertex AI: GCP_PROJECT_ID + GCP_REGION (+ gcp_service_account)
      - Relay/Direct: one of the [claude_relay_presets.*] in secrets,
        picked by ACTIVE_CLAUDE_RELAY or session override. Falls back
        to legacy top-level ANTHROPIC_API_KEY when no presets defined.
    """
    # DeepSeek / VectorEngine 是独立 backend,跟 Claude 走 Vertex 还是 relay
    # 无关 — 任一模式下 stage 用 deepseek-*/gpt-* override 都应该能找到
    # backend。所以这两个的初始化必须放在 mode 判定之前;以前(v0.30.0/0.30.7
    # 引入时)关在 relay 分支里,Vertex 部署下永远拿不到 DS/VE 配置,GPT 阶段
    # 在 _call_openai_chat 报"未配 VectorEngine",DeepSeek 阶段同理。
    # v0.32.0: Moonshot (Kimi) 是换厂后的主链路 backend。跟 DeepSeek 一样
    # 是"独立 base_url + 独立 key"的 anthropic-compat 端点,所以初始化同样
    # 放在 mode 判定之前 —— Vertex / relay / 什么都没配,三种情况下 kimi-*
    # 都必须能找到自己的 backend。
    #
    # 国内站 api.moonshot.cn 和国际站 api.moonshot.ai 是两套独立账号体系,
    # key 不通用。默认走国内站;要换国际站就在 secrets.toml 里设
    # MOONSHOT_BASE_URL = "https://api.moonshot.ai/anthropic"。
    _kimi_key = (st.secrets.get("MOONSHOT_API_KEY") or "").strip()
    if _kimi_key:
        _api_config["kimi"] = {
            "api_key": _kimi_key,
            "base_url": (
                st.secrets.get("MOONSHOT_BASE_URL")
                or "https://api.moonshot.cn/anthropic"
            ).rstrip("/"),
        }
        logger.info(
            "[init_api_config] Moonshot (Kimi) anthropic-compat 端点已配置 "
            "(base=%s)",
            _api_config["kimi"]["base_url"],
        )
    else:
        _api_config.pop("kimi", None)

    _ds_key = (st.secrets.get("DEEPSEEK_API_KEY") or "").strip()
    if _ds_key:
        _api_config["deepseek"] = {
            "api_key": _ds_key,
            "base_url": (
                st.secrets.get("DEEPSEEK_BASE_URL")
                or "https://api.deepseek.com/anthropic"
            ).rstrip("/"),
        }
        logger.info(
            "[init_api_config] DeepSeek anthropic-compat 端点已配置 "
            "(base=%s)",
            _api_config["deepseek"]["base_url"],
        )
    else:
        _api_config.pop("deepseek", None)

    _ve_key = (st.secrets.get("VECTORENGINE_API_KEY") or "").strip()
    if _ve_key:
        _api_config["vectorengine"] = {
            "api_key": _ve_key,
            "base_url": (
                st.secrets.get("VECTORENGINE_BASE_URL")
                or "https://api.vectorengine.ai/v1"
            ).rstrip("/"),
        }
        logger.info(
            "[init_api_config] VectorEngine OpenAI-compat 端点已配置 "
            "(base=%s),GPT 系列将通过这里调用",
            _api_config["vectorengine"]["base_url"],
        )
    else:
        _api_config.pop("vectorengine", None)

    # Vertex AI mode
    if st.secrets.get("GCP_PROJECT_ID"):
        _api_config["mode"] = "vertex"
        _api_config["project_id"] = st.secrets["GCP_PROJECT_ID"]
        _api_config["region"] = st.secrets.get("GCP_REGION", "us-east5")

        # Build credentials from service account JSON in secrets (for Streamlit Cloud)
        if "gcp_service_account" in st.secrets:
            from google.oauth2 import service_account
            sa_info = dict(st.secrets["gcp_service_account"])
            credentials = service_account.Credentials.from_service_account_info(
                sa_info,
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )
            _api_config["credentials"] = credentials
            logger.info(
                f"[init_api_config] Vertex AI mode (Service Account): "
                f"project={_api_config['project_id']}, region={_api_config['region']}, "
                f"sa={sa_info.get('client_email', '?')}"
            )
        else:
            # No service account in secrets → rely on ADC (gcloud auth / env var)
            logger.info(
                f"[init_api_config] Vertex AI mode (ADC): "
                f"project={_api_config['project_id']}, region={_api_config['region']}"
            )
        return

    # ── Relay / direct mode ──────────────────────────────────────────
    presets = _list_claude_relay_presets()
    active_name = get_active_claude_relay_name()

    if not presets or not active_name:
        # v0.32.0: 没有 Claude 中转不再是致命错误 —— 换厂后的默认配置里
        # 根本不该有 claude_relay_presets。只要 Moonshot / DeepSeek 至少配
        # 了一个,主链路就能跑;真正一个 backend 都没有才报错。
        #
        # 这里把 _api_config 置成"没有 Claude preset"的直连态:api_key 留空、
        # 各项 preset 行为标志用全局默认。kimi-*/deepseek-* 走
        # _get_client_for_model 里各自的分支,压根不读这些字段;只有把某个
        # stage 手动 override 回 claude-* 时才会撞上空 api_key,那时报的
        # 错也直白("没配 Claude 接入")。
        _fallback_backends = [
            name for name in ("kimi", "deepseek") if _api_config.get(name)
        ]
        if not _fallback_backends:
            raise RuntimeError(
                "找不到任何可用的模型接入配置：MOONSHOT_API_KEY、"
                "DEEPSEEK_API_KEY 都没配，也没有 Vertex（GCP_PROJECT_ID）"
                "或 [claude_relay_presets.*]。请补齐 .streamlit/secrets.toml，"
                "最少需要 MOONSHOT_API_KEY（主链路 kimi-* 全靠它）。"
            )
        _api_config["mode"] = "direct"
        _api_config["active_preset"] = ""
        _api_config["active_label"] = "无 Claude 中转（Kimi/DeepSeek 直连）"
        _api_config["api_key"] = ""
        _api_config["base_url"] = ""
        _api_config["rpm_limit"] = int(CLAUDE_RPM_LIMIT)
        _api_config["max_concurrent"] = int(CLAUDE_MAX_CONCURRENT)
        _api_config["supports_cache"] = bool(ENABLE_PROMPT_CACHING)
        _api_config["supports_adaptive_thinking"] = True
        _api_config["thinking_via_model_suffix"] = False
        _api_config["model_overrides"] = {}
        _relay_limiter.reconfigure(
            _api_config["rpm_limit"], _api_config["max_concurrent"]
        )
        logger.info(
            "[init_api_config] 无 Claude preset，直连模式：可用 backend=%s, "
            "RPM=%d, concurrent=%d, cache=%s",
            "+".join(_fallback_backends),
            _api_config["rpm_limit"],
            _api_config["max_concurrent"],
            _api_config["supports_cache"],
        )
        return

    cfg = presets[active_name]
    _api_config["mode"] = "direct"
    _api_config["active_preset"] = active_name
    _api_config["active_label"] = str(cfg.get("label", active_name))
    _api_config["api_key"] = str(cfg.get("api_key", "") or "")

    base_url = str(cfg.get("base_url", "") or "").rstrip("/")
    if base_url.endswith("/v1"):
        base_url = base_url[:-3]
    _api_config["base_url"] = base_url

    # Per-preset behavioral flags. None means "use the global default".
    _api_config["rpm_limit"] = (
        int(cfg["rpm_limit"])
        if cfg.get("rpm_limit") not in (None, "")
        else int(CLAUDE_RPM_LIMIT)
    )
    _api_config["max_concurrent"] = (
        int(cfg["max_concurrent"])
        if cfg.get("max_concurrent") not in (None, "")
        else int(CLAUDE_MAX_CONCURRENT)
    )
    # supports_cache defaults to the global ENABLE_PROMPT_CACHING flag
    # so the existing config surface still works when a preset omits it.
    # Check if this specific preset had caching auto-disabled by a prior
    # 400 error (per-preset key) — if so, keep it disabled.
    _cache_disabled_key = f"_cache_disabled_{active_name}"
    if _api_config.get(_cache_disabled_key):
        _api_config["supports_cache"] = False
    elif cfg.get("supports_cache") is None:
        _api_config["supports_cache"] = bool(ENABLE_PROMPT_CACHING)
    else:
        _api_config["supports_cache"] = bool(cfg["supports_cache"])
    if cfg.get("supports_adaptive_thinking") is None:
        # Previously-assumed default: adaptive for Opus 4.6+, which is
        # what most modern relays support. Legacy relays that want the
        # old budget_tokens form set this to false explicitly.
        _api_config["supports_adaptive_thinking"] = True
    else:
        _api_config["supports_adaptive_thinking"] = bool(
            cfg["supports_adaptive_thinking"]
        )

    # Some relays (e.g. tdyun.ai) don't understand the `thinking` JSON
    # parameter at all and use a model-name suffix convention instead
    # (claude-opus-4-6-thinking / -high / -medium / -low / -max). When
    # this flag is true, _call_model:
    #   1. Does NOT send the `thinking` JSON parameter
    #   2. Relies on the model name (resolved via model_overrides below)
    #      to carry thinking-tier information
    _api_config["thinking_via_model_suffix"] = bool(
        cfg.get("thinking_via_model_suffix", False)
    )

    # Per-stage model override. Takes precedence over config.py MODELS
    # when the active preset has an entry for that stage. Useful for
    # suffix-convention relays where different stages need different
    # thinking tiers: e.g. tdyun
    #   secretariat = "claude-opus-4-6-high"     (deep reasoning)
    #   crown_prince = "claude-opus-4-6-thinking" (medium)
    #   dispatcher = "claude-opus-4-6"            (no thinking)
    # Stages not listed fall back to the global MODELS default.
    raw_overrides = cfg.get("model_overrides") or {}
    try:
        _api_config["model_overrides"] = {
            str(k): str(v) for k, v in dict(raw_overrides).items()
        }
    except Exception:
        _api_config["model_overrides"] = {}

    # Refresh the shared rate limiter to match this preset. Any in-flight
    # calls continue under the old limits until they finish; new calls
    # made after this point respect the new values.
    _relay_limiter.reconfigure(
        _api_config["rpm_limit"], _api_config["max_concurrent"]
    )

    logger.info(
        f"[init_api_config] Direct/relay mode preset=%r (%s): "
        f"base_url=%s, RPM=%d, concurrent=%d, cache=%s, adaptive_thinking=%s, "
        f"thinking_via_suffix=%s, model_overrides=%d stages",
        active_name,
        _api_config["active_label"],
        "(default)" if not base_url else base_url,
        _api_config["rpm_limit"],
        _api_config["max_concurrent"],
        _api_config["supports_cache"],
        _api_config["supports_adaptive_thinking"],
        _api_config["thinking_via_model_suffix"],
        len(_api_config["model_overrides"]),
    )


# ── Sliding-window rate limiter ───────────────────────────────────────────
#
# Lives in this module because _call_model is the chokepoint and runs in
# threads (via asyncio.to_thread). All primitives are threading-based, not
# asyncio-based, so they work correctly inside the thread-pool executor.
# A single shared limiter instance per process is correct: even with
# multiple parallel pipeline runs they all share the same backend quota.
class _SlidingWindowLimiter:
    """Caps API call STARTS at `max_per_minute` over a rolling 60-second
    window AND in-flight count at `max_concurrent`. Both constraints are
    enforced together — acquire blocks on whichever is binding.

    Usage:
        limiter = _SlidingWindowLimiter(15, 16)
        with limiter.slot(stage_name="ministry_works"):
            # ... actual API call ...

    Concurrency slot is held for the full call duration (until the with
    block exits). RPM slot is consumed at acquire time only — once the
    timestamp is appended, the call counts toward the window even if it
    later fails.
    """

    def __init__(
        self,
        max_per_minute: int,
        max_concurrent: int,
        *,
        adaptive: bool = CLAUDE_RPM_ADAPTIVE,
        ceiling: int = CLAUDE_RPM_CEILING,
    ):
        # max_per_minute is the KNOWN-SAFE FLOOR. When adaptive, the
        # effective rate starts at `ceiling` and backs off toward the
        # floor on observed throttling (see note_rate_limited / recovery).
        self.max_per_minute = int(max_per_minute)  # floor
        self.max_concurrent = int(max_concurrent)
        self.adaptive = bool(adaptive)
        self.ceiling = max(int(ceiling), int(max_per_minute))
        # Effective RPM currently in force. Start optimistic when adaptive.
        self._effective = self.ceiling if self.adaptive else self.max_per_minute
        self._last_throttle = 0.0   # time.time() of last observed 429
        self._last_recovery = 0.0   # time.time() of last upward probe
        self._lock = threading.Lock()
        self._history: collections.deque[float] = collections.deque()
        # threading.Semaphore is process-wide and works across asyncio
        # to_thread-spawned threads.
        self._concurrency_sem = threading.Semaphore(self.max_concurrent)

    def reconfigure(self, max_per_minute: int, max_concurrent: int) -> None:
        """Update the rate caps live — called by init_api_config when
        the active relay preset changes (each preset carries its own
        RPM / concurrency).

        RPM floor is updated under the lock so it lands atomically. The
        adaptive effective rate is clamped back into [floor, ceiling].

        Concurrency semaphore: rather than swapping the semaphore object
        (which leaves in-flight holders releasing into a dead reference),
        we keep the same object and only update max_concurrent. The slot()
        context manager checks the current max_concurrent value each time
        it acquires, so the effective cap adjusts naturally as in-flight
        calls complete.
        """
        with self._lock:
            self.max_per_minute = int(max_per_minute)
            self.max_concurrent = int(max_concurrent)
            self.ceiling = max(self.ceiling, self.max_per_minute)
            self._effective = min(max(self._effective, self.max_per_minute), self.ceiling)

    def note_rate_limited(self, stage_name: str = "") -> None:
        """Safety valve: called when the relay returns a 429 / rate-limit.
        Drops the effective RPM toward the safe floor and starts a cooldown
        before probing back up. Idempotent-ish under a burst of 429s (each
        one nudges it lower, bounded by the floor)."""
        if not self.adaptive:
            return
        with self._lock:
            now = time.time()
            # Coalesce bursts: don't stack multiple backoffs within 2s.
            if now - self._last_throttle < 2.0:
                self._last_throttle = now
                return
            prev = self._effective
            self._effective = max(
                self.max_per_minute,
                int(self._effective * CLAUDE_RPM_BACKOFF_FACTOR),
            )
            self._last_throttle = now
            self._last_recovery = now
        if prev != self._effective:
            logger.warning(
                "[%s] adaptive RPM: 429 observed, backing off %d → %d/min "
                "(floor=%d)",
                stage_name or "rate", prev, self._effective, self.max_per_minute,
            )

    def _maybe_recover(self, now: float) -> None:
        """Under lock: probe the effective RPM back up toward the ceiling
        after a quiet period with no throttling."""
        if not self.adaptive or self._effective >= self.ceiling:
            return
        anchor = max(self._last_throttle, self._last_recovery)
        if now - anchor >= CLAUDE_RPM_RECOVERY_SECONDS:
            self._effective = min(
                self.ceiling, self._effective + CLAUDE_RPM_RECOVERY_STEP
            )
            self._last_recovery = now

    def _wait_for_window(self, stage_name: str) -> None:
        """Block until our timestamp can be appended to the rolling
        window without exceeding the current effective RPM."""
        if self.max_per_minute <= 0 and not self.adaptive:
            return  # disabled
        while True:
            with self._lock:
                now = time.time()
                self._maybe_recover(now)
                limit = self._effective if self.adaptive else self.max_per_minute
                if limit <= 0:
                    return  # disabled
                # Drop entries older than 60s
                while self._history and now - self._history[0] >= 60.0:
                    self._history.popleft()
                if len(self._history) < limit:
                    self._history.append(now)
                    return
                # +0.1s slack so we don't wake up exactly at the boundary
                # and find the entry hasn't expired yet
                wait_for = 60.0 - (now - self._history[0]) + 0.1
            logger.info(
                f"[{stage_name}] rate limiter: window full "
                f"({limit}/min effective), sleeping {wait_for:.1f}s"
            )
            time.sleep(wait_for)

    @contextlib.contextmanager
    def slot(self, stage_name: str = ""):
        """Acquire concurrency + RPM, hold concurrency until exit.

        Order matters: concurrency first (cheap), then RPM (may sleep
        for tens of seconds). With concurrency-first, the RPM wait
        happens with the concurrency slot held — preserving the
        max_concurrent invariant of "no more than N calls in flight".
        """
        # Read max_concurrent once per call; reconfigure() may update
        # it between calls but the semaphore object stays the same.
        cap = self.max_concurrent
        if cap > 0:
            self._concurrency_sem.acquire()
        try:
            self._wait_for_window(stage_name)
            yield
        finally:
            if cap > 0:
                self._concurrency_sem.release()


# Global shared limiter, one per process. Reconfigurable in init_api_config.
_relay_limiter = _SlidingWindowLimiter(CLAUDE_RPM_LIMIT, CLAUDE_MAX_CONCURRENT)


def _get_active_limiter() -> _SlidingWindowLimiter | None:
    """Return the rate limiter to use for this call, or None to skip it.

    Vertex backend has its own server-side quota and returns 429 we'd
    just retry into; app-level limiting there is wasted overhead. Relay
    + direct Anthropic both go through the shared sliding-window limiter.
    """
    if _api_config.get("mode") == "vertex":
        return None
    return _relay_limiter


class ClarificationNeeded(Exception):
    """Raised when an agent needs user input to continue."""

    def __init__(self, stage_name: str, questions: list[str], context: str, partial_output: dict, log_id: str):
        self.stage_name = stage_name
        self.questions = questions
        self.context = context
        self.partial_output = partial_output
        self.log_id = log_id
        super().__init__(f"Agent {stage_name} needs clarification: {questions}")


class RunBudgetExceededError(RuntimeError):
    """Raised when a pipeline run exceeds MAX_TOKENS_PER_RUN. Surfaces as a
    failed run with an explicit cost-safety message so the operator knows
    the failure was a guardrail trip, not a model error."""


# ── Per-run budget tracker ─────────────────────────────────────────────
# Shared across all agents in the process so every _call_model accrues
# into the same pot. Keyed by run_id to stay isolated across concurrent
# runs (if ever). Reset at the start of orchestrator.run() via
# reset_run_budget(run_id).
#
# Thread-safety: multiple agents call _accumulate_run_tokens concurrently
# via asyncio.to_thread. Python's += is NOT atomic (read-modify-write),
# so a lock is required to prevent lost updates.
_run_totals: dict[str, dict[str, int]] = {}
_run_totals_lock = threading.Lock()

# Track completed run_ids for periodic cleanup to prevent memory leak.
_COMPLETED_RUN_IDS: set[str] = set()
_MAX_COMPLETED_RUNS_KEPT = 20


def reset_run_budget(run_id: str) -> None:
    """Zero out token counters for a run. Called by orchestrator.run()
    before any agent executes, so resumed runs don't re-count tokens
    that were already charged in the prior attempt."""
    with _run_totals_lock:
        _run_totals[run_id] = {
            "input": 0,
            "output": 0,
            "cache_read": 0,
            "cache_creation": 0,
            "cost_usd": 0.0,
            "calls": 0,
            "calls_with_cache_activity": 0,
        }
        # Remove this run from the completed set if it's being re-run
        _COMPLETED_RUN_IDS.discard(run_id)


def release_run_budget(run_id: str) -> None:
    """Mark a run as completed and garbage-collect old entries.
    Called by the orchestrator when a run finishes (success or failure)."""
    with _run_totals_lock:
        _COMPLETED_RUN_IDS.add(run_id)
        # Keep at most _MAX_COMPLETED_RUNS_KEPT finished entries
        if len(_COMPLETED_RUN_IDS) > _MAX_COMPLETED_RUNS_KEPT:
            oldest = list(_COMPLETED_RUN_IDS)[
                : len(_COMPLETED_RUN_IDS) - _MAX_COMPLETED_RUNS_KEPT
            ]
            for old_id in oldest:
                _run_totals.pop(old_id, None)
                _COMPLETED_RUN_IDS.discard(old_id)


def _estimate_call_cost_usd(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read: int,
    cache_creation: int,
) -> float:
    """Approximate the dollar cost of one Claude call.

    Anthropic publishes:
      - cache_creation_input_tokens bill at 1.25× input rate (write premium)
      - cache_read_input_tokens bill at 0.10× input rate (read discount)
      - regular input_tokens and output_tokens at their standard rates

    `input_tokens` from response.usage is the NON-cached portion
    (Anthropic splits them out when caching is active). So we add the
    three input categories independently — no double-counting.

    Unknown model → input and output priced at 0; the call just won't
    accrue (operator will see $0.00 and know to add the model to
    COST_PER_1M_*). We deliberately don't estimate from a default so
    wrong cost data isn't silently reported.
    """
    in_rate = COST_PER_1M_INPUT.get(model, 0.0)
    out_rate = COST_PER_1M_OUTPUT.get(model, 0.0)
    if in_rate == 0.0 and out_rate == 0.0:
        return 0.0
    return (
        input_tokens * in_rate
        + output_tokens * out_rate
        + cache_read * in_rate * 0.10
        + cache_creation * in_rate * 1.25
    ) / 1_000_000


def get_run_totals(run_id: str) -> dict[str, int]:
    """Expose the live counters for UI / observability."""
    with _run_totals_lock:
        return dict(_run_totals.get(run_id, {}))


def accumulate_auxiliary_cost(
    run_id: str,
    cost_usd: float,
    input_tokens: int = 0,
    output_tokens: int = 0,
    source: str = "auxiliary",
) -> None:
    """Add cost from an out-of-band backend to the run's running totals —
    i.e. spend that didn't go through a BaseAgent stage. Used by the
    SocialDataX scouts so their per-call billing shows up in
    pipeline_run.total_cost_usd alongside the LLM spend.

    Does NOT count toward MAX_TOKENS_PER_RUN — that ceiling exists to bound
    runaway retry loops in the main chain, and out-of-band sources (priced
    per API call, or an order of magnitude cheaper per token) would dilute
    the guard into irrelevance. Tokens are still tracked per-source for the
    UI breakdown.

    ⚠️ v0.32.0: 辅助层(kimi_client)【不】走这里 —— 它是 advisory,刻意不进
    run 总账,免得二审/结构审的开销把主链路的预算熔断提前触发。它的成本经
    call_kimi_json 返回值里的 cost_usd 单独上报给调用方。
    """
    with _run_totals_lock:
        totals = _run_totals.setdefault(
            run_id,
            {
                "input": 0,
                "output": 0,
                "cache_read": 0,
                "cache_creation": 0,
                "cost_usd": 0.0,
                "calls": 0,
                "calls_with_cache_activity": 0,
            },
        )
        totals["cost_usd"] = float(totals.get("cost_usd", 0.0)) + float(cost_usd)
        aux_tokens_key = f"aux_{source}_tokens"
        totals[aux_tokens_key] = (
            int(totals.get(aux_tokens_key, 0)) + int(input_tokens) + int(output_tokens)
        )


def _accumulate_run_tokens(
    run_id: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read: int = 0,
    cache_creation: int = 0,
) -> tuple[dict, float]:
    """Accumulate one call's usage into the run-level pot. Returns
    (run_totals_snapshot, this_call_cost_usd) so the caller can write the
    per-stage cost to stage_logs without recomputing.

    Thread-safe: acquires _run_totals_lock to prevent lost-update races
    when multiple agents call this concurrently via asyncio.to_thread.
    """
    this_call_cost = _estimate_call_cost_usd(
        model, input_tokens, output_tokens, cache_read, cache_creation
    )
    with _run_totals_lock:
        totals = _run_totals.setdefault(
            run_id,
            {
                "input": 0,
                "output": 0,
                "cache_read": 0,
                "cache_creation": 0,
                "cost_usd": 0.0,
                "calls": 0,
                "calls_with_cache_activity": 0,
            },
        )
        totals["input"] += input_tokens
        totals["output"] += output_tokens
        totals["cache_read"] += cache_read
        totals["cache_creation"] += cache_creation
        totals["calls"] += 1
        if cache_read > 0 or cache_creation > 0:
            totals["calls_with_cache_activity"] += 1
        totals["cost_usd"] = float(totals.get("cost_usd", 0.0)) + this_call_cost
        snapshot = dict(totals)
    return snapshot, this_call_cost


def _check_run_budget(run_id: str) -> None:
    """Raise RunBudgetExceededError if the run has passed MAX_TOKENS_PER_RUN.
    Called right after every API response is accounted for."""
    with _run_totals_lock:
        totals = _run_totals.get(run_id)
        if not totals:
            return
        combined = totals["input"] + totals["output"]
    if combined > MAX_TOKENS_PER_RUN:
        raise RunBudgetExceededError(
            f"流水线 {run_id[:8]} 已累计 {combined:,} tokens "
            f"(input={totals['input']:,}, output={totals['output']:,})，"
            f"超过 MAX_TOKENS_PER_RUN={MAX_TOKENS_PER_RUN:,}。"
            f"为防止重试失控，已强制终止。如需继续请在 pipeline/config.py 调高上限，"
            f"或人工排查为什么某个阶段反复重试。"
        )


def _maybe_warn_no_cache_hits(run_id: str) -> None:
    """After the first few calls, if ENABLE_PROMPT_CACHING is on but the
    backend reports zero cache activity, log once so the operator knows
    the cache_control field is being dropped by their relay. Silent cache
    misses = paying full price for nothing."""
    if not ENABLE_PROMPT_CACHING:
        return
    with _run_totals_lock:
        totals = _run_totals.get(run_id)
        if not totals or totals["calls"] < 4:
            return
    # Already warned for this run?
    if totals.get("_cache_warned"):
        return
    if totals["calls_with_cache_activity"] == 0:
        logger.warning(
            "[cache-probe] run=%s: made %d calls with cache_control but the "
            "backend reports 0 cache activity (no cache_read or "
            "cache_creation tokens). Your relay likely drops the "
            "cache_control field silently. Consider setting "
            "ENABLE_PROMPT_CACHING=False in pipeline/config.py to avoid "
            "sending dead weight.",
            run_id[:8],
            totals["calls"],
        )
        totals["_cache_warned"] = True


class BaseAgent:
    """Base class for all pipeline agents. Subclasses set stage_name and prompt_file."""

    stage_name: str = ""
    prompt_file: str = ""  # relative to pipeline/prompts/

    def __init__(self):
        # 兜底模型:stage 没登记进 config._STAGE_ROLES 时用主力档。
        # v0.32.0 从 claude-sonnet-4-6 改成 kimi-k2.6 —— 换厂后 Claude 的 key
        # 通常压根没配,老兜底会让"忘了登记 stage"这种小错以"没配 Claude
        # 接入"的面目报出来,查起来南辕北辙。
        self.model = MODELS.get(self.stage_name, "kimi-k2.6")
        self.max_tokens = STAGE_MAX_TOKENS.get(self.stage_name, MAX_TOKENS_DEFAULT)
        # Validate prompt file exists at construction time so we fail fast
        # instead of crashing mid-pipeline after burning tokens on prior stages.
        if self.prompt_file:
            _prompt_path = PROMPTS_DIR / self.prompt_file
            if not _prompt_path.exists():
                raise FileNotFoundError(
                    f"Agent {self.stage_name}: prompt file not found at "
                    f"{_prompt_path}. Check pipeline/prompts/ directory."
                )

    def _get_client(self) -> anthropic.Anthropic:
        """Default client for the active relay/Vertex preset.
        Backwards-compat shim — new code should call _get_client_for_model
        which routes by model-name prefix (deepseek/gpt/claude → 不同
        backend)。"""
        return self._get_client_for_model(self.model)

    # 路由逻辑住在模块级(见上面 get_client_for_model / backend_configured),
    # 这里只是 BaseAgent 上的转发口,保持既有调用点不用改。
    backend_configured = staticmethod(backend_configured)

    def _get_client_for_model(self, model: str) -> anthropic.Anthropic:
        return get_client_for_model(model)

    @classmethod
    def _model_fallback_candidates(cls, primary_model: str) -> list[str]:
        """Build [primary, ...fallbacks] for the no-channel 保底 path.

        v0.32.0 改动两处:

        1. 链条跨厂家。以前只有 claude-* 有备选池(同厂异档),gpt/deepseek
           各自返回 [primary] 就完事。现在 MODEL_FALLBACK_CHAIN 里 Kimi 两档
           + DeepSeek 一档混排,任何模型失联都能降级到另一家继续跑完,而不是
           整条 run 挂在某一家的短暂故障上。

        2. 跳过没配 key 的候选。跨厂家降级的代价是"下一个候选可能压根没配"。
           如果放任它进循环,_get_client_for_model 会抛 RuntimeError("没配
           XXX_API_KEY") —— 那不是 no-channel 错误,循环会直接上抛,于是
           用户看到的报错是"没配 DeepSeek",而真实原因是"Kimi 无渠道"。
           所以在这里就把未配置的候选滤掉。

        primary 永远排第一且永远保留(哪怕它自己没配)—— 让真实的配置错误
        在第一次尝试时就以本来面目抛出来。
        """
        out = [primary_model]
        for m in MODEL_FALLBACK_CHAIN:
            if m in out:
                continue
            if not cls.backend_configured(m):
                continue
            out.append(m)
        return out

    # v0.31 及之前的名字,保留给任何外部调用方。
    _claude_fallback_candidates = _model_fallback_candidates

    # Agents that need strategy-level methodology (差异化/真实感/平台感知)
    _STRATEGY_STAGES = frozenset({
        "secretariat", "chancellery", "chancellery_final",
        "ministry_works", "ministry_works_cell_planner",
    })
    # Agents that need execution-level methodology (网感/钩子/范式) —
    # NOTE: works_builder and vibe agents already have full execution
    # knowledge hardcoded in their own prompt files. Appending foundation's
    # §2 网感 chapter would duplicate ~9.6KB of content they already have.
    # So execution agents get NO extra foundation beyond common.
    # Strategy agents get the lighter strategy foundation.
    # All agents get the common foundation (请旨协议).

    def load_system_prompt(self) -> str:
        """Load agent-specific prompt + append relevant foundation knowledge.

        Before v0.7.5, the FULL foundation.md (16KB, including 9.6KB of 网感
        rules) was appended to every single agent — 14 agents × 16KB = 224KB
        of redundant input tokens per pipeline run. Most agents don't need
        execution-level content rules (hooks, paradigms, real-person samples).

        Now segmented:
        - foundation_common.md (请旨协议): ALL agents
        - foundation_strategy.md (差异化/真实感/平台感知): strategy agents only
        - §2 网感 (9.6KB): NOT appended — already hardcoded in works_builder.md
          and vibe_critic.md
        """
        path = PROMPTS_DIR / self.prompt_file
        prompt = path.read_text(encoding="utf-8")

        # Always append common foundation (请旨协议)
        common_path = PROMPTS_DIR / "foundation_common.md"
        if common_path.exists():
            prompt += "\n\n---\n\n" + common_path.read_text(encoding="utf-8")

        # Strategy agents also get strategy methodology
        if self.stage_name in self._STRATEGY_STAGES:
            strategy_path = PROMPTS_DIR / "foundation_strategy.md"
            if strategy_path.exists():
                prompt += "\n\n---\n\n" + strategy_path.read_text(encoding="utf-8")

        return prompt

    @property
    def _use_thinking(self) -> bool:
        # Thinking is enabled for specific strategic stages, not tied to model name.
        # On Vertex: uses adaptive thinking (model decides depth).
        # On relay: uses budget_tokens=10000 as fallback.
        #
        # ⚠️ v0.30.13: 故意【不】为 strategy_debate_* 放行 thinking。
        # 背景:strategy debate 里 secretariat 以 "strategy_debate_N" 身份跑
        # (orchestrator._strategy_loop 临时改 stage_name)。v0.30.12 一度识别
        # 这个前缀给它开 thinking(原意:debate 也该深推理),但 secretariat
        # 每轮输出的是【完整大 plan】(5-7 个 tactical_directions + matrix_skeleton
        # 含十几个 active_cells),它的 max_tokens=32K 需要几乎全部留给输出。
        # adaptive thinking 会先吃掉一大块预算,导致 plan JSON 被截断 ——
        # truncation repair 只救回第 1 个 direction,target_platforms /
        # matrix_skeleton 直接丢失,下游 cell 重建得 0 cell 整条流水线崩。
        # 结论:debate 的深推理收益 < plan 输出完整性,这里回到精确匹配,
        # secretariat 在 debate 不开 thinking(和 v0.30.11 及之前一致)。
        return self.stage_name in THINKING_STAGES

    @staticmethod
    def _build_content_blocks(text: str) -> list[dict[str, Any]] | str:
        """Convert text with [BASE64_IMAGE:...] markers into multimodal content blocks."""
        if "[BASE64_IMAGE:" not in text:
            return text  # plain text, no conversion needed

        blocks: list[dict[str, Any]] = []
        parts = re.split(r"\[BASE64_IMAGE:(.*?)\]", text)
        # parts = [text_before, base64_data, text_after, base64_data, ...]
        for i, part in enumerate(parts):
            if i % 2 == 0:
                # Text segment
                stripped = part.strip()
                if stripped:
                    blocks.append({"type": "text", "text": stripped})
            else:
                # Base64 image data — detect media type from header
                b64 = part.strip()
                if b64.startswith("/9j/"):
                    media_type = "image/jpeg"
                elif b64.startswith("iVBOR"):
                    media_type = "image/png"
                elif b64.startswith("R0lGOD"):
                    media_type = "image/gif"
                elif b64.startswith("UklGR"):
                    media_type = "image/webp"
                else:
                    media_type = "image/png"  # default
                blocks.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": b64,
                    },
                })

        return blocks if blocks else text

    def _call_openai_chat(
        self,
        system_prompt: str,
        user_message: str,
        effective_model: str,
        max_tokens: int,
    ) -> tuple[str, int, int, int, int, bool, str]:
        """OpenAI-compat chat/completions backend(v0.30.7 起走 vectorengine.ai)。

        GPT 系列模型不能通过 anthropic SDK 调用——tdyun-style anthropic-compat
        中转转发不过去。所以单独走 OpenAI SDK + vectorengine 端点。

        返回和 _call_model 同样的 7-tuple,让上层调度逻辑不用分支处理:
        (text, input_tokens, output_tokens, cache_read, cache_creation,
        thinking_fired, actual_model)。GPT 没有 anthropic 风格的 thinking 块,
        thinking_fired 永远是 False;GPT 不走 Claude 的 no-channel fallback,
        actual_model 恒等于传入的 effective_model。OpenAI 的 prompt caching
        走 usage.prompt_tokens_details.cached_tokens,映射到 cache_read。

        失败模式:
        - vectorengine 没配 → RuntimeError 提示去 secrets.toml 加 key
        - openai SDK 未安装 → ImportError 一样,但 requirements.txt 已经
          固定了 openai>=1.40 所以正常环境不会触发
        """
        ve_cfg = _api_config.get("vectorengine") if _api_config else None
        if not ve_cfg or not ve_cfg.get("api_key"):
            raise RuntimeError(
                f"模型 {effective_model!r} 需要 OpenAI-compat backend "
                "(vectorengine.ai),但 secrets.toml 里没配 "
                "VECTORENGINE_API_KEY。去 .streamlit/secrets.toml 加上 "
                "`VECTORENGINE_API_KEY = \"sk-...\"` 后重启,或者把这个 "
                "stage 的模型改回 Claude 系列。"
            )

        import openai as _openai_sdk  # 延迟 import,Streamlit 未配置时不触发

        client = _openai_sdk.OpenAI(
            api_key=ve_cfg["api_key"],
            base_url=ve_cfg["base_url"],
            timeout=900.0,
        )

        # 把 anthropic 风格的 system + messages 转成 OpenAI ChatCompletion
        # messages 格式:第一条是 role=system,后面是 user/assistant。
        oai_messages: list[dict] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]

        # 大多数 OpenAI-compat 中转支持 max_tokens(传统)和 max_completion_tokens
        # (新版 reasoning 模型用)。先传 max_tokens,如 vectorengine 在
        # gpt-5/gpt-5.5 时报 400 再切换 max_completion_tokens。
        oai_kwargs: dict[str, Any] = {
            "model": effective_model,
            "messages": oai_messages,
            "max_tokens": max_tokens,
            "stream": False,
        }

        try:
            response = client.chat.completions.create(**oai_kwargs)
        except _openai_sdk.BadRequestError as e:
            # gpt-5 / o1 / o3 / o4 系列不接受 max_tokens,要 max_completion_tokens
            _err_msg = str(e).lower()
            if "max_tokens" in _err_msg or "max_completion_tokens" in _err_msg:
                oai_kwargs.pop("max_tokens", None)
                oai_kwargs["max_completion_tokens"] = max_tokens
                response = client.chat.completions.create(**oai_kwargs)
            else:
                raise

        # 解析返回
        text = ""
        if response.choices:
            _msg = response.choices[0].message
            text = (_msg.content or "").strip()

        # Token 统计 + cache 信息
        usage = response.usage
        input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
        cache_read = 0
        try:
            _ptd = getattr(usage, "prompt_tokens_details", None)
            if _ptd is not None:
                cache_read = int(getattr(_ptd, "cached_tokens", 0) or 0)
        except Exception:
            pass

        return (text, input_tokens, output_tokens, cache_read, 0, False, effective_model)

    def _call_model(
        self, system_prompt: str, user_message: str
    ) -> tuple[str, int, int, int, int, bool, str]:
        """Synchronous model call (Anthropic Messages API 形状)。

        v0.32.0 从 `_call_claude` 改名为 `_call_model` —— 换厂之后这条路上
        跑的是 Kimi 和 DeepSeek,名字里的 Claude 已经会误导人。它一直都不是
        "只打 Claude" 的方法(v0.30.0 起就在路由多家),只是名字没跟上。
        类底部保留了 `_call_claude` 别名,以防外部有引用。

        Returns (response_text, input_tokens, output_tokens,
        cache_read_input_tokens, cache_creation_input_tokens,
        thinking_fired, actual_model). thinking_fired is True iff the
        model's response actually contained a `thinking` content block.
        Compare against self._use_thinking: if we asked for thinking but
        didn't get it, the relay silently dropped the `thinking` JSON
        param — visible as "thinking ✗" in the UI's model_used field.

        actual_model is the model that actually produced the response —
        normally the stage's configured model, but if the relay returned
        "No available channel" it's whichever fallback model succeeded
        (see MODEL_FALLBACK_CHAIN). run() surfaces it in model_used so
        the UI reflects the真实 model, not the planned one.

        Three execution paths share the SAME kwargs construction (model,
        max_tokens, system, messages, thinking) — the historical "relay
        doesn't support `thinking`" workaround is no longer needed.
        Modern relays accept the standard Anthropic JSON `thinking`
        parameter, and so do native Anthropic + Vertex.

        The only path-specific bit is the Vertex Opus-4.6 adaptive-mode
        nicety: we ask for `{"type": "adaptive"}` so Vertex picks the
        thinking budget itself; everywhere else we pass an explicit
        `{"type": "enabled", "budget_tokens": N}` which is the universal
        format.
        """
        is_vertex = _api_config.get("mode") == "vertex"

        # Build content (may include image blocks)
        user_content = self._build_content_blocks(user_message)
        messages = [{"role": "user", "content": user_content}]

        # Structured system prompt with ephemeral cache control. Cache
        # support is a per-preset flag: new_relay might support it,
        # old_relay might silently drop the cache_control field and
        # just pay full price. The active preset's supports_cache flag
        # (set in init_api_config) overrides the global
        # ENABLE_PROMPT_CACHING constant, so switching relays via the
        # settings page immediately adjusts this behavior.
        _preset_supports_cache = _api_config.get(
            "supports_cache", ENABLE_PROMPT_CACHING
        )
        if _preset_supports_cache:
            system_block: Any = [
                {
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
        else:
            system_block = system_prompt

        # Resolve effective model. Per-preset model_overrides take
        # precedence over the global config.py MODELS — required for
        # relays like tdyun.ai that signal thinking tier via model name
        # suffix (claude-opus-4-6-thinking / -high / -medium / etc).
        # Stage names not in the override map fall back to self.model
        # (which was set from MODELS at agent construction time).
        _model_overrides = _api_config.get("model_overrides") or {}
        effective_model = _model_overrides.get(self.stage_name) or self.model

        # GPT 系列必须用 vectorengine 的 OpenAI-compat chat/completions 接口,
        # 不能用 anthropic SDK。**提前** dispatch,在 _get_client_for_model
        # 之前 — 否则 Vertex 模式下 _get_client_for_model 看到 gpt-* 会直接
        # raise("Vertex 模式不支持 GPT"),GPT stage 在 Vertex 部署下永远跑
        # 不起来。返回相同的 (text, in_tok, out_tok, cache_read,
        # cache_creation, thinking_fired) 元组,上层 run() 不用分支处理。
        _model_low = (effective_model or "").lower()
        _is_gpt_family = _model_low.startswith("gpt-")
        if _is_gpt_family:
            try:
                return self._call_openai_chat(
                    system_prompt=system_prompt,
                    user_message=user_message,
                    effective_model=effective_model,
                    max_tokens=self.max_tokens,
                )
            except Exception as _gpt_err:
                # Cross-vendor fallback: vectorengine (a small single relay)
                # carries two REQUIRED stages (chancellery / ministry_war)
                # with NO backend of its own to fall back to. On a
                # routing/transient failure, re-run this call on a Claude
                # model instead of burning retries on a dead GPT channel and
                # failing the whole run. Auth/400 errors are NOT masked —
                # they re-raise and fail fast with a real message.
                if _is_rate_limit_error(_gpt_err):
                    note_rate_limit_hit(self.stage_name)
                _routing_or_transient = (
                    _is_no_channel_error(_gpt_err)
                    or _is_rate_limit_error(_gpt_err)
                    or _classify_llm_error(_gpt_err, 0)[1]  # is_5xx
                )
                # v0.32.0: 兜底目标从"Claude 链首"改成 MODEL_FALLBACK_CHAIN
                # 里第一个【真的配了 key】的模型。换厂后 Claude 通常没配,
                # 照老逻辑兜到 claude-* 只会换一个错法失败。
                _cross = next(
                    (m for m in MODEL_FALLBACK_CHAIN
                     if self.backend_configured(m)),
                    None,
                )
                if _routing_or_transient and _cross:
                    logger.warning(
                        "[%s] GPT backend failed (%s: %s); cross-vendor "
                        "fallback to %s",
                        self.stage_name, type(_gpt_err).__name__,
                        mask_secrets(str(_gpt_err))[:200], _cross,
                    )
                    effective_model = _cross
                    _model_low = effective_model.lower()
                    _is_gpt_family = False
                    # fall through to the anthropic-SDK path below;
                    # thinking/backend selection recompute off
                    # effective_model.
                else:
                    raise

        # v0.30.0: 按模型族选 backend(Claude 走 default relay 或 Vertex,
        # DeepSeek 走独立官方 anthropic-compat 端点)。client 必须按
        # effective_model 来挑,不能用全局 _get_client。
        client = self._get_client_for_model(effective_model)

        kwargs: dict[str, Any] = {
            "model": effective_model,
            "max_tokens": self.max_tokens,
            "system": system_block,
            "messages": messages,
        }

        # Thinking control via standard Anthropic JSON `thinking` field.
        # Works on Anthropic native, modern relays, and Vertex.
        #
        # Opus 4.6+ → adaptive (server picks budget). Per Anthropic's own
        # deprecation notice: `thinking.type=enabled` with a fixed
        # budget_tokens is deprecated for 4.6-class models and gives
        # measurably worse results than adaptive in their testing. See
        # https://platform.claude.com/docs/en/build-with-claude/adaptive-thinking
        #
        # Older models (4.1, 4.5) don't support adaptive yet, so keep
        # the explicit budget_tokens form as fallback.
        # Three thinking-control conventions across backends:
        #
        #   1. `thinking_via_model_suffix=True` (e.g. tdyun.ai):
        #      Don't send the JSON `thinking` parameter AT ALL. The
        #      relay infers thinking tier from the model name suffix
        #      (claude-opus-4-6-thinking / -high / -medium / etc),
        #      which is set via model_overrides per stage.
        #
        #   2. `supports_adaptive_thinking=True` (mux, native Anthropic,
        #      Vertex Opus 4.6): pass {"type":"adaptive"} so the model
        #      picks its own budget — Anthropic-recommended for 4.6+.
        #
        #   3. Fallback (older relays / older models): pass
        #      {"type":"enabled","budget_tokens":N} explicit form.
        #
        # v0.32.0 在这三条约定之上加了一层【按厂家】的前置判断:上面三条讲
        # 的是"这个 backend 用什么形态表达 thinking",而厂家决定的是"它到底
        # 认不认这个字段"。不认的直接不发(见 _THINKING_CAPABLE_VENDORS)。
        _preset_uses_suffix = _api_config.get("thinking_via_model_suffix", False)
        _preset_supports_adaptive = _api_config.get(
            "supports_adaptive_thinking", True
        )
        _vendor = _model_vendor(effective_model)
        # 哪些模型接受 {"type":"adaptive"}(让服务端自己定预算),哪些只能收
        # 老的 {"type":"enabled","budget_tokens":N}:
        #   - Kimi K2.6 / K3:Moonshot 的 /anthropic 端点把 enabled 各档都
        #     映射到 adaptive reasoning,直接发 adaptive 最省事也最贴合。
        #   - Claude Opus 4.6+ / Sonnet 4.6:Anthropic 自 4.6 起推荐 adaptive,
        #     固定 budget_tokens 已被标记 deprecated 且效果更差。
        # 前缀精确匹配,避免将来的 "claude-opus-5-0" 之类误命中。
        _is_adaptive_thinking_family = (
            _vendor == "kimi"
            or "claude-opus-4-6" in effective_model
            or "claude-opus-4-7" in effective_model
            or "claude-opus-4-8" in effective_model
            or "claude-sonnet-4-6" in effective_model
        )
        # 不认 thinking 字段的厂家:即使 stage 在 THINKING_STAGES 里也不发。
        #   - GPT:relay 转给 OpenAI 接口会 400。
        #   - DeepSeek:anthropic-compat 端点静默忽略 thinking,发了不报错但
        #     也不生效 —— 与其在每个请求里塞一个死字段、让日志显示"开了
        #     thinking"造成假象,不如不发,thinking_fired 如实记 ✗。
        # 上层保留 _use_thinking 用于日志标签("我们本来想 think,但这家不接")。
        if self._use_thinking and _vendor not in _THINKING_CAPABLE_VENDORS:
            pass  # 跳过 thinking 注入,后面 thinking_fired 会自然记录 ✗
        elif self._use_thinking and not _preset_uses_suffix:
            if _is_adaptive_thinking_family and _preset_supports_adaptive:
                kwargs["thinking"] = {"type": "adaptive"}
            else:
                kwargs["thinking"] = {
                    "type": "enabled",
                    "budget_tokens": THINKING_BUDGET_TOKENS,
                }
        elif self._use_thinking and _preset_uses_suffix:
            # Suffix-mode sanity check: the relay is supposed to infer
            # thinking tier from the model NAME, so the effective model for
            # a thinking stage must carry one of the known suffixes. If the
            # operator forgot to set a model_override for this stage, we'd
            # silently run it WITHOUT thinking on a non-thinking base model.
            # Log once per stage so the mis-config surfaces instead of
            # mysteriously-worse outputs.
            if not any(
                s in effective_model
                for s in ("-thinking", "-high", "-medium", "-low")
            ):
                logger.warning(
                    "[%s] thinking_via_model_suffix=True but effective model "
                    "%r has no known thinking suffix — this stage will likely "
                    "run without thinking. Check model_overrides in secrets.",
                    self.stage_name,
                    effective_model,
                )

        # Apply rate limiting. Vertex returns None (server-side quota);
        # relay/direct gets the sliding-window limiter so we respect both
        # CLAUDE_RPM_LIMIT (sustained) and CLAUDE_MAX_CONCURRENT (peak).
        # The limiter is held for the full call duration to enforce the
        # concurrency cap correctly.
        #
        # Cache-control safety net: some relays silently ignore the
        # cache_control field, others 400 on it. We default to sending
        # it ("万一命中了呢"), but if the backend rejects it specifically
        # with a 400 containing "cache_control", auto-disable caching
        # for this process and retry ONCE with plain-string system.
        # This means a new relay-switch doesn't require a config commit
        # just to find out whether caching works.
        limiter = _get_active_limiter()

        def _do_stream(call_kwargs: dict[str, Any]):
            if limiter is not None:
                with limiter.slot(self.stage_name):
                    with client.messages.stream(**call_kwargs) as stream:
                        return stream.get_final_message()
            with client.messages.stream(**call_kwargs) as stream:
                return stream.get_final_message()

        def _stream_with_cache_fallback():
            """Single-model call. Handles the cache_control 400 →
            disable-cache-and-retry path internally. Any other error
            (including the relay "no available channel" rejection) is
            raised so the outer model-fallback loop can decide whether to
            try the next model in the fallback chain."""
            try:
                return _do_stream(kwargs)
            except anthropic.BadRequestError as e:
                err_text = str(e)
                # Only treat as cache fault if we actually sent cache_control
                # AND the error message specifically mentions it. Avoid false
                # positives from unrelated 400s that happen to contain the word.
                is_cache_fault = (
                    _preset_supports_cache
                    and "cache_control" in err_text.lower()
                )
                if not is_cache_fault:
                    raise
                _active_preset = get_active_claude_relay_name() or "_global"
                _cache_key = f"_cache_disabled_{_active_preset}"
                # Check-and-set under a lock so concurrent agents all see the
                # same decision. First thread that gets the lock flips the
                # bit; later threads see it set and skip the log spam but
                # still fall through to the cache-less retry with this
                # thread's own kwargs.
                with _api_config_lock:
                    _first_to_disable = not _api_config.get(_cache_key)
                    _api_config[_cache_key] = True
                    _api_config["supports_cache"] = False
                if _first_to_disable:
                    logger.warning(
                        "[cache-fallback] backend rejected cache_control with 400, "
                        "auto-disabling caching for preset '%s' and retrying. "
                        "Error: %s",
                        _active_preset,
                        err_text[:300],
                    )
                kwargs["system"] = system_prompt  # plain string, no cache
                return _do_stream(kwargs)

        # ── Model fallback 保底 ────────────────────────────────────────
        # 上游对某个模型返回"无可用渠道"(No available channel for model
        # xxx)时,按 MODEL_FALLBACK_CHAIN 依次降级重试,避免整个 stage 失败。
        # v0.32.0 起这条链跨厂家(Kimi 两档 + DeepSeek),所以候选里未配 key
        # 的会被 _model_fallback_candidates 提前滤掉。每次换模型都重绑
        # client(闭包变量)+ kwargs["model"] —— 跨厂家降级时 base_url 和
        # api_key 都变了,不重绑会拿旧 client 打新厂家的模型名。其它错误
        # (rate limit / 5xx / auth)照常上抛,交给 run() 的重试 / 分类逻辑。
        _candidates = self._model_fallback_candidates(effective_model)
        response = None
        for _ci, _try_model in enumerate(_candidates):
            client = self._get_client_for_model(_try_model)
            kwargs["model"] = _try_model
            try:
                response = _stream_with_cache_fallback()
            except Exception as e:
                if _is_no_channel_error(e) and _ci < len(_candidates) - 1:
                    logger.warning(
                        "[model-fallback] 模型 '%s' 无可用渠道,降级尝试备选 "
                        "'%s';原因: %s",
                        _try_model,
                        _candidates[_ci + 1],
                        str(e)[:160],
                    )
                    continue
                raise
            # 成功 —— effective_model 更新为实际跑通的模型,供日志 / UI label。
            effective_model = _try_model
            if _ci > 0:
                logger.warning(
                    "[model-fallback] 已降级到 '%s' 成功(原计划 '%s' 无可用渠道)",
                    _try_model,
                    _candidates[0],
                )
            break

        # Extract text from response — thinking responses have multiple content blocks
        text = ""
        has_thinking = False
        for block in response.content:
            if block.type == "thinking":
                has_thinking = True
            if block.type == "text":
                text = block.text
                break

        if self._use_thinking:
            logger.info(
                f"[{self.stage_name}] thinking={has_thinking}, "
                f"blocks={[b.type for b in response.content]}, "
                f"mode={'vertex' if is_vertex else 'relay'}"
            )

        input_tokens = response.usage.input_tokens
        output_tokens = response.usage.output_tokens
        # Cache usage fields are present when the backend supports prompt
        # caching. Missing / None = silently unsupported (typical on many
        # relay proxies). getattr falls back to 0 without crashing on older
        # SDK versions that lack these attributes.
        cache_read = int(getattr(response.usage, "cache_read_input_tokens", 0) or 0)
        cache_creation = int(
            getattr(response.usage, "cache_creation_input_tokens", 0) or 0
        )
        return (
            text,
            input_tokens,
            output_tokens,
            cache_read,
            cache_creation,
            has_thinking,
            effective_model,   # v0.30.12: 实际跑通的模型(可能因 no-channel fallback ≠ 计划)
        )

    @staticmethod
    def _lenient_json_loads(text: str) -> dict[str, Any]:
        """Parse JSON tolerantly: allow control chars in strings, normalize smart quotes.

        LLMs frequently emit multi-line content inside JSON string values without
        escaping the newlines/tabs. Python's strict=False decoder accepts these.
        Also normalizes smart/chinese quotes to ASCII equivalents before parsing.
        """
        # Normalize common non-ASCII quote variants that LLMs sometimes produce
        # around JSON keys or values. We only touch quotes; Chinese punctuation
        # inside string VALUES is fine and gets preserved naturally.
        normalized = (
            text
            .replace("\u201c", '"')  # left double quote
            .replace("\u201d", '"')  # right double quote
            .replace("\u2018", "'")  # left single quote
            .replace("\u2019", "'")  # right single quote
        )
        return json.JSONDecoder(strict=False).decode(normalized)

    @staticmethod
    def _extract_json(response_text: str) -> dict[str, Any]:
        """Extract JSON from Claude response.

        Handles: markdown code blocks, outermost braces, unescaped control chars
        inside strings (common LLM mistake with multi-line system_prompt values),
        smart quotes, and truncation.
        """
        loads = BaseAgent._lenient_json_loads

        # Try direct parse
        try:
            return loads(response_text)
        except (json.JSONDecodeError, ValueError):
            pass

        # Try ```json ... ``` block
        match = re.search(r"```json\s*\n?(.*?)\n?\s*```", response_text, re.DOTALL)
        if match:
            try:
                return loads(match.group(1))
            except (json.JSONDecodeError, ValueError):
                pass

        # Try any ``` ... ``` block
        match = re.search(r"```\s*\n?(.*?)\n?\s*```", response_text, re.DOTALL)
        if match:
            try:
                return loads(match.group(1))
            except (json.JSONDecodeError, ValueError):
                pass

        # Try outermost braces
        match = re.search(r"\{.*\}", response_text, re.DOTALL)
        if match:
            try:
                return loads(match.group(0))
            except (json.JSONDecodeError, ValueError):
                pass

        # Try to repair truncated JSON (response cut off by max_tokens)
        json_start = response_text.find("{")
        if json_start >= 0:
            fragment = response_text[json_start:]
            repaired = BaseAgent._try_repair_truncated_json(fragment)
            if repaired is not None:
                logger.warning(
                    f"JSON was truncated (len={len(response_text)}), repaired by closing brackets"
                )
                if isinstance(repaired, dict):
                    # 让下游知道"这是被 max_tokens 截断后修补的残缺产出",而非静默
                    # 当完整结果吞下。无下游质量闸的 stage(尤其五部 personnel/
                    # revenue/rites/war/justice)据此可告警,不再把砍短的 persona
                    # 池当完整交付。
                    repaired["_json_truncated"] = True
                return repaired

        # v0.29.12: 老错误只报 last 200 chars,debug 时还得翻 stage_log
        # 才能看全貌。把首 200 + 末 200 都塞进去,并报告中间遗漏了多少,
        # 让运行时日志就能判断是"整段不是 JSON"还是"尾部截断"。
        _preview_front = response_text[:200]
        _preview_tail = response_text[-200:] if len(response_text) > 200 else ""
        _gap = max(0, len(response_text) - 400)
        raise ValueError(
            f"Could not extract JSON from response (len={len(response_text)}); "
            f"first 200 chars: {_preview_front!r}; "
            f"...[{_gap} chars omitted]... "
            f"last 200 chars: {_preview_tail!r}"
        )

    @staticmethod
    def _try_repair_truncated_json(fragment: str) -> dict[str, Any] | None:
        """Repair truncated JSON by finding the last valid cut point and closing
        open containers in proper nesting order.

        Handles truncation mid-string, mid-value, or between fields. Uses a
        real stack (not counts) so nested structures like {[{[...]}]} close
        correctly as ]}]} instead of the buggy ]]}}.
        """
        in_string = False
        escape_next = False
        # Positions right after a complete value where we could safely cut
        cut_points: list[int] = []

        for i, ch in enumerate(fragment):
            if escape_next:
                escape_next = False
                continue
            if ch == "\\" and in_string:
                escape_next = True
                continue
            if ch == '"':
                in_string = not in_string
                if not in_string:
                    cut_points.append(i + 1)
                continue
            if in_string:
                continue
            if ch in "{[":
                pass  # container open — don't add cut here
            elif ch in "}]":
                cut_points.append(i + 1)
            elif ch == ",":
                cut_points.append(i)  # cut BEFORE comma is also valid

        # Try cut points from most recent to earliest (keep as much data as possible).
        # v0.29.12: 老版本 `cut_points[-300:]` 硬限在 13K+ 响应里常常不够
        # ——最后 300 个 cut point 可能都深埋在一个没闭合的嵌套结构里,
        # 每个 candidate 都不合法。放开限制,扫所有 cut point。即使 5000
        # 个 candidate 每个 re-scan 也就 ~50ms,总共 <1s,比整条流水线
        # 报错重跑强得多。
        for cp in reversed(cut_points):
            candidate = fragment[:cp].rstrip().rstrip(",").rstrip()
            # Strip a dangling partial field. Order matters — more specific
            # patterns first. All regexes assume the candidate has no unclosed
            # strings (cut_points are only recorded outside strings). After
            # these substitutions the candidate should end at a clean structure
            # boundary (`}`, `]`, a closed string value, a number, or
            # `true`/`false`/`null`).
            candidate = re.sub(r',\s*"[^"]*"\s*:\s*$', "", candidate)  # , "k":
            candidate = re.sub(r'\{\s*"[^"]*"\s*:\s*$', "{", candidate)  # { "k":
            # NEW: lone trailing keys with no colon yet (truncation hit between
            # the key and its colon). Matches `, "k"` and `{ "k"` respectively.
            candidate = re.sub(r',\s*"[^"]*"\s*$', "", candidate)  # , "k"
            candidate = re.sub(r'\{\s*"[^"]*"\s*$', "{", candidate)  # { "k"
            # NEW: trailing array item that is just a lone string with no
            # preceding comma — e.g. `[ "a", "b"` is fine (commas delimit)
            # but `["a"` after a cut is fine too (stack handles it). We only
            # need to handle `[ "a", ` which rstrip already covers.
            candidate = candidate.rstrip().rstrip(",")

            # Re-scan candidate with a real stack
            stack: list[str] = []
            ins = False
            esc = False
            for ch in candidate:
                if esc:
                    esc = False
                    continue
                if ch == "\\" and ins:
                    esc = True
                    continue
                if ch == '"':
                    ins = not ins
                    continue
                if ins:
                    continue
                if ch == "{":
                    stack.append("{")
                elif ch == "}":
                    if stack and stack[-1] == "{":
                        stack.pop()
                    else:
                        # Unmatched close — this candidate is malformed
                        stack = ["__BAD__"]
                        break
                elif ch == "[":
                    stack.append("[")
                elif ch == "]":
                    if stack and stack[-1] == "[":
                        stack.pop()
                    else:
                        stack = ["__BAD__"]
                        break

            if stack == ["__BAD__"] or ins:
                continue  # malformed or ended inside string
            if not stack:
                # Already balanced — try parsing as-is
                try:
                    result = BaseAgent._lenient_json_loads(candidate)
                    logger.info(
                        f"Truncation repair: cut at pos {cp}/{len(fragment)}, "
                        f"already balanced"
                    )
                    return result
                except (json.JSONDecodeError, ValueError):
                    continue

            # Build proper close suffix: reverse stack, flip { → } and [ → ]
            suffix = "".join("}" if c == "{" else "]" for c in reversed(stack))
            try:
                result = BaseAgent._lenient_json_loads(candidate + suffix)
                logger.info(
                    f"Truncation repair: cut at pos {cp}/{len(fragment)}, "
                    f"closed stack {stack} → suffix {suffix!r}"
                )
                return result
            except (json.JSONDecodeError, ValueError):
                continue

        return None

    async def run(self, input_data: dict, run_id: str, db: SupabaseClient) -> dict:
        """Execute the agent: log → call Claude → parse → check clarification → update log."""
        log = db.create_stage_log(run_id, self.stage_name, input_data)
        log_id = log["id"]
        start_time = time.time()
        total_input_tokens = 0
        total_output_tokens = 0

        last_error = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                system_prompt = self.load_system_prompt()
                user_message = json.dumps(input_data, ensure_ascii=False, indent=2)

                (
                    text,
                    input_tokens,
                    output_tokens,
                    cache_read,
                    cache_creation,
                    thinking_fired,
                    actual_model,
                ) = await asyncio.to_thread(
                    self._call_model, system_prompt, user_message
                )
                total_input_tokens += input_tokens
                total_output_tokens += output_tokens

                # Build a human-readable model label that reveals thinking
                # actually fired (or didn't) so the UI doesn't need to dig
                # into server logs to answer "did this stage reason deeply?".
                #  - `[thinking ✓]` — we asked for thinking AND the model
                #    returned a thinking block
                #  - `[thinking ✗]` — we asked for thinking but got none;
                #    relay likely dropped the `thinking` JSON param
                #  - no tag          — this stage doesn't use thinking
                if self._use_thinking:
                    _thinking_tag = " [thinking ✓]" if thinking_fired else " [thinking ✗]"
                else:
                    _thinking_tag = ""
                # Use the EFFECTIVE model name (which may be a per-preset
                # override like "claude-opus-4-6-high") so the UI shows
                # what the relay actually saw, not the global default.
                _model_overrides_for_label = _api_config.get("model_overrides") or {}
                _planned_model = (
                    _model_overrides_for_label.get(self.stage_name) or self.model
                )
                # v0.30.12: actual_model comes back from _call_model. If the
                # relay had no channel for the planned model and we fell back
                # to another Claude model, actual_model ≠ planned — surface
                # that with a [fallback←planned] tag so model_used in the UI /
                # stage_log reflects what really ran, not a misleading plan.
                _fallback_tag = ""
                if actual_model and actual_model != _planned_model:
                    _fallback_tag = f" [fallback←{_planned_model}]"
                model_label = f"{actual_model or _planned_model}{_thinking_tag}{_fallback_tag}"

                # Feed the per-run budget tracker. Raises RunBudgetExceededError
                # if we've blown through MAX_TOKENS_PER_RUN — caught at the
                # orchestrator level which marks the run as failed. Runs
                # BEFORE JSON extraction so a catastrophic reply doesn't also
                # get billed into a loop.
                #
                # v0.30.12: 成本按【实际跑通的模型】actual_model 核算,而不是
                # 计划的 self.model。no-channel fallback 时(如 opus-4-7 降级到
                # sonnet-4-6)用 Opus 费率算会让 total_cost_usd 严重偏高。
                # 若 actual_model 不在价格表(罕见:relay suffix 变体如
                # claude-opus-4-6-high),退回 self.model 的 base 名查费率
                # (suffix 变体与 base 同费率);都没有则 _estimate 返回 0
                # (已有的 unknown-model 行为,运营看到 $0 就知道要补价格表)。
                _cost_model = actual_model or self.model
                if (
                    _cost_model not in COST_PER_1M_INPUT
                    and _cost_model not in COST_PER_1M_OUTPUT
                ):
                    _cost_model = self.model
                _, _call_cost = _accumulate_run_tokens(
                    run_id,
                    _cost_model,
                    input_tokens,
                    output_tokens,
                    cache_read,
                    cache_creation,
                )
                _check_run_budget(run_id)
                _maybe_warn_no_cache_hits(run_id)

                output = self._extract_json(text)
                duration = time.time() - start_time

                # 截断可见化(#3):_extract_json 修补过截断 JSON 时会打
                # _json_truncated 标记。这里按 stage 名再报一次 warning(便于定位
                # 是哪个阶段被截断),标记随 output 一并写进 stage_log.output_data
                # 持久化(下面 update_stage_log 原样写 output),UI/SQL 可查。
                if isinstance(output, dict) and output.get("_json_truncated"):
                    logger.warning(
                        "[%s] 输出被 max_tokens 截断,已用闭合括号修补 —— 产出可能"
                        "残缺(如画像池被砍短)。建议调高该 stage 的 max_tokens 或分块。",
                        self.stage_name,
                    )

                # ── Check if agent is requesting clarification ─────────
                if output.get("status") == "needs_clarification":
                    db.update_stage_log(
                        log_id,
                        status="needs_input",
                        output_data=output,
                        model_used=model_label,
                        tokens_used=total_input_tokens + total_output_tokens,
                        duration_seconds=round(duration, 2),
                    )
                    raise ClarificationNeeded(
                        stage_name=self.stage_name,
                        questions=output.get("questions", []),
                        context=output.get("context", ""),
                        partial_output=output.get("partial_output", {}),
                        log_id=log_id,
                    )

                # The model call + parse already SUCCEEDED — `output` is in
                # hand. A DB write failure here must NOT bubble into the retry
                # loop below (its except would misclassify it and re-run the
                # expensive model call). update_stage_log self-retries transient
                # errors now; if it still fails, log and return the output
                # anyway — losing one stage_log row is far cheaper than
                # re-billing or failing a good run.
                try:
                    db.update_stage_log(
                        log_id,
                        status="completed",
                        output_data=output,
                        model_used=model_label,
                        tokens_used=total_input_tokens + total_output_tokens,
                        duration_seconds=round(duration, 2),
                    )
                except Exception:
                    logger.warning(
                        "[%s] completion stage_log write failed after retries "
                        "— returning output anyway (model call succeeded, not "
                        "re-running)",
                        self.stage_name,
                        exc_info=True,
                    )
                # Push the run-level cost + token totals to pipeline_runs so
                # the UI can display current spend without needing access to
                # the in-process _run_totals dict. Swallow DB errors — cost
                # tracking is observability, not a hard invariant. One small
                # write per agent completion, at most ~14/run.
                try:
                    _run_total = get_run_totals(run_id)
                    _run_tokens = (
                        _run_total.get("input", 0) + _run_total.get("output", 0)
                    )
                    _run_cost = round(float(_run_total.get("cost_usd", 0.0)), 4)
                    db.update_pipeline_run(
                        run_id,
                        total_tokens=_run_tokens,
                        total_cost_usd=_run_cost,
                    )
                except Exception:
                    logger.warning(
                        "[%s] failed to push run totals to pipeline_runs",
                        self.stage_name,
                        exc_info=True,
                    )
                return output

            except ClarificationNeeded:
                raise  # Don't retry clarification requests

            except RunBudgetExceededError:
                # Don't retry budget exhaustion — that would just double-down
                # on the failure. Let it bubble up to the orchestrator, which
                # will mark the run as failed. Still log the stage as failed
                # so the UI shows which stage tripped the guard.
                duration = time.time() - start_time
                db.update_stage_log(
                    log_id,
                    status="failed",
                    error_message=(
                        "RunBudgetExceededError: 流水线累计 token 超过 "
                        "MAX_TOKENS_PER_RUN 上限，已强制终止以防成本失控。"
                        "请检查是否有阶段在反复重试，或在 pipeline/config.py "
                        "调高 MAX_TOKENS_PER_RUN。"
                    ),
                    tokens_used=total_input_tokens + total_output_tokens,
                    duration_seconds=round(duration, 2),
                )
                raise

            except Exception as e:
                last_error = e
                logger.exception(
                    f"[{self.stage_name}] attempt {attempt + 1}/{MAX_RETRIES + 1} failed"
                )
                # Adaptive-RPM safety valve: a 429 means the relay is
                # throttling us. Back the effective rate off toward the safe
                # floor before retrying — so throttling stays the relay's
                # problem, not a flood we keep re-sending.
                if _is_rate_limit_error(e):
                    note_rate_limit_hit(self.stage_name)

                # Vendor-agnostic classification (anthropic.* AND openai.* AND
                # relay string errors). The old code isinstance'd anthropic.*
                # only, so every GPT error fell through to retryable=True and
                # burned retries on auth/400 with the wrong (short) backoff.
                _retryable, _is_5xx = _classify_llm_error(e, attempt)

                if _retryable and attempt < MAX_RETRIES:
                    # 5xx / rate-limit need a longer window to recover
                    # (~10-30s); other transients use the 3s base. Cap at
                    # 30s so a flapping upstream can't stretch one retry into
                    # minutes.
                    _base = 10 if (_is_5xx or _is_rate_limit_error(e)) else RETRY_BASE_DELAY_SECONDS
                    delay = min(_base * (2 ** attempt), 30)
                    await asyncio.sleep(delay)
                elif not _retryable:
                    logger.warning(
                        f"[{self.stage_name}] non-retryable error ({type(e).__name__}), "
                        f"skipping remaining retries"
                    )
                    break

        # All retries exhausted — build a robust, non-empty error message
        duration = time.time() - start_time
        err_str = ""
        if last_error is not None:
            try:
                err_str = str(last_error) or repr(last_error)
            except Exception:
                err_str = repr(last_error)
            tb = traceback.format_exception(type(last_error), last_error, last_error.__traceback__)
            err_str = (
                f"{type(last_error).__name__}: {err_str}\n"
                f"model={self.model} max_tokens={self.max_tokens}\n"
                f"--- traceback ---\n{''.join(tb)}"
            )
        if not err_str.strip():
            err_str = (
                f"Stage {self.stage_name} failed after {MAX_RETRIES + 1} attempts "
                f"but no error info was captured (last_error={last_error!r}, "
                f"model={self.model})"
            )
        # Mask secrets (API keys / JWTs in the traceback) BEFORE persisting —
        # this string is stored long-term in stage_logs and shown in the UI.
        err_str = mask_secrets(err_str)
        # Cap to avoid blowing up the DB column; but leave an explicit marker
        # so the UI/operator knows the tail was cut instead of silently
        # getting 8000 chars that look complete.
        if len(err_str) > 8000:
            truncated_msg = (
                err_str[:7900]
                + "\n\n[... 错误信息被截断于 8000 字符上限，完整 traceback 请看 "
                + "Streamlit 运行控制台日志 ...]"
            )
        else:
            truncated_msg = err_str
        db.update_stage_log(
            log_id,
            status="failed",
            error_message=truncated_msg,
            tokens_used=total_input_tokens + total_output_tokens,
            duration_seconds=round(duration, 2),
        )
        raise last_error  # type: ignore[misc]


# v0.31 及之前 BaseAgent 上这个方法叫 `_call_claude`。v0.32.0 换厂后改名为
# `_call_model`(见该方法 docstring)。别名挂在类上,以防有外部代码或测试
# 按老名字引用/打桩。
BaseAgent._call_claude = BaseAgent._call_model
