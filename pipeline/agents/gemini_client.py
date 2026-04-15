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
)

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
        client = genai.Client(api_key=api_key)
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


def _estimate_cost_usd(input_tokens: int, output_tokens: int) -> float:
    """Same formula as _estimate_call_cost_usd in agents/__init__.py but
    hard-wired to Gemini rates. Kept local so Gemini pricing changes
    don't leak into the Claude cost table."""
    return (
        input_tokens * GEMINI_COST_PER_1M_INPUT
        + output_tokens * GEMINI_COST_PER_1M_OUTPUT
    ) / 1_000_000


def call_gemini_json(
    system_prompt: str,
    user_message: str,
    *,
    model: str | None = None,
    max_output_tokens: int | None = None,
) -> dict[str, Any]:
    """Run one Gemini call and parse the response as JSON.

    Returns a dict with:
      - "data": the parsed JSON output (guaranteed dict on success)
      - "input_tokens": int, non-cached input
      - "output_tokens": int
      - "cost_usd": float, this-call cost estimate
      - "model": the model ID that was called

    Raises GeminiNotConfigured / GeminiCallFailed on any failure. Never
    raises from JSON parse failures — falls back to wrapping the raw
    text in {"_raw_text": "...", "_parse_error": "..."} so callers can
    decide whether to ignore or treat as a soft-fail.

    Shape is deliberately JSON-oriented because every current caller
    (critic, structure reviewer) needs structured output. Tasks that
    want free-form text should add a call_gemini_text variant later.
    """
    client = _get_client()
    model_id = model or GEMINI_MODEL
    max_tokens = max_output_tokens or GEMINI_MAX_OUTPUT_TOKENS

    try:
        # google-genai uses `contents` for the user turn and a separate
        # `system_instruction` in config. JSON mode is requested via
        # response_mime_type; the model will (best-effort) emit a single
        # valid JSON object. We still run _extract_json as a safety net
        # because Gemini occasionally wraps JSON in prose anyway.
        from google.genai import types  # type: ignore

        response = client.models.generate_content(
            model=model_id,
            contents=user_message,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                response_mime_type="application/json",
                max_output_tokens=max_tokens,
                # Lower temperature for critic/reviewer roles — we want
                # consistent judgment, not creative variation.
                temperature=0.3,
            ),
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

    raw_text = (getattr(response, "text", "") or "").strip()
    if not raw_text:
        raise GeminiCallFailed(
            f"Gemini returned empty text (model={model_id}, "
            f"input_tokens={input_tokens}, output_tokens={output_tokens}). "
            f"Likely safety-blocked or quota-limited."
        )

    parsed = _extract_json(raw_text)

    return {
        "data": parsed,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": _estimate_cost_usd(input_tokens, output_tokens),
        "model": model_id,
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
