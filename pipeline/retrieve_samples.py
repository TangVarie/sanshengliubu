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
    that doesn't inform imitation. Keeps the injected payload ≤ 5KB/pack.

    `source_type` is computed from `source_truth_vault_note_id`:
      - "truth_vault" → TV 飞轮自动同步的爆款(权威)
      - "manual" → 用户在 reference_library 页面手动录入
    rewriter 用这个字段在 rewrite_summary 里追溯锚点来源,便于审计
    "飞轮飞起来了吗 / 还是只有手工样本在撑"。"""
    source_type = (
        "truth_vault"
        if pack.get("source_truth_vault_note_id")
        else "manual"
    )
    return {
        "id": pack.get("id"),
        "platform": pack.get("platform"),
        "category": pack.get("category"),
        "post_title": pack.get("post_title"),
        "post_body": pack.get("post_body"),
        "top_comments": pack.get("top_comments") or [],
        "ai_analysis": pack.get("ai_analysis") or {},
        "source_type": source_type,
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
        _tv_count = sum(1 for p in packs if p.get("source_type") == "truth_vault")
        _manual_count = len(packs) - _tv_count
        logger.info(
            "[retrieve_samples] matched %d packs for platform=%r category=%r "
            "(exact=%d, platform-only=%d, tv_synced=%d, manual=%d)",
            len(packs),
            platform,
            category,
            sum(1 for p in packs if p.get("category") == category),
            sum(1 for p in packs if p.get("category") != category),
            _tv_count,
            _manual_count,
        )
    else:
        # R-022: 这条 warning(不是 info)是给运营看的飞轮健康信号。
        # 0 命中说明:① TV 飞轮没把这个平台的爆款 sync 进来,
        # ② 用户没在 reference_library 录,③ category 不匹配。
        # 任一情况下,vibe_rewriter 都只能退回静态兜底样本,
        # 飞轮数据没回流——这是 R-022 audit 想暴露的核心问题。
        logger.warning(
            "[retrieve_samples] 0 packs for platform=%r category=%r — "
            "vibe loop will fall back to static samples. "
            "Check: (1) reference_samples table for this platform, "
            "(2) truth-vault sync job, (3) category mismatch.",
            platform,
            category,
        )
    return packs


def summarize_packs_by_platform(
    packs_by_platform: dict[str, list[dict[str, Any]]],
    requested_platforms: list[str],
) -> dict[str, Any]:
    """Build a compact summary that gets injected alongside reference_packs
    so the rewriter can see at a glance: total / per-platform hit / which
    platforms had 0 DB samples and need fallback. Used in vibe_rewriter.md
    §`reference_packs_summary` so the LLM can decide PRIMARY vs FALLBACK
    sourcing per-cell.

    Returns shape:
        {
          "total_packs": int,
          "platforms_hit": {"小红书": 4, "抖音": 2},
          "platforms_missed": ["B站"],
          "tv_synced_total": int,   # of total, how many came from飞轮
          "manual_total": int,      # of total, how many are手工录入
        }
    """
    hit: dict[str, int] = {}
    missed: list[str] = []
    tv_count = 0
    manual_count = 0
    for plat in requested_platforms:
        plat_packs = packs_by_platform.get(plat) or []
        if plat_packs:
            hit[plat] = len(plat_packs)
            for p in plat_packs:
                if p.get("source_type") == "truth_vault":
                    tv_count += 1
                else:
                    manual_count += 1
        else:
            missed.append(plat)
    return {
        "total_packs": tv_count + manual_count,
        "platforms_hit": hit,
        "platforms_missed": missed,
        "tv_synced_total": tv_count,
        "manual_total": manual_count,
    }
