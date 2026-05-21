"""Gemini structure reviewer — audits works_builder output for completeness.

Runs AFTER _run_works_builders succeeds but BEFORE _run_vibe_loop. The
vibe critic judges content taste; the structure reviewer judges
structural completeness — are the 5 differentiation pools present? Is
there a concrete compliance block? Is the AI-cliché banlist a list of
actual phrases or just "avoid AI tone"? These are the kinds of
shortfalls chancellery_final reliably calls out, and the whole point
of shifting the check left is so the rewriter gets the feedback BEFORE
the final review round-trip.

Advisory semantics (identical to gemini_critic):
  - Not configured / call failed / parse error → return empty result,
    caller proceeds without any revision_directives from Gemini.
  - Pipeline NEVER blocks on a structure-reviewer failure.

Output is consumed by orchestrator to (a) log a stage_log entry for
UI visibility and (b) append advisory hints to project.brief._revision_context
so the vibe_rewriter and a subsequent revision cycle can act on them.
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


def _load_prompt() -> str:
    path = _PROMPTS_DIR / "gemini_structure_reviewer.md"
    return path.read_text(encoding="utf-8")


async def run_gemini_structure_review(
    prompt_cells: list[dict],
) -> dict[str, Any]:
    """Audit a list of prompt_cells for structural completeness.

    Returns a dict:
      {"verdict": "all_pass | some_incomplete | skipped",
       "cells_incomplete": [{"cell_id": "...", "missing_items": [...],
                             "revision_hint": "..."}],
       "cell_reviews": [...],  # full per-cell breakdown for stage_logs
       "_gemini_usage": {"input_tokens": .., "output_tokens": ..,
                         "cost_usd": .., "model": ..}}

    On failure returns {"verdict": "skipped", "_skip_reason": "...",
                        "cells_incomplete": []}.
    """
    if not prompt_cells:
        return {
            "verdict": "all_pass",
            "cells_incomplete": [],
            "cell_reviews": [],
        }

    # Slim payload — reviewer only needs what it can actually inspect.
    # No need to send ministry_digest, user_prompt_template, etc.
    user_payload = {
        "prompt_cells": [
            {
                "cell_id": c.get("cell_id"),
                "direction_id": c.get("direction_id"),
                "direction_name": c.get("direction_name"),
                "platform": c.get("platform"),
                "system_prompt": c.get("system_prompt", ""),
                "demo_output": c.get("demo_output", "")[:500],  # capped for token budget
            }
            for c in prompt_cells
        ]
    }
    user_message = json.dumps(user_payload, ensure_ascii=False, indent=2)

    try:
        system_prompt = _load_prompt()
    except Exception as e:
        logger.warning(
            "[gemini_structure] prompt load failed, skipping: %s", e
        )
        return {
            "verdict": "skipped",
            "cells_incomplete": [],
            "_skip_reason": f"prompt_load_failed: {e}",
        }

    try:
        result = call_gemini_json(
            system_prompt,
            user_message,
            model=resolve_gemini_model("structure_reviewer"),
        )
    except GeminiNotConfigured as e:
        logger.debug("[gemini_structure] not configured, skipping: %s", e)
        return {
            "verdict": "skipped",
            "cells_incomplete": [],
            "_skip_reason": f"not_configured: {e}",
        }
    except GeminiCallFailed as e:
        logger.warning("[gemini_structure] call failed, skipping: %s", e)
        return {
            "verdict": "skipped",
            "cells_incomplete": [],
            "_skip_reason": f"call_failed: {e}",
        }

    parsed = result.get("data")
    if not isinstance(parsed, dict) or "_parse_error" in parsed:
        _preview = ""
        if isinstance(parsed, dict):
            _preview = str(parsed.get("_raw_text", ""))[:500]
        logger.warning(
            "[gemini_structure] output was not valid JSON, skipping. Raw: %s",
            _preview[:300] or "(no preview)",
        )
        return {
            "verdict": "skipped",
            "cells_incomplete": [],
            "_skip_reason": "parse_error",
            "_raw_text_preview": _preview,
            "_gemini_usage": {
                "input_tokens": result.get("input_tokens", 0),
                "output_tokens": result.get("output_tokens", 0),
                "cost_usd": result.get("cost_usd", 0.0),
                "model": result.get("model"),
            },
        }

    parsed.setdefault("verdict", "unknown")
    parsed.setdefault("cells_incomplete", [])
    parsed.setdefault("cell_reviews", [])
    parsed["_gemini_usage"] = {
        "input_tokens": result.get("input_tokens", 0),
        "output_tokens": result.get("output_tokens", 0),
        "cost_usd": result.get("cost_usd", 0.0),
        "model": result.get("model"),
    }

    logger.info(
        "[gemini_structure] verdict=%s, incomplete=%d/%d, cost=$%.4f, tokens=%d/%d",
        parsed.get("verdict"),
        len(parsed.get("cells_incomplete") or []),
        len(prompt_cells),
        result.get("cost_usd", 0.0),
        result.get("input_tokens", 0),
        result.get("output_tokens", 0),
    )
    return parsed


def format_revision_hints(review_result: dict) -> str:
    """Turn a structure review result into a human-readable block that
    can be appended to a rewriter's revision_directives. Returns empty
    string when nothing actionable came back (skipped or all-pass)."""
    if not review_result or review_result.get("verdict") in (
        "all_pass",
        "skipped",
        "unknown",
    ):
        return ""
    incomplete = review_result.get("cells_incomplete") or []
    if not incomplete:
        return ""
    lines = ["【Gemini 结构审提示】下面每条 cell 被识别出结构不全，建议在重写时顺带补上："]
    for c in incomplete:
        cid = c.get("cell_id", "?")
        missing = c.get("missing_items") or []
        hint = c.get("revision_hint") or ""
        lines.append(f"- {cid}: 缺 {missing}")
        if hint:
            lines.append(f"    · 建议：{hint}")
    return "\n".join(lines)
