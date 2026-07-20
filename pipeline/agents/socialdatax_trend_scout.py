"""SocialDataX 小红书原文取样官 — first-party structured post sampling.

Drop-in replacement for the retired Gemini/Google-grounding trend scout.
Exposes the same public surface (``run_trend_scout`` +
``format_trend_intel_for_prompt``) and the same return contract, so the
orchestrator wiring, stage logs, and downstream ``brief['_trend_intel']``
consumers are unchanged. Only the data source changes: Google-Search-
grounded Gemini → SocialDataX MCP.

What gets better vs the Gemini path
-----------------------------------
  * Real notes, not Google's thin XHS index — we hit XHS directly.
  * Real 原文 (title + desc), not copyright-filtered "hook descriptions".
  * Real 互动量 (赞/藏/评) — so we can rank by actual 爆款, and we search
    the product *category* (topical + viral) instead of the old
    format-anchor workaround that existed only because Google couldn't
    rank XHS content.

Return contract (superset of the old gemini scout's):
    {"verdict": "all_pass" | "no_posts_found" | "skipped",
     "posts": [ {url,title,snippet,cover_image_url,note_id,author,
                 engagement,engagement_display} ],
     "queries_used": [...],
     "grounding_urls": [...],          # source note_urls (repurposed)
     "_skip_reason": None | "...",
     "_not_found_reason": None | "...",
     "_partial_failures": [...],       # calls that failed after posts came in
     "_engagement_resolved": bool,     # False → counts didn't map, ranking off
     "_raw_keys_sample": [...],        # first raw note's keys, for pinning
     "_gemini_usage": {...}}           # key kept for orchestrator compat

Failure semantics: this module only reports (verdict skipped /
no_posts_found). Whether that blocks the run is the ORCHESTRATOR's policy
(SOCIALDATAX_TREND_SCOUT_PRE_REQUIRED — see pipeline/config.py).
A call failure AFTER some posts were already gathered keeps the partial
posts (verdict all_pass + _partial_failures) instead of discarding them.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from pipeline.agents.socialdatax_client import (
    SocialDataXCallFailed,
    SocialDataXNotConfigured,
    open_session,
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
# deliberately tolerant. Every scout result carries `_raw_keys_sample`
# (the first raw note's keys) into the stage log so the mapping can be
# pinned against a real response from the UI — see scripts/
# probe_socialdatax.py for the offline/live pinning workflow.
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
# Hot-search list items are keyword entries, not notes — they may carry
# only a bare topic string under one of these keys.
_HOT_WORD_KEYS = ("word", "name", "keyword", "query", "title")

_SNIPPET_MAX = 220


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


def _looks_like_note(d: dict) -> bool:
    """A dict is note-like only if it carries actual content signal
    (title/desc/url) — a bare id is not enough, which keeps sibling
    metadata lists (suggestions, pagination cards) out."""
    return bool(
        _first(d, _TITLE_KEYS) or _first(d, _DESC_KEYS)
        or _first(d, _URL_KEYS)
    )


# Container keys checked first at each level, in order, before generic
# values — gives well-known shapes a deterministic priority.
_CONTAINER_KEYS = ("notes", "note_list", "noteList", "items", "results",
                   "list", "videos", "video_list", "posts", "data",
                   "records")
_MAX_WALK_DEPTH = 4


def _find_note_list(payload: Any) -> list[dict]:
    """Locate the list of note dicts inside an unknown structuredContent
    shape: one bounded recursive walk that at each level tries the known
    container keys first, then any other value, accepting the first
    list-of-dicts whose members look note-like (see _looks_like_note)."""
    def _walk(obj: Any, depth: int) -> list[dict]:
        if depth > _MAX_WALK_DEPTH:
            return []
        if isinstance(obj, list):
            cand = [x for x in obj if isinstance(x, dict)]
            if cand and any(_looks_like_note(c) for c in cand):
                return cand
            return []
        if not isinstance(obj, dict):
            return []
        ordered = [obj[k] for k in _CONTAINER_KEYS if k in obj]
        ordered += [v for k, v in obj.items() if k not in _CONTAINER_KEYS]
        for val in ordered:
            got = _walk(val, depth + 1)
            if got:
                return got
        return []

    return _walk(payload, 0)


def _extract_hot_keywords(payload: Any, limit: int = 3) -> list[str]:
    """Pull topic strings out of a hot-search-list payload. Hot items are
    keyword entries ({'word': ...} / {'name': ...}), NOT notes, so this
    deliberately does not reuse the note-shaped heuristics."""
    words: list[str] = []

    def _walk(obj: Any, depth: int) -> None:
        if len(words) >= limit or depth > _MAX_WALK_DEPTH:
            return
        if isinstance(obj, list):
            for item in obj:
                if len(words) >= limit:
                    return
                if isinstance(item, str) and item.strip():
                    words.append(item.strip())
                elif isinstance(item, dict):
                    w = _first(item, _HOT_WORD_KEYS)
                    if isinstance(w, str) and w.strip():
                        words.append(w.strip())
        elif isinstance(obj, dict):
            for val in obj.values():
                _walk(val, depth + 1)

    _walk(payload, 0)
    # de-dup, preserve order
    seen: set[str] = set()
    out = []
    for w in words:
        if w not in seen:
            seen.add(w)
            out.append(w)
    return out[:limit]


def _engagement_score(post: dict) -> int:
    """Ranking key: likes dominate, collects/comments contribute. Derived
    from the stored engagement counts — deliberately NOT persisted on the
    post so the formula stays tweakable."""
    eng = post.get("engagement") or {}
    return (
        int(eng.get("liked", 0))
        + int(eng.get("collected", 0))
        + int(eng.get("comment", 0)) * 2
    )


def _normalize_note(raw: Any, platform_id: str) -> dict | None:
    """Trim one raw note into the downstream post schema. Returns None for
    notes carrying no usable signal (no title, no desc, no url)."""
    if not isinstance(raw, dict):
        return None

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
    plumbing (the `_gemini_usage` key name is a kept compat contract; the
    `model` field identifies the real source). SocialDataX bills per API
    call; cost = calls * configured per-call price (0 → not tracked)."""
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
        "_partial_failures": [],
        "_engagement_resolved": False,
        "_raw_keys_sample": [],
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

    ``vibe_hints`` are ordered SEARCH-KEYWORD candidates (topical terms like
    the product category / audience / direction name), searched in order
    until ``target_count`` posts are gathered. ``keywords`` is a legacy
    alias. With no candidates, falls back to the platform's hot list where
    available (xhs) and searches its top topics.

    All calls in one invocation share ONE MCP session (single handshake).
    A call failing AFTER earlier calls produced posts keeps the partial
    posts and records the failure in ``_partial_failures`` — partial
    calibration data beats none, especially under the REQUIRED policy.

    See module docstring for the return contract.
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

    if not candidates and not spec.get("hot_tool"):
        # Nothing to search and no hot-list fallback on this platform —
        # a configuration/input gap, not a retryable error. Name it
        # precisely so the REQUIRED error message doesn't tell the user
        # to "retry" something deterministic.
        return _skipped(
            f"no_search_keywords: brief 没有可搜索的品类/产品/人群词,"
            f"且 {platform_id} 平台无热榜兜底",
            platform_id,
        )

    posts: list[dict] = []
    queries_used: list[str] = []
    partial_failures: list[str] = []
    raw_keys_sample: list[str] = []
    calls = 0
    seen_posts: set[str] = set()

    try:
        async with open_session(platform_id) as call:
            # No topical keyword → seed candidates from the platform's
            # real hot list (xhs only for now).
            if not candidates:
                calls += 1
                try:
                    hot_payload = await call(spec["hot_tool"], {})
                    candidates = _extract_hot_keywords(hot_payload)
                    queries_used.append(
                        f"{spec['hot_tool']} → {candidates}"
                    )
                except SocialDataXCallFailed as e:
                    # Hot list failing leaves nothing to search.
                    return _skipped(
                        f"call_failed: {e}", platform_id, calls
                    )

            for kw in candidates:
                if len(posts) >= target_count:
                    break
                calls += 1
                args = _build_search_arguments(platform_id, kw, spec)
                try:
                    payload = await call(search_tool, args)
                except SocialDataXCallFailed as e:
                    # Keep partial results from earlier keywords — partial
                    # calibration beats none under the REQUIRED policy.
                    logger.warning(
                        "[socialdatax scout] search %r failed "
                        "(keeping %d posts already gathered): %s",
                        kw,
                        len(posts),
                        e,
                    )
                    partial_failures.append(f"{kw}: {e}")
                    if posts:
                        continue
                    return _skipped(
                        f"call_failed: {e}", platform_id, calls
                    )
                sort_note = args.get("sort_type", "default")
                queries_used.append(
                    f"{kw} ({search_tool}, sort={sort_note})"
                )
                for raw in _find_note_list(payload):
                    if not raw_keys_sample and isinstance(raw, dict):
                        raw_keys_sample = sorted(raw.keys())
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
        # Session-level failure (connect/initialize). Nothing gathered.
        logger.warning("[socialdatax scout] session failed, skipping: %s", e)
        return _skipped(f"call_failed: {e}", platform_id, calls)

    # Rank by real engagement, keep the top target_count. If NO post
    # resolved any engagement count, the field mapping likely missed the
    # real schema — flag it so UI/prompt don't claim engagement ranking.
    engagement_resolved = any(_engagement_score(p) > 0 for p in posts)
    if engagement_resolved:
        posts.sort(key=_engagement_score, reverse=True)
    elif posts:
        logger.warning(
            "[socialdatax scout] no engagement counts resolved on %d posts "
            "(raw keys: %s) — field mapping needs pinning; keeping API "
            "order.",
            len(posts),
            raw_keys_sample,
        )
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
        "keywords=%s, engagement_resolved=%s, partial_failures=%d, "
        "cost=$%.4f",
        verdict,
        len(posts),
        calls,
        candidates,
        engagement_resolved,
        len(partial_failures),
        usage["cost_usd"],
    )

    return {
        "verdict": verdict,
        "posts": posts,
        "queries_used": queries_used,
        "grounding_urls": [p["url"] for p in posts if p.get("url")],
        "_skip_reason": None,
        "_not_found_reason": not_found_reason,
        "_partial_failures": partial_failures,
        "_engagement_resolved": engagement_resolved,
        "_raw_keys_sample": raw_keys_sample,
        "_gemini_usage": usage,
    }


