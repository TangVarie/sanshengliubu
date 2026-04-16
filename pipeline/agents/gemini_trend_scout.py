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
    vibe_hints: list[str] | None = None,
    *,
    platform: str = "小红书",
    target_count: int = 10,
    required_domain: str = "xiaohongshu.com",
    # Legacy alias: older callers passed `keywords` as a positional
    # product-brand list. The new scout does NOT search brand terms
    # (that returns software-ads + analysis instead of raw viral posts);
    # keywords=... is accepted only as a loose soft filter and the
    # scout's prompt explicitly tells it never to search the brand.
    keywords: list[str] | None = None,
) -> dict[str, Any]:
    """Pull RAW CURRENT VIRAL Xiaohongshu samples as vibe-calibration
    references for the downstream copy-writing pipeline.

    **Important design note**: this used to search product/brand keywords
    (e.g. "珂润精华液") which returned software ads + analysis articles
    — the opposite of what we need. We want素人真实爆款 format samples:
    the first-sentences and hooks that actually stop scrollers right
    now, regardless of topic. So the prompt instructs Gemini to search
    FORMAT anchors (反差 / 社死 / 身份标签 / "不是广" / reposts on weibo
    etc.) NOT brand terms. The `vibe_hints` param is a soft preference
    for result sorting only — demographic hints like "30 岁女性职场人" to
    help it pick the more relevant viral example among many, never fed
    as a direct search term.

    Returns:
      {"verdict": "all_pass | no_posts_found | skipped",
       "posts": [...validated raw posts, possibly with _suspect_repost=True
                 for weibo/douban etc. that quoted xhs originals...],
       "queries_used": [...],
       "grounding_urls": [...],
       "_skip_reason": null | "...",
       "_gemini_usage": {...}}

    No summary fields ever survive post-processing (see
    _FORBIDDEN_SUMMARY_KEYS + _strip_summaries).
    """
    # Backward compat: if a caller still passes keywords=, map to vibe_hints
    # but drop them into the prompt with the soft-filter-only semantics.
    effective_hints = list(vibe_hints or [])
    if keywords and not effective_hints:
        effective_hints = list(keywords)

    user_payload = {
        "vibe_hints": effective_hints,
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
        # Extract whatever Gemini actually said so the UI can show it.
        # Multiple fallback sources because the parse failure mode keeps
        # biting us in unexpected ways:
        #   1. raw_data["_raw_text"] — what _extract_json populates on
        #      failure (primary path)
        #   2. raw_data["_raw_text_debug"] — populated on SUCCESS too now
        #      (shouldn't be hit here but defensive)
        #   3. str(raw_data) — last-resort dump of whatever dict we got
        _preview = ""
        if isinstance(raw_data, dict):
            _preview = (
                str(raw_data.get("_raw_text") or "")
                or str(raw_data.get("_raw_text_debug") or "")
                or str(raw_data)
            )[:500]
        elif raw_data is not None:
            _preview = str(raw_data)[:500]
        logger.warning(
            "[trend_scout] output parse failed. Gemini raw output preview: %s",
            _preview[:500] or "(no preview — raw_data was %r)" % (raw_data,),
        )
        return {
            "verdict": "skipped",
            "posts": [],
            "queries_used": [],
            "grounding_urls": result.get("grounding_urls", []),
            "_skip_reason": "parse_error",
            "_raw_text_preview": _preview,
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
        f"【当前小红书爆款钩子模式取样（{len(posts)} 条，来自 Gemini 搜索）】",
        "下面是 Gemini 刚搜到的真实正在流通的小红书帖子的**钩子模式描述**"
        "（不是原文——Google 版权过滤不允许逐字复制），请把它们当作"
        "「目标平台当前语境」的具象参照——看里面的钩子结构/情绪驱动/叙事模式，"
        "校准你自己策略的味道基准：",
    ]
    for i, p in enumerate(posts, 1):
        title = p.get("title") or "(无描述)"
        snippet = p.get("snippet") or ""
        url = p.get("url", "")
        flag = " [⚠️ 可能是分析文]" if p.get("_suspect_analysis") else ""
        lines.append(f"{i}. {title}{flag}")
        if snippet:
            lines.append(f"   钩子模式：{snippet}")
        if url:
            lines.append(f"   URL：{url}")
    return "\n".join(lines)
