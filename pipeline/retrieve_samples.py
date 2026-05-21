"""Reference-pack retrieval for the vibe loop.

Called from orchestrator._run_vibe_loop() to fetch relevant human-curated
证据包 by (platform, category) and shape them into a rewriter-friendly
payload. The heavy lifting(上游分析) was already done by
pipeline/agents/reference_pack_analyzer.py at ingest time — at retrieve
time we only filter + reshape.

Design notes:
  - Cross-platform references are deliberately NOT allowed. 小红书 的 vibe
    跟抖音差很远,混着投反而污染生成。
  - We strip cover_image_b64 from the retrieved packs by default — the
    base64 is ~100KB per image, injecting 6 of them would blow the prompt
    budget. cover_analysis.description(文字总结)已经在 ai_analysis 里,
    足够让 rewriter 理解视觉钩子机制。
  - Per-cell retrieval happens in the rewriter batch loop so different
    cells can see different samples (diversity > coverage).
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _shape_for_rewriter(pack: dict) -> dict[str, Any]:
    """Pick just the fields rewriter needs; drop base64 blobs + metadata
    that doesn't inform imitation. Keeps the injected payload ≤ 5KB/pack."""
    return {
        "id": pack.get("id"),
        "platform": pack.get("platform"),
        "category": pack.get("category"),
        "post_title": pack.get("post_title"),
        "post_body": pack.get("post_body"),
        "top_comments": pack.get("top_comments") or [],
        "ai_analysis": pack.get("ai_analysis") or {},
        # cover_image_b64 intentionally dropped — see module docstring
    }


def retrieve_reference_packs(
    db,
    *,
    platform: str,
    category: str | None = None,
    limit: int = 6,
) -> list[dict[str, Any]]:
    """Fetch up to `limit` packs matching (platform, category).

    Silent fallback: if DB errors out, return [] and log. vibe_loop should
    continue with no references rather than fail the run — human curation
    of the sample library is optional infrastructure, not a hard requirement.
    """
    if not platform:
        return []
    try:
        raw = db.get_relevant_reference_packs(
            platform=platform,
            category=category,
            limit=limit,
        )
    except Exception:
        logger.exception(
            "[retrieve_samples] query failed for platform=%r category=%r; "
            "proceeding without references",
            platform,
            category,
        )
        return []
    packs = [_shape_for_rewriter(p) for p in raw]
    if packs:
        logger.info(
            "[retrieve_samples] matched %d packs for platform=%r category=%r "
            "(exact=%d, platform-only=%d)",
            len(packs),
            platform,
            category,
            sum(1 for p in packs if p.get("category") == category),
            sum(1 for p in packs if p.get("category") != category),
        )
    else:
        logger.info(
            "[retrieve_samples] no packs found for platform=%r — "
            "vibe loop will run without human references",
            platform,
        )
    return packs
