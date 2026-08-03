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

Use :func:`open_session` when making several calls to the same platform
(one TCP+TLS connect + MCP initialize handshake for the whole batch);
:func:`call_tool` is the one-shot convenience wrapper.

Failure semantics
-----------------
Missing key / disabled / mcp SDK absent → :class:`SocialDataXNotConfigured`
API / network / protocol error (after bounded transient retries)
→ :class:`SocialDataXCallFailed`

This client only ever raises these two typed exceptions — POLICY lives in
the caller: the orchestrator's POST scout treats them as advisory (skip and
proceed), while the PRE scout may escalate to a fail-fast run abort under
``SOCIALDATAX_TREND_SCOUT_PRE_REQUIRED`` (missing calibration data skews
the whole strategy; see pipeline/config.py).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any, AsyncIterator, Awaitable, Callable

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

# Bounded transient retry: attempts = 1 initial + _TRANSIENT_RETRIES.
# Matters because the PRE scout can run under REQUIRED/fail-fast — a
# single transient 429/5xx must not kill a whole pipeline run.
_TRANSIENT_RETRIES = 2
_TRANSIENT_BACKOFF_SECONDS = (1.0, 2.0)
_TRANSIENT_MARKERS = (
    "429", "rate limit", "too many request",
    "502", "503", "504", "bad gateway", "service unavailable",
    "gateway timeout", "timed out", "timeout",
    "connection reset", "connection refused", "connection error",
    "temporarily", "eof occurred", "broken pipe",
)

