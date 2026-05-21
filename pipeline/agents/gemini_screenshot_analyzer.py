"""Gemini screenshot analyzer — multimodal OCR + hook structure analysis.

Takes uploaded images (小红书 screenshots) and uses Gemini Vision to
extract text + analyze hook patterns. This is the most RELIABLE way
to get real xiaohongshu content into the pipeline because:
  - No auth/login wall issues (user already has the screenshot)
  - No web scraping / ToS violations
  - Gemini Vision is excellent at Chinese text OCR
  - User curates exactly what they want to reference (high signal)

Advisory-only failure semantics, same as all Gemini agents.
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
    path = _PROMPTS_DIR / "gemini_screenshot_analyzer.md"
    return path.read_text(encoding="utf-8")


async def analyze_screenshots(
    images: list[tuple[bytes, str]],
    supplementary_text: str = "",
) -> dict[str, Any]:
    """Analyze 1-10 uploaded screenshots via Gemini Vision.

    Args:
        images: list of (raw_bytes, mime_type) tuples
        supplementary_text: optional user-provided context / copy-pasted
                           text from the xhs app

    Returns:
        {
          "verdict": "ok | skipped",
          "screenshots": [...per-screenshot analysis...],
          "cross_screenshot_patterns": "...",
          "_skip_reason": null | "...",
          "_gemini_usage": {...},
        }
    """
    if not images:
        return {
            "verdict": "skipped",
            "screenshots": [],
            "_skip_reason": "no images provided",
        }

    try:
        system_prompt = _load_prompt()
    except Exception as e:
        logger.warning("[screenshot_analyzer] prompt load failed: %s", e)
        return {
            "verdict": "skipped",
            "screenshots": [],
            "_skip_reason": f"prompt_load_failed: {e}",
        }

    user_msg = f"请分析以下 {len(images)} 张截图。"
    if supplementary_text:
        user_msg += f"\n\n用户补充说明：\n{supplementary_text}"

    try:
        result = call_gemini_json(
            system_prompt,
            user_msg,
            images=images,
            model=resolve_gemini_model("screenshot_analyzer"),
        )
    except GeminiNotConfigured as e:
        logger.debug("[screenshot_analyzer] not configured: %s", e)
        return {
            "verdict": "skipped",
            "screenshots": [],
            "_skip_reason": f"not_configured: {e}",
        }
    except GeminiCallFailed as e:
        logger.warning("[screenshot_analyzer] call failed: %s", e)
        return {
            "verdict": "skipped",
            "screenshots": [],
            "_skip_reason": f"call_failed: {e}",
        }

    parsed = result.get("data")
    if not isinstance(parsed, dict) or "_parse_error" in parsed:
        _preview = ""
        if isinstance(parsed, dict):
            _preview = str(parsed.get("_raw_text") or parsed)[:500]
        logger.warning(
            "[screenshot_analyzer] parse failed: %s", _preview[:300]
        )
        return {
            "verdict": "skipped",
            "screenshots": [],
            "_skip_reason": "parse_error",
            "_raw_text_preview": _preview,
        }

    parsed.setdefault("screenshots", [])
    parsed.setdefault("cross_screenshot_patterns", "")
    parsed["_gemini_usage"] = {
        "input_tokens": result.get("input_tokens", 0),
        "output_tokens": result.get("output_tokens", 0),
        "cost_usd": result.get("cost_usd", 0.0),
        "model": result.get("model"),
    }

    logger.info(
        "[screenshot_analyzer] analyzed %d screenshots, cost=$%.4f",
        len(parsed["screenshots"]),
        result.get("cost_usd", 0.0),
    )
    return {"verdict": "ok", **parsed}


def format_analysis_for_brief(analysis: dict) -> str:
    """Turn screenshot analysis into a text block suitable for injection
    into the structured brief. Secretariat and works_builder can
    reference these as concrete calibration samples."""
    if not analysis or analysis.get("verdict") != "ok":
        return ""
    screenshots = analysis.get("screenshots") or []
    if not screenshots:
        return ""
    lines = [
        f"【用户上传的参考爆文截图分析（{len(screenshots)} 张，Gemini Vision 提取）】",
        "下面是用户指定的小红书参考帖子的结构化分析。把这些当作**具体的钩子模板参照**——",
        "不是要复制它们的内容，而是学习它们的情绪驱动、开头切入、叙事结构。",
    ]
    for ss in screenshots:
        idx = ss.get("index", "?")
        title = ss.get("extracted_title", "?")
        first_line = ss.get("extracted_first_line", "")
        hook = ss.get("hook") or {}
        reusable = ss.get("reusable_elements") or []
        lines.append(f"\n📸 截图 #{idx}：{title}")
        if first_line:
            lines.append(f"   第一句：{first_line}")
        if hook:
            lines.append(
                f"   钩子：{hook.get('emotion_driver', '?')} / "
                f"{hook.get('opening_pattern', '?')} / "
                f"{hook.get('narrative_structure', '?')}"
            )
        for r in reusable:
            lines.append(f"   💡 {r}")
    xp = analysis.get("cross_screenshot_patterns", "")
    if xp:
        lines.append(f"\n共性模式：{xp}")
    return "\n".join(lines)
