"""Gemini reference-post analyzer — fetch user-pasted Xiaohongshu URLs.

Differs from gemini_trend_scout in two ways:
  - Input is specific URLs the user provided, not keywords
  - Uses url_context tool instead of google_search

Xiaohongshu pages are often JS-rendered and/or login-gated, so
url_context frequently returns only og:title + og:image (not full
body). The prompt explicitly tells the model to mark fetch_status
honestly (ok / partial / js_rendered_blocked / not_accessible)
rather than hallucinate body content when access is blocked.

Advisory-only failure semantics — all errors surface as skipped,
pipeline proceeds without reference-post data.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from pipeline.agents.gemini_client import (
    GeminiCallFailed,
    GeminiNotConfigured,
    call_gemini_json,
    resolve_gemini_model,
)

logger = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).parent.parent / "prompts"

_FORBIDDEN_SUMMARY_KEYS = frozenset({
    "trends", "trend_analysis", "analysis", "summary", "insights",
    "common_patterns", "recommendations", "suggested_hooks",
    "observed_patterns", "high_level_takeaways",
})


def _load_prompt() -> str:
    path = _PROMPTS_DIR / "gemini_reference_analyzer.md"
    return path.read_text(encoding="utf-8")


def _strip_summaries(data: dict) -> dict:
    return {k: v for k, v in data.items() if k not in _FORBIDDEN_SUMMARY_KEYS}


def _validate_post(post: Any) -> dict | None:
    """Accept post if it has a URL. body_fragment can be empty (OK for
    url_context that only got og tags). Anything claiming ok fetch_status
    but missing both title and body is suspicious — drop it."""
    if not isinstance(post, dict):
        return None
    url = str(post.get("url", "")).strip()
    if not url:
        return None
    title = str(post.get("title", "")).strip()
    body = str(post.get("body_fragment", "")).strip()
    status = str(post.get("fetch_status", "")).strip()
    if status == "ok" and not title and not body:
        return None  # suspicious: claims success but content is empty
    return {
        "url": url,
        "title": title,
        "body_fragment": body,
        "cover_image_url": str(post.get("cover_image_url", "")).strip(),
        "fetch_status": status or "unknown",
        "notes": str(post.get("notes", "")).strip(),
    }


async def run_reference_analyzer(
    urls: list[str],
    *,
    platform: str = "小红书",
) -> dict[str, Any]:
    """Fetch user-pasted reference post URLs via Gemini's url_context.

    Returns:
      {
        "verdict": "all_pass | partial | skipped",
        "posts": [...validated per-URL records...],
        "_summary_stats": {attempted, ok, partial, failed},
        "_skip_reason": null | "...",
        "_gemini_usage": {...},
      }

    Each input URL produces exactly one output record even on failure
    (fetch_status tells you what happened). This keeps accounting clean
    for the UI: "you gave me 5 URLs, here's what I got for each."
    """
    urls = [u for u in (urls or []) if u and isinstance(u, str)]
    if not urls:
        return {
            "verdict": "skipped",
            "posts": [],
            "_skip_reason": "no urls provided",
        }

    user_payload = {"urls": urls, "platform": platform}
    user_message = json.dumps(user_payload, ensure_ascii=False, indent=2)

    try:
        system_prompt = _load_prompt()
    except Exception as e:
        logger.warning("[ref_analyzer] prompt load failed: %s", e)
        return {
            "verdict": "skipped",
            "posts": [],
            "_skip_reason": f"prompt_load_failed: {e}",
        }

    try:
        result = call_gemini_json(
            system_prompt,
            user_message,
            enable_url_context=True,
            model=resolve_gemini_model("reference_analyzer"),
        )
    except GeminiNotConfigured as e:
        logger.debug("[ref_analyzer] not configured: %s", e)
        return {
            "verdict": "skipped",
            "posts": [],
            "_skip_reason": f"not_configured: {e}",
        }
    except GeminiCallFailed as e:
        logger.warning("[ref_analyzer] call failed: %s", e)
        return {
            "verdict": "skipped",
            "posts": [],
            "_skip_reason": f"call_failed: {e}",
        }

    raw_data = result.get("data")
    if not isinstance(raw_data, dict) or "_parse_error" in raw_data:
        _preview = ""
        if isinstance(raw_data, dict):
            _preview = str(raw_data.get("_raw_text", ""))[:500]
        logger.warning(
            "[ref_analyzer] output parse failed. Gemini raw output preview: %s",
            _preview[:300] or "(no preview)",
        )
        return {
            "verdict": "skipped",
            "posts": [],
            "_skip_reason": "parse_error",
            "_raw_text_preview": _preview,
            "_gemini_usage": {
                "input_tokens": result.get("input_tokens", 0),
                "output_tokens": result.get("output_tokens", 0),
                "cost_usd": result.get("cost_usd", 0.0),
                "model": result.get("model"),
            },
        }

    raw_data = _strip_summaries(raw_data)

    raw_posts = raw_data.get("posts")
    if not isinstance(raw_posts, list):
        raw_posts = []

    validated: list[dict] = []
    for p in raw_posts:
        v = _validate_post(p)
        if v is not None:
            validated.append(v)

    stats = raw_data.get("_summary_stats") or {}
    if not isinstance(stats, dict):
        stats = {}

    usage = {
        "input_tokens": result.get("input_tokens", 0),
        "output_tokens": result.get("output_tokens", 0),
        "cost_usd": result.get("cost_usd", 0.0),
        "model": result.get("model"),
    }

    # verdict: all_pass if every URL got at least a title OR body;
    # partial if some succeeded some failed; skipped handled above.
    ok_count = sum(
        1 for p in validated
        if p["fetch_status"] == "ok" and (p["title"] or p["body_fragment"])
    )
    partial_count = sum(
        1 for p in validated
        if p["fetch_status"] == "partial" and (p["title"] or p["cover_image_url"])
    )
    effective_count = ok_count + partial_count
    if effective_count == len(urls):
        verdict = "all_pass"
    elif effective_count > 0:
        verdict = "partial"
    else:
        verdict = "no_access"

    logger.info(
        "[ref_analyzer] verdict=%s, got %d/%d (ok=%d, partial=%d), cost=$%.4f",
        verdict,
        effective_count,
        len(urls),
        ok_count,
        partial_count,
        usage["cost_usd"],
    )

    return {
        "verdict": verdict,
        "posts": validated,
        "_summary_stats": {
            "attempted": len(urls),
            "ok": ok_count,
            "partial": partial_count,
            "failed": len(urls) - effective_count,
        },
        "_gemini_usage": usage,
    }
