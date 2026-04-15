"""Gemini 小红书原文取样官 — fetches RAW current posts, no summarization.

Used by two pipeline points:
  1. Pre-secretariat (A1): enrich the brief with real trending posts
     so secretariat's strategy is calibrated against actual current
     content, not Claude's prior assumptions.
  2. Post-pipeline (A2): per-direction reference posts so the user can
     compare our demos against what's actually hot right now.

Key invariant enforced here: the agent output must be RAW POSTS ONLY,
never "trend analysis" or summaries. This is enforced at three levels:

  1. Prompt (gemini_trend_scout.md) explicitly forbids summary fields
     and demands original titles/snippets verbatim.
  2. Google search queries are forced to site:xiaohongshu.com to avoid
     analysis blogs on zhihu/36kr/etc.
  3. Post-process filter strips any "trends"/"summary"/"insights" keys
     the model might have sneaked in anyway, and drops any post whose
     URL doesn't contain xiaohongshu.com.

Advisory-only failure semantics (identical to other Gemini agents):
any error → empty result, pipeline proceeds without trend data.
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
)

logger = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).parent.parent / "prompts"

# Fields the user EXPLICITLY forbade the model from producing. Even
# though the prompt says "don't emit these", some models still sneak
# in abstracted summaries — strip them in post-processing so downstream
# consumers only ever see raw posts.
_FORBIDDEN_SUMMARY_KEYS = frozenset({
    "trends", "trend_analysis", "analysis", "summary", "insights",
    "common_patterns", "recommendations", "suggested_hooks",
    "observed_patterns", "high_level_takeaways",
})


def _load_prompt() -> str:
    path = _PROMPTS_DIR / "gemini_trend_scout.md"
    return path.read_text(encoding="utf-8")


def _strip_summaries(data: dict) -> dict:
    """Remove any top-level keys in the forbidden summary set. Returns
    a shallow copy so the caller doesn't mutate the original."""
    return {k: v for k, v in data.items() if k not in _FORBIDDEN_SUMMARY_KEYS}


def _validate_post(post: Any, required_domain: str = "xiaohongshu.com") -> dict | None:
    """Accept a post dict only if it has a URL that looks like Xiaohongshu
    content. Trims the post to the whitelisted fields we care about so
    the model can't leak analysis text through extra fields.

    URL acceptance is broader than a single-domain check because Google's
    index of xiaohongshu.com proper is very thin; we accept:
      - xiaohongshu.com        (canonical domain)
      - xhslink.com            (official share links)
      - any URL with `xhs` substring except known false-positives
    Plus a fallback: the model may flag _suspect_repost for posts found
    on weibo/tieba/etc. that quoted a xhs original — we keep those so
    the user can decide.
    """
    if not isinstance(post, dict):
        return None
    url = str(post.get("url", "")).strip()
    if not url:
        return None
    url_lower = url.lower()

    # Known false-positive substrings we DON'T want even though "xhs"
    # appears in them (e.g. "xhsshop" is a taobao store brand, not xhs).
    _xhs_blocklist = ("xhsshop", "xhsoutlet")

    is_xhs_url = (
        "xiaohongshu.com" in url_lower
        or "xhslink.com" in url_lower
        or (
            "xhs" in url_lower
            and not any(b in url_lower for b in _xhs_blocklist)
        )
    )
    is_acceptable_repost = bool(post.get("_suspect_repost", False))

    if not is_xhs_url and not is_acceptable_repost:
        return None

    title = str(post.get("title", "")).strip()
    snippet = str(post.get("snippet", "")).strip()
    if not title and not snippet:
        return None  # empty post carries no signal
    return {
        "url": url,
        "title": title,
        "snippet": snippet,
        "cover_image_url": str(post.get("cover_image_url", "")).strip(),
        "_suspect_analysis": bool(post.get("_suspect_analysis", False)),
        "_suspect_repost": is_acceptable_repost,
    }