# Human-facing platform label → SocialDataX MCP platform id (URL segment).
# All ASCII keys lowercase; CJK keys are unaffected by .lower().
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
    """


class SocialDataXCallFailed(RuntimeError):
    """Raised when a SocialDataX MCP call errors (auth, quota, network,
    protocol, tool-level error) after bounded transient retries.
    """


def resolve_platform_id(platform: str | None) -> str | None:
    """Map a human platform label ('小红书', 'Douyin', …) to the SocialDataX
    MCP platform id ('xhs', 'douyin', …). Returns None if unsupported."""
    if not platform:
        return None
    return PLATFORM_ID_MAP.get(str(platform).strip().lower())


def _resolve_api_key() -> str:
    """Resolve the SocialDataX API key from Streamlit secrets first (the
    app runtime), then process env (standalone scripts / probe). Mirrors
    the API-key resolution convention used elsewhere in pipeline/agents.
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
    connectivity — a call can still fail at request time. Used by the
    settings page to render configuration status."""
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


def _is_transient(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(marker in msg for marker in _TRANSIENT_MARKERS)


def _unwrap_result(platform_id: str, tool: str, result: Any) -> Any:
    """Shared result unwrapping for session and one-shot paths."""
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


# A bound session call: (tool, arguments) -> structured payload.
SessionCall = Callable[[str, dict[str, Any] | None], Awaitable[Any]]


@asynccontextmanager
async def open_session(
    platform_id: str,
    *,
    timeout_seconds: float | None = None,
) -> AsyncIterator[SessionCall]:
    """Open ONE MCP session to a platform endpoint and yield a bound
    ``call(tool, arguments)`` coroutine. All calls through it share a
    single TCP+TLS connection and a single MCP initialize handshake —
    use this whenever making more than one call (the trend scout's
    hot-list + N keyword searches, for example).

    Each yielded call retries transient failures (429/5xx/timeouts) with
    bounded backoff before raising SocialDataXCallFailed. Session-level
    connect/initialize errors raise SocialDataXCallFailed from the
    context manager itself.
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
            f"mcp SDK not installed (pip install 'mcp>=1.9.4,<2.0'). "
            f"Import error: {e}"
        )

    api_key = _resolve_api_key()  # raises SocialDataXNotConfigured
    url = _endpoint_for(platform_id)
    headers = {"Authorization": f"Bearer {api_key}"}
    # timedelta works on every mcp version; bare floats crash mcp
    # 1.9.0-1.9.3 (their transport does `timeout.seconds`).
    timeout_td = timedelta(
        seconds=float(
            timeout_seconds
            if timeout_seconds is not None
            else SOCIALDATAX_REQUEST_TIMEOUT_SECONDS
        )
    )

    try:
        async with streamablehttp_client(
            url,
            headers=headers,
            timeout=timeout_td,
            sse_read_timeout=timeout_td,
        ) as (read_stream, write_stream, _get_session_id):
            # read_timeout_seconds is the session-level default deadline for
            # EVERY request, including initialize(). Without it, the handshake
            # can hang indefinitely under an SSE keepalive (transport-level
            # timeout/sse_read_timeout don't bound the app-level request) — the
            # heartbeat keeps ticking so the reaper thinks the run is alive
            # while the PRE 取样 stage never advances.
            async with ClientSession(
                read_stream, write_stream, read_timeout_seconds=timeout_td
            ) as session:
                await session.initialize()

                async def _call(
                    tool: str, arguments: dict[str, Any] | None = None
                ) -> Any:
                    args = dict(arguments or {})
                    logger.debug(
                        "[socialdatax] call %s/%s args=%s",
                        platform_id,
                        tool,
                        mask_secrets(json.dumps(args, ensure_ascii=False)),
                    )
                    last_exc: Exception | None = None
                    for attempt in range(1 + _TRANSIENT_RETRIES):
                        try:
                            result = await session.call_tool(
                                tool,
                                args,
                                read_timeout_seconds=timeout_td,
                            )
                            return _unwrap_result(platform_id, tool, result)
                        except SocialDataXCallFailed as e:
                            # A tool-level error (result.isError) can STILL be a
                            # transient upstream throttle (429/503/rate limit)
                            # surfaced as error CONTENT rather than a transport
                            # exception. Retry those exactly like a transport
                            # transient; a real auth/quota/bad-arg tool error is
                            # not transient and raises immediately. Matters most
                            # under REQUIRED/fail-fast: a retryable blip must not
                            # kill the whole pipeline run.
                            last_exc = e
                            if attempt < _TRANSIENT_RETRIES and _is_transient(e):
                                delay = _TRANSIENT_BACKOFF_SECONDS[
                                    min(
                                        attempt,
                                        len(_TRANSIENT_BACKOFF_SECONDS) - 1,
                                    )
                                ]
                                logger.warning(
                                    "[socialdatax] transient tool-level error on "
                                    "%s/%s (attempt %d/%d), retrying in %.0fs: %s",
                                    platform_id,
                                    tool,
                                    attempt + 1,
                                    1 + _TRANSIENT_RETRIES,
                                    delay,
                                    mask_secrets(str(e)),
                                )
                                await asyncio.sleep(delay)
                                continue
                            raise  # non-transient tool error, or retries exhausted
                        except Exception as e:
                            last_exc = e
                            if (
                                attempt < _TRANSIENT_RETRIES
                                and _is_transient(e)
                            ):
                                delay = _TRANSIENT_BACKOFF_SECONDS[
                                    min(
                                        attempt,
                                        len(_TRANSIENT_BACKOFF_SECONDS) - 1,
                                    )
                                ]
                                logger.warning(
                                    "[socialdatax] transient error on "
                                    "%s/%s (attempt %d/%d), retrying in "
                                    "%.0fs: %s",
                                    platform_id,
                                    tool,
                                    attempt + 1,
                                    1 + _TRANSIENT_RETRIES,
                                    delay,
                                    mask_secrets(str(e)),
                                )
                                await asyncio.sleep(delay)
                                continue
                            break
                    raise SocialDataXCallFailed(
                        f"{platform_id}/{tool} call failed: "
                        f"{mask_secrets(str(last_exc))}"
                    ) from last_exc

                yield _call
    except (SocialDataXNotConfigured, SocialDataXCallFailed):
        raise
    except Exception as e:
        # Connect / initialize / teardown failure.
        raise SocialDataXCallFailed(
            f"{platform_id} session failed: {mask_secrets(str(e))}"
        ) from e


async def call_tool(
    platform_id: str,
    tool: str,
    arguments: dict[str, Any] | None = None,
    *,
    timeout_seconds: float | None = None,
) -> Any:
    """One-shot convenience wrapper: open a session, make a single tool
    call, tear down. For multiple calls to the same platform use
    :func:`open_session` instead (one handshake for the batch).

    Returns ``result.structuredContent`` when the server provides it,
    else the JSON parsed out of the text content blocks. Raises
    SocialDataXNotConfigured / SocialDataXCallFailed only.
    """
    async with open_session(
        platform_id, timeout_seconds=timeout_seconds
    ) as call:
        return await call(tool, arguments)
