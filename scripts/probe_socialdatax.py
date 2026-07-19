#!/usr/bin/env python3
"""Probe / validate the SocialDataX trend-scout integration.

Modes:

  * offline (default): exercises the pure normalization + formatting
    functions against a synthetic payload. No API key, no network. Fails
    (non-zero exit) if the mapping regresses.

  * live (when ``SOCIALDATAX_API_KEY`` is set in the environment): makes
    ONE search call on the requested platform, dumps the raw response
    field names, then reuses that SAME payload to exercise the
    normalization pipeline locally — one billable call total. Use this to
    PIN the defensive field mapping in socialdatax_trend_scout.py.

  * ``--e2e``: additionally runs the full ``run_trend_scout`` path (a
    second billable call) to verify the end-to-end scout behavior.

Usage::

    python scripts/probe_socialdatax.py
    SOCIALDATAX_API_KEY=sk-... python scripts/probe_socialdatax.py --keyword 露营
    SOCIALDATAX_API_KEY=sk-... python scripts/probe_socialdatax.py --platform 抖音 --e2e

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
    _KEY_NAMES,
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


def run_offline() -> None:
    print("=== OFFLINE self-test (no network) ===")

    # _parse_count
    assert scout._parse_count("1.2万") == 12000
    assert scout._parse_count("10万+") == 100000
    assert scout._parse_count("1,024") == 1024
    assert scout._parse_count(512) == 512
    print("  _parse_count …………………… OK")

    # _find_note_list finds notes under a container key
    payload = {"data": {"notes": [_SYNTHETIC_NOTE]}}
    assert len(scout._find_note_list(payload)) == 1
    print("  _find_note_list ………………… OK")

    # hot-list keyword extraction accepts word-only entries anywhere
    hot = {"data": {"hot_list": [{"word": "早八穿搭"}, {"word": "春日野餐"}]}}
    assert scout._extract_hot_keywords(hot) == ["早八穿搭", "春日野餐"]
    print("  _extract_hot_keywords ………… OK")

    # _normalize_note maps every field
    post = scout._normalize_note(_SYNTHETIC_NOTE, "xhs")
    assert post is not None
    assert post["note_id"] == _SYNTHETIC_NOTE["note_id"]
    assert "xsec_token=ABC123" in post["url"], "xsec_token must be preserved"
    assert post["engagement"]["liked"] == 12000
    assert post["author"] == "露营小张"
    assert "赞1.2万" in post["engagement_display"]
    print("  _normalize_note ………………… OK")
    print("    → normalized:", json.dumps(post, ensure_ascii=False))

    # format block only emits on verdict=all_pass
    block = scout.format_trend_intel_for_prompt(
        {"verdict": "all_pass", "posts": [post],
         "_engagement_resolved": True}
    )
    assert "真实爆款取样" in block
    assert "xsec_token=ABC123" in block
    assert scout.format_trend_intel_for_prompt({"verdict": "skipped"}) == ""
    print("  format_trend_intel_for_prompt … OK")
    print("\n--- formatted block preview ---")
    print(block)
    print("\nOFFLINE self-test PASSED\n")


async def run_live(keyword: str, platform: str, e2e: bool) -> None:
    pid = resolve_platform_id(platform)
    if pid is None or pid not in scout._SEARCH_SPEC:
        print(f"platform {platform!r} not supported by SocialDataX",
              file=sys.stderr)
        raise SystemExit(2)
    spec = scout._SEARCH_SPEC[pid]
    args = scout._build_search_arguments(pid, keyword, spec)
    print(f"=== LIVE call: {pid}/{spec['tool']} args={args} ===")

    # ONE billable call — raw shape dump AND normalization run on the
    # same payload.
    raw = await call_tool(pid, spec["tool"], args)
    print("\n--- raw structuredContent (top-level keys) ---")
    if isinstance(raw, dict):
        print(sorted(raw.keys()))
    notes = scout._find_note_list(raw)
    print(f"  found {len(notes)} note dicts")
    if notes:
        print("  first note raw keys:", sorted(notes[0].keys()))
        print("  first note (raw):",
              json.dumps(notes[0], ensure_ascii=False)[:1200])

    normalized = [
        p for p in (scout._normalize_note(n, pid) for n in notes) if p
    ]
    resolved = sum(1 for p in normalized if scout._engagement_score(p) > 0)
    print(f"\n--- normalized {len(normalized)} posts "
          f"({resolved} with engagement resolved) ---")
    for p in normalized[:3]:
        print(" ", json.dumps(p, ensure_ascii=False)[:400])
    if normalized and resolved == 0:
        print("\n⚠️ engagement counts did NOT resolve — compare the raw "
              "keys above against the _LIKED_KEYS/_COLLECT_KEYS/... tuples "
              "in socialdatax_trend_scout.py and pin them.")

    if e2e:
        print("\n=== E2E run_trend_scout (second billable call) ===")
        result = await scout.run_trend_scout(
            vibe_hints=[keyword], platform=platform
        )
        print("  verdict:", result.get("verdict"))
        print("  posts kept:", len(result.get("posts") or []))
        print("  engagement_resolved:", result.get("_engagement_resolved"))
        print("  usage:", result.get("_gemini_usage"))
        block = scout.format_trend_intel_for_prompt(result)
        print("\n--- formatted block ---")
        print(block or "(empty)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--keyword", default="露营", help="live search keyword")
    ap.add_argument("--platform", default="小红书", help="platform label")
    ap.add_argument(
        "--e2e",
        action="store_true",
        help="also run the full run_trend_scout path (extra billable call)",
    )
    args = ap.parse_args()

    try:
        run_offline()
    except AssertionError as e:
        print(f"OFFLINE self-test FAILED: {e}", file=sys.stderr)
        return 1

    if any(os.environ.get(name) for name in _KEY_NAMES):
        try:
            asyncio.run(run_live(args.keyword, args.platform, args.e2e))
        except SystemExit:
            raise
        except Exception as e:
            print(f"LIVE call failed: {e}", file=sys.stderr)
            return 2
    else:
        print(
            f"(No {' / '.join(_KEY_NAMES)} in env → skipped live call. "
            f"Set one to dump the real response shape and pin the field "
            f"mapping.)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