def format_trend_intel_for_prompt(scout_result: dict) -> str:
    """Turn scout output into an LLM-readable calibration block for
    secretariat: real 原文 + real 互动量 (first-party data, no copyright
    filtering). When engagement counts failed to resolve, the header
    honestly drops the "engagement-ranked" claim instead of lying."""
    if not scout_result or scout_result.get("verdict") != "all_pass":
        return ""
    posts = scout_result.get("posts") or []
    if not posts:
        return ""
    ranked = scout_result.get("_engagement_resolved", True)
    if ranked:
        header = (
            f"【当前小红书真实爆款取样({len(posts)} 条,SocialDataX 直连小红书,"
            f"按真实互动量排序)】"
        )
        intro = (
            "下面是刚从小红书拉到的、当前正在流通的真实爆款笔记**原文样本 + "
            "真实互动量**。这是一手数据(不是趋势分析、不是二手转述)——把它当作"
            "「目标平台当前语境」的具象参照:看它们的钩子结构 / 情绪驱动 / "
            "叙事模式,以及什么样的内容真的拿到了高赞高藏,据此校准你自己策略"
            "的味道基准:"
        )
    else:
        header = (
            f"【当前小红书真实笔记取样({len(posts)} 条,SocialDataX 直连小红书,"
            f"平台搜索序)】"
        )
        intro = (
            "下面是刚从小红书拉到的真实笔记**原文样本**(本次互动量字段未解析,"
            "按平台返回顺序展示)。这是一手数据——把它当作「目标平台当前语境」"
            "的具象参照:看它们的钩子结构 / 情绪驱动 / 叙事模式,据此校准你"
            "自己策略的味道基准:"
        )
    lines = [header, intro]
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
