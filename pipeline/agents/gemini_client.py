"""Gemini auxiliary client — wraps google-genai with Vertex Express API key auth.

Purpose
-------
This module is the secondary LLM backend used for "second opinion" tasks:
  - auxiliary vibe critic (arbitration layer over Claude's vibe_critic)
  - structure reviewer of works_builder output

It is deliberately SEPARATE from BaseAgent / _call_claude. Reasons:
  - Different auth (Vertex Express API key, not SA JWT or relay key)
  - Different message shape (Gemini uses contents + systemInstruction, not
    Anthropic's messages + system)
  - Advisory-only semantics: Gemini failures must never crash the pipeline
  - Independent cost accounting — Gemini pricing is 10-100× cheaper than
    Opus for Flash models, so lumping its tokens into the Claude budget
    would distort the MAX_TOKENS_PER_RUN ceiling

Callers:
  - pipeline/agents/gemini_critic.py
  - pipeline/agents/gemini_structure_reviewer.py

Secrets layout:
  VERTEX_EXPRESS_API_KEY = "AIzaSy..."  # top-level key in secrets.toml
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import streamlit as st

from pipeline.config import (
    ENABLE_GEMINI_ASSIST,
    GEMINI_COST_PER_1M_INPUT,
    GEMINI_COST_PER_1M_OUTPUT,
    GEMINI_MAX_OUTPUT_TOKENS,
    GEMINI_MODEL,
    GEMINI_MODEL_OVERRIDES,
    GEMINI_PRICE_TABLE,
)
from pipeline.logger_utils import mask_secrets

logger = logging.getLogger(__name__)


class GeminiNotConfigured(RuntimeError):
    """Raised when a caller asks for a Gemini call but the backend isn't
    configured (missing API key, disabled in config, google-genai not
    importable). Callers should catch this and treat it as advisory —
    the pipeline continues on Claude alone.
    """


class GeminiCallFailed(RuntimeError):
    """Raised when the Gemini API returns an error (auth, model not
    found, quota, network). Callers should catch → log warn → proceed
    without Gemini's input. Never propagated to the top-level run.
    """


# Cache the client at module level. google-genai's Client is cheap to
# construct but has some bootstrap overhead; reusing it across calls in
# the same process avoids that.
_client_cache: dict[str, Any] = {}


def _get_client():
    """Lazy-init the google-genai Client. Raises GeminiNotConfigured when
    the feature isn't available. Never raises for runtime errors — those
    surface on the actual generate call as GeminiCallFailed."""
    if not ENABLE_GEMINI_ASSIST:
        raise GeminiNotConfigured("ENABLE_GEMINI_ASSIST=False in pipeline/config.py")

    api_key = st.secrets.get("VERTEX_EXPRESS_API_KEY", "").strip()
    if not api_key:
        raise GeminiNotConfigured(
            "VERTEX_EXPRESS_API_KEY not set in .streamlit/secrets.toml. "
            "Generate one at GCP Console → Vertex AI → Settings → API keys."
        )

    cached = _client_cache.get("client")
    if cached is not None:
        return cached

    try:
        # Import lazily so an unconfigured install doesn't crash at startup.
        from google import genai  # type: ignore
    except ImportError as e:
        raise GeminiNotConfigured(
            f"google-genai not installed (pip install google-genai). "
            f"Import error: {e}"
        )

    try:
        # Gemini 3.x + Google Search grounding + 24K max_tokens single-
        # call latency can easily hit 2-3 minutes (grounding adds an
        # internal search+summarize step on top of normal generation).
        # google-genai's default HTTP timeout (~60s at the time of
        # writing) fires early and we get a client-side 499 CANCELLED
        # before the model ever finishes. Set a generous 10-minute
        # ceiling — longer than any reasonable single call.
        try:
            from google.genai.types import HttpOptions  # type: ignore
            client = genai.Client(
                api_key=api_key,
                http_options=HttpOptions(timeout=600000),  # ms
            )
        except (ImportError, TypeError):
            # Older SDK versions that don't accept http_options or
            # lack HttpOptions — fall back to default timeout.
            client = genai.Client(api_key=api_key)
            logger.warning(
                "[gemini] installed google-genai version doesn't support "
                "HttpOptions timeout; grounding calls may hit default "
                "client timeout and 499 CANCELLED. Upgrade google-genai "
                "to >=0.3.0 if you see repeated cancellations."
            )
    except Exception as e:
        raise GeminiNotConfigured(
            f"Failed to construct google-genai Client: {e}"
        )

    _client_cache["client"] = client
    return client


def is_available() -> bool:
    """Quick predicate for UI / callers that want to know whether Gemini
    assist is usable without having to catch exceptions. Does the same
    config checks as _get_client but swallows the NotConfigured signal.
    Does NOT verify connectivity — a call can still fail at generate time.
    """
    try:
        _get_client()
        return True
    except GeminiNotConfigured:
        return False


def list_available_models() -> list[dict[str, Any]]:
    """Call the Gemini ListModels API. Returns a list of dicts, each:
      {"id": "gemini-2.5-pro",
       "display_name": "Gemini 2.5 Pro",
       "input_token_limit": 2000000,
       "output_token_limit": 8192,
       "supported_methods": ["generateContent", ...]}

    Used by the Settings page's diagnostic button to answer "what's
    actually callable with this API key". Helps the user pick a
    GEMINI_MODEL value that their account can reach, instead of
    trial-and-error on the pipeline.

    Raises GeminiNotConfigured / GeminiCallFailed on errors so the
    caller can distinguish "you didn't set it up" from "the API
    rejected the request".
    """
    client = _get_client()
    try:
        raw = list(client.models.list())
    except Exception as e:
        raise GeminiCallFailed(
            f"ListModels call failed: {type(e).__name__}: {e}"
        ) from e

    out: list[dict[str, Any]] = []
    for m in raw:
        name = getattr(m, "name", "") or ""
        # Normalize: the API returns "models/gemini-2.5-pro"; strip prefix.
        short_id = name.split("/", 1)[1] if "/" in name else name
        out.append(
            {
                "id": short_id,
                "display_name": getattr(m, "display_name", "") or short_id,
                "input_token_limit": int(
                    getattr(m, "input_token_limit", 0) or 0
                ),
                "output_token_limit": int(
                    getattr(m, "output_token_limit", 0) or 0
                ),
                "supported_methods": list(
                    getattr(m, "supported_actions", None)
                    or getattr(m, "supported_generation_methods", None)
                    or []
                ),
            }
        )
    # Put gemini-* first, grouped by rough version (2.5 > 2.0 > 1.5) so
    # the UI puts the most relevant models at the top.
    out.sort(
        key=lambda r: (
            0 if r["id"].startswith("gemini-") else 1,
            r["id"],
        )
    )
    return out


def resolve_gemini_model(role: str | None) -> str:
    """Return the Gemini model ID to use for a given pipeline role.

    Looks up `role` in GEMINI_MODEL_OVERRIDES (set in pipeline/config.py).
    Falls back to GEMINI_MODEL when the role isn't pinned or is None.

    Known role keys (must match what agents pass in):
      "critic", "structure_reviewer", "trend_scout",
      "image_transcriber", "screenshot_analyzer", "reference_analyzer"

    Unknown role names silently fall back to the default — that's fine,
    callers that want strict checking can wrap this themselves.
    """
    if role and role in GEMINI_MODEL_OVERRIDES:
        return GEMINI_MODEL_OVERRIDES[role]
    return GEMINI_MODEL


def _estimate_cost_usd(
    input_tokens: int,
    output_tokens: int,
    model_id: str | None = None,
) -> float:
    """Estimate USD cost for one Gemini call. Picks rates from
    GEMINI_PRICE_TABLE keyed on `model_id`, falling back to
    GEMINI_COST_PER_1M_INPUT/OUTPUT when the model isn't listed.

    Kept local so Gemini pricing changes don't leak into the Claude cost
    table (agents/__init__.py uses its own MODELS rate map).
    """
    rates = GEMINI_PRICE_TABLE.get(model_id or "") if model_id else None
    if rates:
        in_rate = float(rates.get("input", GEMINI_COST_PER_1M_INPUT))
        out_rate = float(rates.get("output", GEMINI_COST_PER_1M_OUTPUT))
    else:
        in_rate = GEMINI_COST_PER_1M_INPUT
        out_rate = GEMINI_COST_PER_1M_OUTPUT
    return (input_tokens * in_rate + output_tokens * out_rate) / 1_000_000


def call_gemini_json(
    system_prompt: str,
    user_message: str,
    *,
    model: str | None = None,
    max_output_tokens: int | None = None,
    enable_search: bool = False,
    enable_url_context: bool = False,
    images: list[tuple[bytes, str]] | None = None,
) -> dict[str, Any]:
    """Run one Gemini call and parse the response as JSON.

    images: optional list of (raw_bytes, mime_type) tuples for
    multimodal (Vision) calls. When provided, each image is sent as
    an inline Part alongside the text user_message. This is how we
    send screenshots for OCR + analysis.

    Returns a dict with:
      - "data": the parsed JSON output (guaranteed dict on success)
      - "input_tokens": int, non-cached input
      - "output_tokens": int
      - "cost_usd": float, this-call cost estimate
      - "model": the model ID that was called
      - "grounding_urls": list[str], URLs Gemini actually cited via the
        Google Search tool (only populated when enable_search=True).
        Useful as a verification layer: if Gemini claims to have seen
        posts X/Y/Z, the URLs should be in this list. Empty list means
        grounding produced no citations (search returned nothing or
        Gemini chose not to cite).

    Raises GeminiNotConfigured / GeminiCallFailed on any failure. Never
    raises from JSON parse failures — falls back to wrapping the raw
    text in {"_raw_text": "...", "_parse_error": "..."} so callers can
    decide whether to ignore or treat as a soft-fail.

    Shape is deliberately JSON-oriented because every current caller
    (critic, structure reviewer, trend scout) needs structured output.
    Tasks that want free-form text should add a call_gemini_text variant.

    enable_search=True attaches Google Search as a tool so Gemini can
    run live searches mid-response. Citations come back in
    grounding_urls. Note that JSON mime type + grounding can conflict
    with some models — if the response comes back as prose instead of
    JSON, _extract_json falls back through ```json``` fence / outermost
    braces before giving up.

    enable_url_context=True lets Gemini fetch specific URLs the user
    mentions in the prompt and include their content as context. Useful
    for "analyze this specific post" workflows.
    """
    client = _get_client()
    model_id = model or GEMINI_MODEL
    max_tokens = max_output_tokens or GEMINI_MAX_OUTPUT_TOKENS

    try:
        from google.genai import types  # type: ignore

        # Tool list — only attached when at least one is enabled, because
        # empty tools=[] can confuse some models.
        tools: list[Any] = []
        if enable_search:
            tools.append(types.Tool(google_search=types.GoogleSearch()))
            # Grounding adds internal thought/narrative tokens before the
            # actual JSON output. The default 8K cap often runs out mid-
            # JSON, producing a truncated response that fails parsing.
            # Bump to 24K for grounded calls — pure text output of 10-15
            # posts is typically <4K, so headroom to spare for reasoning.
            if max_tokens < 24576:
                max_tokens = 24576
        if enable_url_context:
            # url_context is a separate tool that lets the model fetch
            # specific URLs mentioned in the prompt. Graceful: the type
            # may not exist on older SDK versions — guard the import.
            try:
                tools.append(types.Tool(url_context=types.UrlContext()))
            except AttributeError:
                logger.warning(
                    "[gemini] url_context tool not available in installed "
                    "google-genai SDK; continuing without it"
                )

        # Safety settings: lower all available thresholds to avoid
        # false-positive blocks. RECITATION specifically can't be
        # disabled (it's a separate system), but relaxing all safety
        # thresholds reduces the chance of the model being blocked
        # for "dangerous" content when it's just quoting xhs posts.
        try:
            _safety = [
                types.SafetySetting(
                    category="HARM_CATEGORY_HARASSMENT",
                    threshold="BLOCK_NONE",
                ),
                types.SafetySetting(
                    category="HARM_CATEGORY_HATE_SPEECH",
                    threshold="BLOCK_NONE",
                ),
                types.SafetySetting(
                    category="HARM_CATEGORY_SEXUALLY_EXPLICIT",
                    threshold="BLOCK_NONE",
                ),
                types.SafetySetting(
                    category="HARM_CATEGORY_DANGEROUS_CONTENT",
                    threshold="BLOCK_NONE",
                ),
            ]
        except (AttributeError, TypeError):
            _safety = []  # SDK too old

        config_kwargs: dict[str, Any] = {
            "system_instruction": system_prompt,
            "max_output_tokens": max_tokens,
            # Lower temperature for critic/reviewer/scout — consistent
            # judgment, not creative variation.
            "temperature": 0.3,
        }
        if _safety:
            config_kwargs["safety_settings"] = _safety
        # response_mime_type=application/json and grounding tools conflict
        # on some model IDs (grounding pushes response into natural text).
        # Only request JSON mime when grounding is OFF. With grounding ON,
        # we rely on _extract_json's markdown-fence / brace fallback.
        if not tools:
            config_kwargs["response_mime_type"] = "application/json"
        if tools:
            config_kwargs["tools"] = tools

        # Build contents: text + optional images for multimodal calls.
        if images:
            # v0.29.10: 新版 google-genai SDK 的 Part.from_text 要 keyword
            # 参数 text=...,不能位置传。之前写 Part.from_text(user_message)
            # 报 `TypeError: Part.from_text() takes 1 posit...`,所有带图片
            # 的 Gemini 调用(image transcriber / reference_analyzer / 各
            # reference_analyzer)全挂。
            _content_parts: list[Any] = [types.Part.from_text(text=user_message)]
            for img_bytes, img_mime in images:
                _content_parts.append(
                    types.Part.from_bytes(data=img_bytes, mime_type=img_mime)
                )
            _contents: Any = _content_parts
        else:
            _contents = user_message

        # R-026: bounded retry on transient errors (429/503/504/timeout).
        # Auth / validation errors fall through immediately via fail-fast
        # in call_with_retry. max_attempts=3 + 2s initial wait keeps the
        # worst-case wall clock under ~12s, far cheaper than failing the
        # whole pipeline run that may have already burned $1+ upstream.
        from pipeline.llm_retry import call_with_retry

        def _do_generate():
            return client.models.generate_content(
                model=model_id,
                contents=_contents,
                config=types.GenerateContentConfig(**config_kwargs),
            )

        response = call_with_retry(
            _do_generate,
            max_attempts=3,
            initial_wait=2.0,
            max_wait=30.0,
            operation=f"gemini.{model_id}",
        )
    except Exception as e:
        raise GeminiCallFailed(
            f"Gemini generate_content failed (model={model_id}): {type(e).__name__}: {e}"
        ) from e

    # Token counts — Gemini returns them via usage_metadata. Fields can
    # be None on error responses; coerce safely.
    usage = getattr(response, "usage_metadata", None)
    input_tokens = int(getattr(usage, "prompt_token_count", 0) or 0) if usage else 0
    output_tokens = int(getattr(usage, "candidates_token_count", 0) or 0) if usage else 0

    # Extract response text. Try response.text first (standard path);
    # if empty (can happen when tools are active and the model returned
    # tool calls / grounding metadata instead of plain text), manually
    # walk the response parts and concatenate any .text fields.
    raw_text = (getattr(response, "text", "") or "").strip()
    if not raw_text:
        try:
            candidates = getattr(response, "candidates", None) or []
            pieces: list[str] = []
            for cand in candidates:
                content = getattr(cand, "content", None)
                if not content:
                    continue
                for part in getattr(content, "parts", None) or []:
                    t = getattr(part, "text", None)
                    if t:
                        pieces.append(str(t))
            raw_text = "\n".join(pieces).strip()
        except Exception as e:
            logger.debug("[gemini] fallback text extraction failed: %r", e)

    if not raw_text:
        # Truly empty — log finish_reason / safety signals to help diagnosis.
        _finish_reason = "unknown"
        try:
            candidates = getattr(response, "candidates", None) or []
            if candidates:
                _finish_reason = str(getattr(candidates[0], "finish_reason", "unknown"))
        except Exception:
            pass
        raise GeminiCallFailed(
            f"Gemini returned empty text (model={model_id}, "
            f"input_tokens={input_tokens}, output_tokens={output_tokens}, "
            f"finish_reason={_finish_reason}). "
            f"Likely safety-blocked, quota-limited, or max_tokens hit "
            f"before any text was produced (grounding can burn 3-5K tokens "
            f"on thought/narrative before emitting JSON)."
        )

    # Always log the first 1KB of raw text to server stderr. Useful when
    # the UI preview capture chain breaks — this gives us a fallback
    # diagnostic path in Streamlit Cloud logs.
    # R-023: raw_text may carry user-pasted secrets / URLs with bearer
    # tokens; mask before writing to stderr.
    logger.info(
        "[gemini] raw text first 1KB (model=%s, in=%d, out=%d): %s",
        model_id,
        input_tokens,
        output_tokens,
        mask_secrets(raw_text[:1024]).replace("\n", " | "),
    )

    parsed = _extract_json(raw_text)
    # If parsing failed, stuff the raw text into the return dict so
    # downstream callers don't have to re-extract it from response.
    # (_extract_json already does text[:2000] on failure — keep that.)
    # Also include raw_text as a separate debug field even on SUCCESS
    # so the UI can optionally show it for "what did Gemini actually
    # say" debugging.
    if isinstance(parsed, dict) and "_parse_error" not in parsed:
        parsed.setdefault("_raw_text_debug", raw_text[:2000])

    # Extract grounding citations when search was enabled. Gemini puts
    # them on the first candidate's grounding_metadata.grounding_chunks.
    grounding_urls: list[str] = []
    if enable_search:
        try:
            candidates = getattr(response, "candidates", None) or []
            if candidates:
                gm = getattr(candidates[0], "grounding_metadata", None)
                chunks = getattr(gm, "grounding_chunks", None) or [] if gm else []
                for ch in chunks:
                    web = getattr(ch, "web", None)
                    if web:
                        uri = getattr(web, "uri", None)
                        if uri:
                            grounding_urls.append(str(uri))
        except Exception as e:
            logger.debug(
                "[gemini] failed to extract grounding metadata: %r", e
            )

    return {
        "data": parsed,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": _estimate_cost_usd(input_tokens, output_tokens, model_id),
        "model": model_id,
        "grounding_urls": grounding_urls,
    }


def _extract_json(text: str) -> dict[str, Any]:
    """Tolerant JSON extraction for Gemini output. response_mime_type=
    application/json USUALLY yields clean JSON, but we've seen Gemini
    occasionally prepend ```json fences or prose, so we defend.

    Unlike BaseAgent._extract_json in agents/__init__.py, we do NOT
    attempt truncation repair here — Gemini's auxiliary role means a
    garbled response is better returned as {_parse_error} than
    silently patched up. The caller can treat any _parse_error as
    "Gemini said nothing actionable" and move on.
    """
    # Direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # ```json ... ``` block
    m = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass

    # Outermost braces
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass

    # Give up — return the raw for the caller to decide on
    return {
        "_parse_error": "could_not_extract_json",
        "_raw_text": text[:2000],
    }
