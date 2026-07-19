#!/usr/bin/env python3
"""Probe / validate the SocialDataX trend-scout integration.

Two modes:

  * offline (default): exercises the pure normalization + formatting
    functions against a synthetic payload. No API key, no network. Fails
    (non-zero exit) if the mapping regresses.

  * live: if ``SOCIALDATAX_API_KEY`` is set in the environment, also runs
    one real ``xhs_search_notes`` call and dumps the raw response field
    names + the normalized posts. Use this to PIN the defensive field
    mapping in socialdatax_trend_scout.py against a real response.

Usage::

    python scripts/probe_socialdatax.py
    SOCIALDATAX_API_KEY=sk-... python scripts/probe_socialdatax.py --keyword 露营

Run from the repo root (so ``pipeline`` is importable).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

# Allow running as `python scripts/probe_socialdatax.py` from repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.agents import socialdatax_trend_scout as scout  # noqa: E402
from pipeline.agents.socialdatax_client import (  # noqa: E402
    call_tool,
    resolve_platform_id,
)


# A synthetic XHS-ish note covering the field-name variants + a localized
# "1.2万" count string, to exercise the tolerant normalizer offline.
_SYNTHETIC_NOTE = {
    "note_id": "65f0a1b2c3d4e5f600112233",
    "note_url": "https://www.xiaohongshu.com/explore/65f0a1b2c3d4e5f600112233?xsec_token=ABC123",
    "display_title": "露营新手第一次翻车实录",
    "desc": "本来想拍出氛围感大片，结果风一吹帐篷直接飞了，全程社死……姐妹们一定要买防风的！",
    "liked_count": "1.2万",
    "collected_count": 3400,
    "comment_count": "128",
    "cover": {"url": "https://sns-img.xhscdn.com/abc.jpg"},
    "user": {"nickname": "露营小张"},
}


def run_offline() -> bool:
    print("=== OFFLINE self-test (no network) ===")
    ok = True

    # _parse_count
    assert scout._parse_count("1.2万") == 12000, scout._parse_count("1.2万")
    assert scout._parse_count("10万+") == 100000
    assert scout._parse_count("1,024") == 1024
    assert scout._parse_count(512) == 512
    print("  _parse_count …………………… OK")

    # _find_note_list finds notes under a container key
    payload = {"data": {"notes": [_SYNTHETIC_NOTE]}}
    found = scout._find_note_list(payload)
    assert len(found) == 1, found
    print("  _find_note_list ………………… OK")

    # _normalize_note maps every field
    post = scout._normalize_note(_SYNTHETIC_NOTE, "xhs")
    assert post is not None
    assert post["note_id"] == _SYNTHETIC_NOTE["note_id"]
    assert "xsec_token=ABC123" in post["url"], "xsec_token must be preserved"
    assert post["title"] == "露营新手第一次翻车实录"
    assert post["engagement"]["liked"] == 12000
    assert post["engagement"]["collected"] == 3400
    assert post["author"] == "露营小张"
    assert post["cover_image_url"].endswith("abc.jpg")
    assert "赞1.2万" in post["engagement_display"]
    print("  _normalize_note ………………… OK")
    print("    → normalized:", json.dumps(post, ensure_ascii=False))

    # format block only emits on verdict=all_pass
    block = scout.format_trend_intel_for_prompt(
        {"verdict": "all_pass", "posts": [post]}
    )
    assert "真实爆款取样" in block
    assert "xsec_token=ABC123" in block
    assert scout.format_trend_intel_for_prompt({"verdict": "skipped"}) == ""
    print("  format_trend_intel_for_prompt … OK")
    print("\n--- formatted block preview ---")
    print(block)

    print("\nOFFLINE self-test PASSED\n")
    return ok


async def run_live(keyword: str, platform: str) -> None:
    print(f"=== LIVE call: platform={platform} keyword={keyword!r} ===")
    pid = resolve_platform_id(platform) or "xhs"

    # 1) Raw structuredContent — reveals the true field names.
    raw = await call_tool(
        pid, "xhs_search_notes", {"keyword": keyword, "sort_type": "like_count_descending"}
    )
    print("\n--- raw structuredContent (top-level keys) ---")
    if isinstance(raw, dict):
        print(sorted(raw.keys()))
    notes = scout._find_note_list(raw)
    print(f"  found {len(notes)} note dicts")
    if notes:
        print("  first note raw keys:", sorted(notes[0].keys()))
        print("  first note (raw):",
              json.dumps(notes[0], ensure_ascii=False)[:1200])

    # 2) End-to-end scout → normalized posts + formatted block.
    result = await scout.run_trend_scout(vibe_hints=[keyword], platform=platform)
    print("\n--- run_trend_scout result ---")
    print("  verdict:", result.get("verdict"))
    print("  posts kept:", len(result.get("posts") or []))
    print("  usage:", result.get("_gemini_usage"))
    block = scout.format_trend_intel_for_prompt(result)
    print("\n--- formatted block ---")
    print(block or "(empty)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--keyword", default="露营", help="live search keyword")
    ap.add_argument("--platform", default="小红书", help="platform label")
    args = ap.parse_args()

    try:
        run_offline()
    except AssertionError as e:
        print(f"OFFLINE self-test FAILED: {e}", file=sys.stderr)
        return 1

    if os.environ.get("SOCIALDATAX_API_KEY") or os.environ.get(
        "SOCIAL_MEDIA_MCP_API_KEY"
    ):
        try:
            asyncio.run(run_live(args.keyword, args.platform))
        except Exception as e:
            print(f"LIVE call failed: {e}", file=sys.stderr)
            return 2
    else:
        print(
            "(No SOCIALDATAX_API_KEY in env → skipped live call. Set it to "
            "dump the real response shape and pin the field mapping.)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
