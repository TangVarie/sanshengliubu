"""SocialDataX MCP client — direct structured social-data access.

Replaces the Gemini + Google-Search-grounding path for fetching real
Xiaohongshu / XHS content (see pipeline/agents/socialdatax_trend_scout.py).

Why this exists
---------------
The old trend scout reached XHS *through Google* (grounding forced to
``site:xiaohongshu.com``). XHS is a walled garden Google barely indexes,
so that path could only return thin, copyright-filtered snippets with no
engagement metrics and no comments — and it was disabled by default. This
module talks to SocialDataX's official MCP servers instead, which return
first-party structured notes (real note_id, note_url with xsec_token, real
互动量, real 评论区).

Transport
---------
SocialDataX ships as an MCP server over streamable-HTTP (verified against
the ``socialdatax-skills`` npm package v0.2.30):

  - endpoint: ``https://mcp.socialdatax.com/{platform}/mcp``
  - auth:     ``Authorization: Bearer <SOCIALDATAX_API_KEY>``
  - protocol: MCP JSON-RPC (initialize → tools/call)

We speak it from Python with the official ``mcp`` SDK's
``streamablehttp_client`` — pure Python, no Node runtime required in the
deploy environment (Streamlit Cloud).

Failure semantics (advisory-only, identical to the Gemini agents)
-----------------------------------------------------------------
Missing key / disabled  → :class:`SocialDataXNotConfigured`
API / network / protocol error → :class:`SocialDataXCallFailed`

Callers catch both, log a warning, and let the pipeline proceed. Nothing
here is ever allowed to block a run.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import timedelta
from typing import Any

from pipeline.config import (
    ENABLE_SOCIALDATAX,
    SOCIALDATAX_MCP_BASE,
    SOCIALDATAX_REQUEST_TIMEOUT_SECONDS,
)
from pipeline.logger_utils import mask_secrets

logger = logging.getLogger(__name__)

# Env / secrets field names. Primary matches the SocialDataX skill contract;
# the legacy name is what older socialdatax-skills installs used.
_PRIMARY_KEY_NAME = "SOCIALDATAX_API_KEY"
_LEGACY_KEY_NAME = "SOCIAL_MEDIA_MCP_API_KEY"
_KEY_NAMES = (_PRIMARY_KEY_NAME, _LEGACY_KEY_NAME)

# Human-facing platform label → SocialDataX MCP platform id (URL segment).
PLATFORM_ID_MAP = {
    "小红书": "xhs",
    "xhs": "xhs",
    "xiaohongshu": "xhs",
    "rednote": "xhs",
    "抖音": "douyin",
    "douyin": "douyin",
    "快手": "kuaishou",
    "kuaishou": "kuaishou",
    "kwai": "kuaishou",
    "微博": "weibo",
    "weibo": "weibo",
    "视频号": "wechat",
    "微信视频号": "wechat",
    "wechat": "wechat",
    "wechat channels": "wechat",
}


class SocialDataXNotConfigured(RuntimeError):
    """Raised when a SocialDataX call is requested but the backend isn't
    usable (feature disabled, missing API key, ``mcp`` SDK not installed).
    Callers treat this as advisory — the pipeline continues without
    SocialDataX input.
    """


class SocialDataXCallFailed(RuntimeError):
    """Raised when a SocialDataX MCP call errors (auth, quota, network,
    protocol, tool-level error). Callers catch → log warn → proceed.
    Never propagated to the top-level run.
    """


def resolve_platform_id(platform: str | None) -> str | None:
    """Map a human platform label ('小红书', 'Douyin', …) to the SocialDataX
    MCP platform id ('xhs', 'douyin', …). Returns None if unsupported."""
    if not platform:
        return None
    key = str(platform).strip().lower()
    # exact (covers CJK labels which .lower() leaves unchanged)
    if platform.strip() in PLATFORM_ID_MAP:
        return PLATFORM_ID_MAP[platform.strip()]
    return PLATFORM_ID_MAP.get(key)


def _resolve_api_key() -> str:
    """Resolve the SocialDataX API key from Streamlit secrets first (the
    app runtime), then process env (standalone scripts / probe). Mirrors
    the VERTEX_EXPRESS_API_KEY resolution convention in gemini_client.py.
    """
    # 1) Streamlit secrets — primary path inside the app.
    try:
        import streamlit as st  # type: ignore

        for name in _KEY_NAMES:
            try:
                val = st.secrets.get(name, "")
            except Exception:
                val = ""
            if val and str(val).strip():
                return str(val).strip()
    except Exception:
        # streamlit not importable or no secrets context — fall through
        pass

    # 2) Process environment — standalone / probe usage.
    for name in _KEY_NAMES:
        val = os.environ.get(name, "")
        if val and val.strip():
            return val.strip()

    raise SocialDataXNotConfigured(
        f"{_PRIMARY_KEY_NAME} not set in .streamlit/secrets.toml (or env). "
        f"Request one at https://socialdatax.com/?from=npm and add it as a "
        f"top-level `{_PRIMARY_KEY_NAME}` field."
    )


def is_available() -> bool:
    """Quick predicate: is SocialDataX usable at all? Checks the feature
    flag, the ``mcp`` SDK import, and key presence. Does NOT verify
    connectivity — a call can still fail at request time."""
    if not ENABLE_SOCIALDATAX:
        return False
    try:
        import mcp  # noqa: F401
        from mcp.client.streamable_http import (  # noqa: F401
            streamablehttp_client,
        )
    except Exception:
        return False
    try:
        _resolve_api_key()
    except SocialDataXNotConfigured:
        return False
    return True


def _endpoint_for(platform_id: str) -> str:
    base = str(SOCIALDATAX_MCP_BASE).rstrip("/")
    return f"{base}/{platform_id}/mcp"


def _extract_text_content(content: Any) -> str:
    """Concatenate the text of any TextContent blocks in an MCP result's
    ``content`` list — used both for error messages and as a JSON fallback
    when ``structuredContent`` is absent."""
    if not content:
        return ""
    parts: list[str] = []
    for block in content:
        text = getattr(block, "text", None)
        if isinstance(text, str) and text:
            parts.append(text)
    return "\n".join(parts)


async def call_tool(
    platform_id: str,
    tool: str,
    arguments: dict[str, Any] | None = None,
    *,
    timeout_seconds: float | None = None,
) -> Any:
    """Call one SocialDataX MCP tool and return its structured payload.

    Returns ``result.structuredContent`` when the server provides it
    (the normal case for these tools), else the JSON parsed out of the
    text content blocks. The shape of the payload is platform/tool
    specific and is normalized by the calling scout, not here.

    Raises SocialDataXNotConfigured / SocialDataXCallFailed only; both are
    advisory (see module docstring).
    """
    if not ENABLE_SOCIALDATAX:
        raise SocialDataXNotConfigured(
            "ENABLE_SOCIALDATAX=False in pipeline/config.py"
        )

    try:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client
    except Exception as e:  # ImportError or transitive failure
        raise SocialDataXNotConfigured(
            f"mcp SDK not installed (pip install 'mcp>=1.9,<2.0'). "
            f"Import error: {e}"
        )

    api_key = _resolve_api_key()  # raises SocialDataXNotConfigured
    url = _endpoint_for(platform_id)
    headers = {"Authorization": f"Bearer {api_key}"}
    timeout = float(
        timeout_seconds
        if timeout_seconds is not None
        else SOCIALDATAX_REQUEST_TIMEOUT_SECONDS
    )
    args = dict(arguments or {})

    logger.debug(
        "[socialdatax] call %s/%s args=%s",
        platform_id,
        tool,
        mask_secrets(json.dumps(args, ensure_ascii=False)),
    )

    try:
        async with streamablehttp_client(
            url,
            headers=headers,
            timeout=timeout,
            sse_read_timeout=timeout,
        ) as (read_stream, write_stream, _get_session_id):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.call_tool(
                    tool,
                    args,
                    read_timeout_seconds=timedelta(seconds=timeout),
                )
    except SocialDataXNotConfigured:
        raise
    except Exception as e:
        raise SocialDataXCallFailed(
            f"{platform_id}/{tool} call failed: {mask_secrets(str(e))}"
        ) from e

    if getattr(result, "isError", False):
        msg = _extract_text_content(getattr(result, "content", None))
        raise SocialDataXCallFailed(
            f"{platform_id}/{tool} returned an error: "
            f"{mask_secrets(msg) or 'unknown tool error'}"
        )

    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        return structured

    # Fallback: some tools may only return text content — parse JSON out.
    text = _extract_text_content(getattr(result, "content", None))
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        # Return the raw text so the caller can decide; scouts treat a
        # non-dict/list payload as "no usable data".
        return {"_raw_text": text}
