"""Kimi second-opinion vibe critic.

Runs ONLY on cells 主链路的 vibe_critic passed, to catch AI-tone content
that the main-chain critic tends to give face-saving "borderline but pass" scores to.
Uses the same vibe_critic.md system prompt so judgment criteria are
aligned — the only difference is which model does the judging.

Failure semantics (advisory-only):
  - If the Kimi client isn't configured → return empty result, log at
    debug. Pipeline proceeds on the main-chain verdict alone.
  - If Kimi call fails (network, quota, model error) → return empty
    result, log at warning level. Pipeline proceeds.
  - If Kimi returns malformed JSON → return empty result, log warning.

Result shape mirrors vibe_critic.md's output contract: the
orchestrator merges 二审的 `failed_cells` 和主判的,so rewriter
gets a single combined list.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from pipeline.agents.kimi_client import (
    KimiCallFailed,
    KimiNotConfigured,
    call_kimi_json,
    resolve_assist_model,
)

logger = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).parent.parent / "prompts"


def _load_prompt() -> str:
    """Reuse vibe_critic.md verbatim. 主判和二审按同一份标准判,
    same criteria; only the execution model differs. Any improvement
    to the critic prompt automatically benefits both.
    """
    path = _PROMPTS_DIR / "vibe_critic.md"
    return path.read_text(encoding="utf-8")


async def run_kimi_critic(
    prompt_cells: list[dict],
) -> dict[str, Any]:
    """Second-opinion critic over a set of cells (typically those the main-chain critic
    passed, since the arbitration mode B calls us only for those).

    Returns a dict matching vibe_critic's output shape:
      {"verdict": "all_pass | some_failed",
       "failed_cells": [...],
       "cell_reviews": [...],
       "cross_cell_duplicates": [...]}

    On any failure (not configured / call error / parse error) returns
    an advisory-skipped result:
      {"verdict": "skipped",
       "failed_cells": [],
       "_skip_reason": "..."}
    """
    if not prompt_cells:
        return {"verdict": "all_pass", "failed_cells": []}

    # Build the slim input shape 主链路 vibe_critic expects — the prompt
    # file refers to prompt_cells.[*].cell_id, system_prompt, demo_output.
    user_payload = {
        "prompt_cells": [
            {
                "cell_id": c.get("cell_id"),
                "direction_id": c.get("direction_id"),
                "direction_name": c.get("direction_name"),
                "platform": c.get("platform"),
                "system_prompt": c.get("system_prompt", ""),
                "demo_output": c.get("demo_output", ""),
            }
            for c in prompt_cells
        ]
    }
    user_message = json.dumps(user_payload, ensure_ascii=False, indent=2)

    try:
        system_prompt = _load_prompt()
    except Exception as e:
        logger.warning(
            "[kimi_critic] failed to load vibe_critic.md, skipping: %s", e
        )
        return {
            "verdict": "skipped",
            "failed_cells": [],
            "_skip_reason": f"prompt_load_failed: {e}",
        }

    try:
        result = call_kimi_json(
            system_prompt,
            user_message,
            model=resolve_assist_model("critic"),
        )
    except KimiNotConfigured as e:
        logger.debug("[kimi_critic] not configured, skipping: %s", e)
        return {
            "verdict": "skipped",
            "failed_cells": [],
            "_skip_reason": f"not_configured: {e}",
        }
    except KimiCallFailed as e:
        logger.warning("[kimi_critic] call failed, skipping: %s", e)
        return {
            "verdict": "skipped",
            "failed_cells": [],
            "_skip_reason": f"call_failed: {e}",
        }

    parsed = result.get("data")
    if not isinstance(parsed, dict) or "_parse_error" in parsed:
        logger.warning(
            "[kimi_critic] output was not valid JSON, skipping. Raw: %s",
            str(parsed)[:300],
        )
        return {
            "verdict": "skipped",
            "failed_cells": [],
            "_skip_reason": "parse_error",
            "_gemini_usage": {
                "input_tokens": result.get("input_tokens", 0),
                "output_tokens": result.get("output_tokens", 0),
                "cost_usd": result.get("cost_usd", 0.0),
                "model": result.get("model"),
            },
        }

    # Normalize — even if Kimi followed the contract, verify critical
    # fields exist so the orchestrator's arbitration logic doesn't KeyError.
    parsed.setdefault("verdict", "unknown")
    parsed.setdefault("failed_cells", [])
    parsed.setdefault("cell_reviews", [])
    parsed.setdefault("cross_cell_duplicates", [])

    # Validate list types for critical fields — Kimi occasionally returns
    # strings instead of lists, which would crash downstream iteration.
    for _list_key in ("failed_cells", "cell_reviews", "cross_cell_duplicates"):
        if not isinstance(parsed.get(_list_key), list):
            logger.warning(
                "[kimi_critic] expected list for '%s', got %s — defaulting to []",
                _list_key,
                type(parsed.get(_list_key)).__name__,
            )
            parsed[_list_key] = []

    # Attach observability data so the orchestrator can log it + push
    # Kimi's token/cost into the pipeline_run totals.
    # NOTE: 键名 `_gemini_usage` 是历史遗留的【跨模块契约】,不是漏改的。
    # orchestrator (pipeline/orchestrator.py) 和 pages/3 都按这个名字读,
    # DB 里已落库的 stage_log.output_data 也是这个名字 —— 改名会让历史 run
    # 的用量回显变空。socialdatax_trend_scout.py 迁移时做过同样的决定。
    # 换成 Kimi 之后它的语义是"辅助层用量",跟具体哪家无关。
    parsed["_gemini_usage"] = {
        "input_tokens": result.get("input_tokens", 0),
        "output_tokens": result.get("output_tokens", 0),
        "cost_usd": result.get("cost_usd", 0.0),
        "model": result.get("model"),
    }

    logger.info(
        "[kimi_critic] verdict=%s, failed=%d/%d, cost=$%.4f, tokens=%d/%d",
        parsed.get("verdict"),
        len(parsed.get("failed_cells") or []),
        len(prompt_cells),
        result["cost_usd"],
        result["input_tokens"],
        result["output_tokens"],
    )
    return parsed
