"""Kimi screenshot analyzer — multimodal OCR + hook structure analysis.

Takes uploaded images (小红书 screenshots) and uses Kimi Vision to
extract text + analyze hook patterns. This is the most RELIABLE way
to get real xiaohongshu content into the pipeline because:
  - No auth/login wall issues (user already has the screenshot)
  - No web scraping / ToS violations
  - Kimi Vision is excellent at Chinese text OCR
  - User curates exactly what they want to reference (high signal)

Advisory-only failure semantics, same as all Kimi agents.
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
    path = _PROMPTS_DIR / "kimi_screenshot_analyzer.md"
    return path.read_text(encoding="utf-8")


async def analyze_screenshots(
    images: list[tuple[bytes, str]],
    supplementary_text: str = "",
) -> dict[str, Any]:
    """Analyze 1-10 uploaded screenshots via Kimi Vision.

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
        result = call_kimi_json(
            system_prompt,
            user_msg,
            images=images,
            model=resolve_assist_model("screenshot_analyzer"),
        )
    except KimiNotConfigured as e:
        logger.debug("[screenshot_analyzer] not configured: %s", e)
        return {
            "verdict": "skipped",
            "screenshots": [],
            "_skip_reason": f"not_configured: {e}",
        }
    except KimiCallFailed as e:
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
        f"【用户上传的参考爆文截图分析（{len(screenshots)} 张，Kimi Vision 提取）】",
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
