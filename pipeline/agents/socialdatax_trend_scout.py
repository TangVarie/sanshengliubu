"""SocialDataX 小红书原文取样官 — first-party structured post sampling.

Drop-in replacement for pipeline/agents/gemini_trend_scout.py. Exposes the
SAME public surface (``run_trend_scout`` + ``format_trend_intel_for_prompt``)
and the SAME return contract, so the orchestrator wiring, stage logs, and
downstream ``brief['_trend_intel']`` consumers are unchanged. Only the data
source changes: Google-Search-grounded Gemini → SocialDataX MCP.

What gets better vs the Gemini path
-----------------------------------
  * Real notes, not Google's thin XHS index — we hit XHS directly.
  * Real 原文 (title + desc), not copyright-filtered "hook descriptions".
  * Real 互动量 (赞/藏/评) — so we can rank by actual 爆款, and we search
    the product *category* (topical + viral) instead of the old
    format-anchor workaround that existed only because Google couldn't
    rank XHS content.
  * No summary-stripping / repost-fallback machinery needed — the server
    returns raw structured data, never trend analysis.

Return contract (unchanged from gemini_trend_scout.run_trend_scout):
    {"verdict": "all_pass" | "no_posts_found" | "skipped",
     "posts": [ {url,title,snippet,cover_image_url,_suspect_analysis,
                 _suspect_repost, note_id, author, engagement, ...} ],
     "queries_used": [...],
     "grounding_urls": [...],          # source note_urls (repurposed)
     "_skip_reason": None | "...",
     "_not_found_reason": None | "...",
     "_rejected_off_domain_count": int,
     "_gemini_usage": {...}}           # key kept for orchestrator compat

Advisory-only: not-configured / call-failed / unsupported-platform all
return verdict="skipped"; the pipeline proceeds without trend data.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from pipeline.agents.socialdatax_client import (
    SocialDataXCallFailed,
    SocialDataXNotConfigured,
    call_tool,
    resolve_platform_id,
)
from pipeline.config import (
    SOCIALDATAX_COST_PER_CALL_USD,
    SOCIALDATAX_TREND_SCOUT_SORT,
)

logger = logging.getLogger(__name__)

# Per-platform keyword-search tool + the sort_type values that platform
# actually accepts (verified against socialdatax-skills v0.2.30 cli.mjs —
# they differ per platform, e.g. WeChat Channels has no
# like_count_descending). "default_sort" is each platform's best
# engagement-ranked option, used when the configured sort isn't in the
# platform's allowed set. Keyword argument is `keyword` everywhere.
_SEARCH_SPEC: dict[str, dict[str, Any]] = {
    "xhs": {
        "tool": "xhs_search_notes",
        "hot_tool": "xhs_get_search_hot_list",
        "sorts": ("general", "time_descending", "like_count_descending",
                  "comment_count_descending", "collect_count_descending"),
        "default_sort": "like_count_descending",
    },
    "douyin": {
        "tool": "douyin_search_videos",
        "hot_tool": None,
        "sorts": ("general", "time_descending", "like_count_descending"),
        "default_sort": "like_count_descending",
    },
    "kuaishou": {  # keyword + page_token only — no sort argument
        "tool": "kuaishou_search_videos",
        "hot_tool": None,
        "sorts": (),
        "default_sort": None,
    },
    "weibo": {  # keyword + page_token only — no sort argument
        "tool": "weibo_search_posts",
        "hot_tool": None,
        "sorts": (),
        "default_sort": None,
    },
    "wechat": {
        "tool": "wechat_search_videos",
        "hot_tool": None,
        "sorts": ("all", "time_descending", "collect_count_descending"),
        "default_sort": "collect_count_descending",
    },
}

# Field-name candidates. The exact SocialDataX response field names are not
# documented (the CLI just passes `data` through), so extraction is
# deliberately tolerant. The first successful call logs the raw note keys
# once (see _log_shape_once) so the mapping can be pinned precisely.
_ID_KEYS = ("note_id", "noteId", "id", "item_id", "aweme_id", "photo_id",
            "post_id", "object_id")
_URL_KEYS = ("note_url", "noteUrl", "url", "share_url", "shareUrl",
             "link", "note_link", "share_link")
_TITLE_KEYS = ("title", "display_title", "displayTitle", "note_title",
               "share_title", "desc_title", "caption")
_DESC_KEYS = ("desc", "description", "content", "note_desc", "body", "text",
              "share_text", "abstract")
_COVER_KEYS = ("cover_image_url", "coverImageUrl", "cover_url", "coverUrl",
               "cover", "image", "image_url", "imageUrl", "thumbnail",
               "thumb")
_LIKED_KEYS = ("liked_count", "likedCount", "like_count", "likeCount",
               "likes", "digg_count", "diggCount")
_COLLECT_KEYS = ("collected_count", "collectedCount", "collect_count",
                 "collectCount", "collects", "favorites", "fav_count")
_COMMENT_KEYS = ("comment_count", "commentCount", "comments_count",
                 "commentsCount", "comments")
_SHARE_KEYS = ("share_count", "shareCount", "shared_count", "shares",
               "forward_count")
_AUTHOR_KEYS = ("nickname", "nick_name", "author", "author_name",
                "user_name", "userName")

_SNIPPET_MAX = 220
_shape_logged = False


def _first(d: dict, keys: tuple[str, ...]) -> Any:
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None


def _parse_count(v: Any) -> int:
    """Best-effort numeric from XHS-style counts that may arrive as ints or
    localized strings ('1.2万', '3.4w', '1,024', '10万+')."""
    if isinstance(v, bool):
        return 0
    if isinstance(v, (int, float)):
        return int(v)
    if not isinstance(v, str):
        return 0
    s = v.strip().replace(",", "").replace("+", "")
    if not s:
        return 0
    mult = 1.0
    if s.endswith(("万", "w", "W")):
        mult, s = 10000.0, s[:-1]
    elif s.endswith(("亿",)):
        mult, s = 1e8, s[:-1]
    try:
        return int(float(s) * mult)
    except ValueError:
        return 0


def _clean_snippet(text: str) -> str:
    collapsed = re.sub(r"\s+", " ", str(text)).strip()
    if len(collapsed) > _SNIPPET_MAX:
        return collapsed[:_SNIPPET_MAX].rstrip() + "…"
    return collapsed


def _extract_author(raw: dict) -> str:
    a = _first(raw, _AUTHOR_KEYS)
    if isinstance(a, str):
        return a.strip()
    for parent in ("user", "author", "creator", "note_user"):
        p = raw.get(parent)
        if isinstance(p, dict):
            v = _first(p, _AUTHOR_KEYS)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return ""


def _extract_cover(raw: dict) -> str:
    c = _first(raw, _COVER_KEYS)
    if isinstance(c, str):
        return c.strip()
    if isinstance(c, dict):
        for k in ("url", "url_default", "urlDefault", "src"):
            if isinstance(c.get(k), str) and c[k].strip():
                return c[k].strip()
    imgs = raw.get("images") or raw.get("image_list") or raw.get("imageList")
    if isinstance(imgs, list) and imgs:
        first = imgs[0]
        if isinstance(first, str):
            return first.strip()
        if isinstance(first, dict):
            for k in ("url", "url_default", "src"):
                if isinstance(first.get(k), str) and first[k].strip():
                    return first[k].strip()
    return ""


def _fmt_wan(n: int) -> str:
    if n >= 10000:
        return f"{n / 10000:.1f}".rstrip("0").rstrip(".") + "万"
    return str(n)


def _log_shape_once(raw: dict, platform_id: str) -> None:
    """Log the raw note keys of the very first note we ever see, so the
    defensive field mapping can be pinned against a real response without
    needing an API key in this environment."""
    global _shape_logged
    if _shape_logged:
        return
    _shape_logged = True
    try:
        logger.info(
            "[socialdatax scout] first %s note raw keys: %s",
            platform_id,
            sorted(raw.keys()),
        )
    except Exception:
        pass


def _find_note_list(payload: Any) -> list[dict]:
    """Locate the list of note dicts inside an unknown structuredContent
    shape. Tries well-known container keys, then a bounded recursive search
    for the first list-of-dicts that looks note-like."""
    if payload is None:
        return []
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if not isinstance(payload, dict):
        return []

    known = ("notes", "note_list", "noteList", "items", "results", "list",
             "videos", "video_list", "posts", "data", "records")
    for k in known:
        v = payload.get(k)
        if isinstance(v, list) and any(isinstance(x, dict) for x in v):
            return [x for x in v if isinstance(x, dict)]
        if isinstance(v, dict):
            nested = _find_note_list(v)
            if nested:
                return nested

    # Bounded recursive fallback: first list-of-dicts anywhere shallow.
    def _walk(obj: Any, depth: int) -> list[dict]:
        if depth > 3 or not isinstance(obj, dict):
            return []
        for val in obj.values():
            if isinstance(val, list) and any(
                isinstance(x, dict) for x in val
            ):
                cand = [x for x in val if isinstance(x, dict)]
                # note-like heuristic: has an id/title/desc-ish field
                if cand and any(
                    _first(c, _ID_KEYS) or _first(c, _TITLE_KEYS)
                    or _first(c, _DESC_KEYS)
                    for c in cand
                ):
                    return cand
            if isinstance(val, dict):
                got = _walk(val, depth + 1)
                if got:
                    return got
        return []

    return _walk(payload, 0)


def _normalize_note(raw: Any, platform_id: str) -> dict | None:
    """Trim one raw note into the downstream post schema. Returns None for
    notes carrying no usable signal (no title, no desc, no url)."""
    if not isinstance(raw, dict):
        return None
    _log_shape_once(raw, platform_id)

    note_id = _first(raw, _ID_KEYS)
    note_id = str(note_id).strip() if note_id is not None else ""
    url = _first(raw, _URL_KEYS)
    url = str(url).strip() if url is not None else ""  # preserve xsec_token
    title = _first(raw, _TITLE_KEYS)
    title = str(title).strip() if title is not None else ""
    desc_raw = _first(raw, _DESC_KEYS)
    desc = str(desc_raw).strip() if desc_raw is not None else ""

    if not title and not desc and not url:
        return None

    liked = _parse_count(_first(raw, _LIKED_KEYS))
    collected = _parse_count(_first(raw, _COLLECT_KEYS))
    comment = _parse_count(_first(raw, _COMMENT_KEYS))
    share = _parse_count(_first(raw, _SHARE_KEYS))

    # snippet = real 原文 opening (first-party API → no copyright filter).
    snippet_source = desc or title
    snippet = _clean_snippet(snippet_source)
    if not title:
        title = _clean_snippet(desc)[:40] or "(无标题)"

    parts = []
    if liked:
        parts.append(f"赞{_fmt_wan(liked)}")
    if collected:
        parts.append(f"藏{_fmt_wan(collected)}")
    if comment:
        parts.append(f"评{_fmt_wan(comment)}")
    engagement_display = "·".join(parts)

    # ranking score: likes dominate, collects/comments contribute.
    score = liked + collected + comment * 2

    return {
        "url": url,
        "title": title,
        "snippet": snippet,
        "cover_image_url": _extract_cover(raw),
        "note_id": note_id,
        "author": _extract_author(raw),
        "engagement": {
            "liked": liked,
            "collected": collected,
            "comment": comment,
            "share": share,
        },
        "engagement_display": engagement_display,
        # kept for downstream / UI compatibility with the old contract
        "_suspect_analysis": False,
        "_suspect_repost": False,
        "_score": score,
    }


def _dedupe_key(post: dict) -> str:
    return post.get("note_id") or post.get("url") or post.get("title") or ""


def _build_search_arguments(
    platform_id: str, keyword: str, spec: dict
) -> dict[str, Any]:
    args: dict[str, Any] = {"keyword": keyword}
    allowed = spec.get("sorts") or ()
    if allowed:
        configured = str(SOCIALDATAX_TREND_SCOUT_SORT or "").strip()
        # Use the configured sort only where the platform accepts it;
        # otherwise fall back to that platform's engagement-ranked default
        # (e.g. WeChat Channels has no like_count_descending).
        args["sort_type"] = (
            configured if configured in allowed else spec["default_sort"]
        )
    return args


def _usage(calls: int, platform_id: str) -> dict:
    """Usage record shaped like the Gemini one for orchestrator cost
    plumbing. SocialDataX bills per API call; cost = calls * configured
    per-call price (0 → not tracked)."""
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_usd": round(calls * float(SOCIALDATAX_COST_PER_CALL_USD), 6),
        "model": f"socialdatax/{platform_id}",
        "_api_calls": calls,
    }


def _skipped(reason: str, platform_id: str, calls: int = 0) -> dict:
    return {
        "verdict": "skipped",
        "posts": [],
        "queries_used": [],
        "grounding_urls": [],
        "_skip_reason": reason,
        "_not_found_reason": None,
        "_rejected_off_domain_count": 0,
        "_gemini_usage": _usage(calls, platform_id),
    }


async def run_trend_scout(
    vibe_hints: list[str] | None = None,
    *,
    platform: str = "小红书",
    target_count: int = 10,
    keywords: list[str] | None = None,
) -> dict[str, Any]:
    """Pull real current 爆款 posts for the given platform as vibe-calibration
    references, ranked by actual engagement.

    ``vibe_hints`` are treated as ordered SEARCH-KEYWORD candidates (topical
    terms like the product category / audience / direction name). Unlike the
    old Gemini scout — which never searched product terms because Google
    returned ads + analysis — SocialDataX returns real notes sorted by real
    互动量, so a topical keyword yields relevant *and* viral samples. Each
    candidate is searched in order until ``target_count`` posts are gathered.
    ``keywords`` is a legacy alias for ``vibe_hints``.

    See module docstring for the (unchanged) return contract.
    """
    platform_id = resolve_platform_id(platform)
    if platform_id is None or platform_id not in _SEARCH_SPEC:
        logger.info(
            "[socialdatax scout] unsupported platform=%r, skipping", platform
        )
        return _skipped(f"unsupported_platform: {platform}", "unknown")

    spec = _SEARCH_SPEC[platform_id]
    search_tool = spec["tool"]

    # Ordered, de-duplicated, non-empty keyword candidates.
    raw_hints = list(vibe_hints or []) + list(keywords or [])
    candidates: list[str] = []
    seen_kw: set[str] = set()
    for h in raw_hints:
        if isinstance(h, str) and h.strip() and h.strip() not in seen_kw:
            candidates.append(h.strip())
            seen_kw.add(h.strip())

    posts: list[dict] = []
    queries_used: list[str] = []
    calls = 0
    seen_posts: set[str] = set()

    try:
        # No topical keyword → fall back to the platform's real hot list
        # (xhs only for now) and search its top topics.
        if not candidates and spec.get("hot_tool"):
            calls += 1
            hot_payload = await call_tool(platform_id, spec["hot_tool"], {})
            for item in _find_note_list(hot_payload)[:3]:
                t = _first(item, _TITLE_KEYS) or _first(item, ("word", "name"))
                if isinstance(t, str) and t.strip():
                    candidates.append(t.strip())
            queries_used.append(f"{spec['hot_tool']} → {candidates}")

        for kw in candidates:
            if len(posts) >= target_count:
                break
            calls += 1
            args = _build_search_arguments(platform_id, kw, spec)
            payload = await call_tool(platform_id, search_tool, args)
            sort_note = args.get("sort_type", "default")
            queries_used.append(f"{kw} ({search_tool}, sort={sort_note})")
            for raw in _find_note_list(payload):
                v = _normalize_note(raw, platform_id)
                if v is None:
                    continue
                key = _dedupe_key(v)
                if key and key in seen_posts:
                    continue
                if key:
                    seen_posts.add(key)
                posts.append(v)
    except SocialDataXNotConfigured as e:
        logger.debug("[socialdatax scout] not configured, skipping: %s", e)
        return _skipped(f"not_configured: {e}", platform_id, calls)
    except SocialDataXCallFailed as e:
        logger.warning("[socialdatax scout] call failed, skipping: %s", e)
        return _skipped(f"call_failed: {e}", platform_id, calls)

    # Rank by real engagement, keep the top target_count.
    posts.sort(key=lambda p: p.get("_score", 0), reverse=True)
    posts = posts[:target_count]

    verdict = "all_pass" if posts else "no_posts_found"
    not_found_reason = (
        None
        if posts
        else f"no notes returned for keywords={candidates!r}"
    )
    usage = _usage(calls, platform_id)

    logger.info(
        "[socialdatax scout] verdict=%s, kept=%d, api_calls=%d, "
        "keywords=%s, cost=$%.4f",
        verdict,
        len(posts),
        calls,
        candidates,
        usage["cost_usd"],
    )

    return {
        "verdict": verdict,
        "posts": posts,
        "queries_used": queries_used,
        "grounding_urls": [p["url"] for p in posts if p.get("url")],
        "_not_found_reason": not_found_reason,
        "_rejected_off_domain_count": 0,
        "_gemini_usage": usage,
    }


def format_trend_intel_for_prompt(scout_result: dict) -> str:
    """Turn scout output into an LLM-readable calibration block for
    secretariat. Now carries real 原文 + real 互动量 (SocialDataX is
    first-party, so no copyright filtering and no "hook description only"
    caveat that the Gemini path had)."""
    if not scout_result or scout_result.get("verdict") != "all_pass":
        return ""
    posts = scout_result.get("posts") or []
    if not posts:
        return ""
    lines = [
        f"【当前小红书真实爆款取样（{len(posts)} 条，SocialDataX 直连小红书，"
        f"按真实互动量排序）】",
        "下面是刚从小红书拉到的、当前正在流通的真实爆款笔记**原文样本 + 真实互动量**。"
        "这是一手数据(不是趋势分析、不是二手转述)——把它当作「目标平台当前语境」的"
        "具象参照:看它们的钩子结构 / 情绪驱动 / 叙事模式,以及什么样的内容真的拿到了"
        "高赞高藏,据此校准你自己策略的味道基准:",
    ]
    for i, p in enumerate(posts, 1):
        title = p.get("title") or "(无标题)"
        eng = p.get("engagement_display") or ""
        head = f"{i}. {title}"
        if eng:
            head += f"  [{eng}]"
        lines.append(head)
        snippet = p.get("snippet") or ""
        if snippet and snippet != title:
            lines.append(f"   原文开头：{snippet}")
        url = p.get("url", "")
        if url:
            lines.append(f"   note_url：{url}")
    return "\n".join(lines)