async def run_trend_scout(
    keywords: list[str],
    *,
    platform: str = "小红书",
    target_count: int = 10,
    required_domain: str = "xiaohongshu.com",
) -> dict[str, Any]:
    """Fetch real current posts from Google (site:xiaohongshu.com).

    Returns:
      {
        "verdict": "all_pass | no_posts_found | skipped",
        "posts": [...validated raw posts...],
        "queries_used": [...],
        "grounding_urls": [...actually-cited URLs from Google Search...],
        "_skip_reason": null | "...",
        "_gemini_usage": {...},
      }

    No summary fields. No trend analysis. If the scout couldn't find
    anything usable (all results were analysis blogs, or search was
    blocked), verdict is "no_posts_found" with an explanation in
    _not_found_reason (passed through from the model).
    """
    if not keywords:
        return {
            "verdict": "skipped",
            "posts": [],
            "queries_used": [],
            "grounding_urls": [],
            "_skip_reason": "no keywords provided",
        }

    user_payload = {
        "keywords": keywords,
        "platform": platform,
        "target_count": int(target_count),
    }
    user_message = json.dumps(user_payload, ensure_ascii=False, indent=2)

    try:
        system_prompt = _load_prompt()
    except Exception as e:
        logger.warning("[trend_scout] prompt load failed: %s", e)
        return {
            "verdict": "skipped",
            "posts": [],
            "queries_used": [],
            "grounding_urls": [],
            "_skip_reason": f"prompt_load_failed: {e}",
        }

    try:
        result = call_gemini_json(
            system_prompt,
            user_message,
            enable_search=True,
        )
    except GeminiNotConfigured as e:
        logger.debug("[trend_scout] not configured, skipping: %s", e)
        return {
            "verdict": "skipped",
            "posts": [],
            "queries_used": [],
            "grounding_urls": [],
            "_skip_reason": f"not_configured: {e}",
        }
    except GeminiCallFailed as e:
        logger.warning("[trend_scout] call failed, skipping: %s", e)
        return {
            "verdict": "skipped",
            "posts": [],
            "queries_used": [],
            "grounding_urls": [],
            "_skip_reason": f"call_failed: {e}",
        }

    raw_data = result.get("data")
    if not isinstance(raw_data, dict) or "_parse_error" in raw_data:
        logger.warning(
            "[trend_scout] output parse failed: %s",
            str(raw_data)[:300],
        )
        return {
            "verdict": "skipped",
            "posts": [],
            "queries_used": [],
            "grounding_urls": result.get("grounding_urls", []),
            "_skip_reason": "parse_error",
            "_gemini_usage": {
                "input_tokens": result.get("input_tokens", 0),
                "output_tokens": result.get("output_tokens", 0),
                "cost_usd": result.get("cost_usd", 0.0),
                "model": result.get("model"),
            },
        }

    # STRIP summary-style fields the model may have sneaked in.
    raw_data = _strip_summaries(raw_data)

    raw_posts = raw_data.get("posts")
    if not isinstance(raw_posts, list):
        raw_posts = []

    # Validate each post: must have xiaohongshu.com in URL + title/snippet.
    validated: list[dict] = []
    for p in raw_posts:
        v = _validate_post(p, required_domain=required_domain)
        if v is not None:
            validated.append(v)
    rejected_count = len(raw_posts) - len(validated)

    queries_used = raw_data.get("queries_used")
    if not isinstance(queries_used, list):
        queries_used = []

    not_found_reason = raw_data.get("_not_found_reason")

    verdict = "all_pass" if validated else "no_posts_found"

    usage = {
        "input_tokens": result.get("input_tokens", 0),
        "output_tokens": result.get("output_tokens", 0),
        "cost_usd": result.get("cost_usd", 0.0),
        "model": result.get("model"),
    }

    logger.info(
        "[trend_scout] verdict=%s, kept=%d, rejected=%d (wrong domain/empty), "
        "grounding_urls=%d, cost=$%.4f",
        verdict,
        len(validated),
        rejected_count,
        len(result.get("grounding_urls", [])),
        usage["cost_usd"],
    )

    return {
        "verdict": verdict,
        "posts": validated,
        "queries_used": queries_used,
        "grounding_urls": result.get("grounding_urls", []),
        "_not_found_reason": not_found_reason,
        "_rejected_off_domain_count": rejected_count,
        "_gemini_usage": usage,
    }


def format_trend_intel_for_prompt(scout_result: dict) -> str:
    """Turn scout output into a human/LLM-readable snippet suitable for
    injection into secretariat's input. Deliberately NOT a summary —
    just a numbered list of raw titles + snippets so secretariat's
    prompt sees concrete examples, not abstract trends.
    """
    if not scout_result or scout_result.get("verdict") != "all_pass":
        return ""
    posts = scout_result.get("posts") or []
    if not posts:
        return ""
    lines = [
        f"【当前小红书真实帖子取样（{len(posts)} 条原文，非分析文）】",
        "下面是 Gemini 刚搜到的真实正在流通的帖子，请把它们当作"
        "「目标平台当前语境」的具象参照——不要把它们泛化成规律，"
        "而是看里面具体的人物/场景/第一句话结构，校准你自己策略的味道基准：",
    ]
    for i, p in enumerate(posts, 1):
        title = p.get("title") or "(无标题)"
        snippet = p.get("snippet") or ""
        url = p.get("url", "")
        flag = " [⚠️ 可能是分析文]" if p.get("_suspect_analysis") else ""
        lines.append(f"{i}. 《{title}》{flag}")
        if snippet:
            lines.append(f"   片段：{snippet}")
        if url:
            lines.append(f"   URL：{url}")
    return "\n".join(lines)
