"""Reference-pack analyzer — 把用户手工录入的"四位一体证据包"结构化成
LLM 友好的分析 JSON,存回 reference_samples.ai_analysis。

用法(从 pages/6_reference_library.py 调):

    from pipeline.agents.reference_pack_analyzer import analyze_reference_pack
    analysis = analyze_reference_pack(pack_dict)

pack_dict = {
    "post_title": "...",
    "post_body": "...",
    "top_comments": [{"text": "...", "likes": 123}, ...],
    "cover_image_b64": "iVBOR..." or None,
    "platform": "小红书",
    "category": "护肤",
}

返回的 analysis 就是 reference_pack_analyzer.md 里约定的 JSON 结构。

和主流水线区别:
  - 不走 BaseAgent.run()(那个需要 run_id / db,本分析不隶属任何 pipeline run)
  - 直接用 _get_client() + messages API 一次性调用
  - 支持视觉:封面图通过 image 块传入(只在 direct/Vertex 且模型是 vision-capable 时有效)
  - 失败降级:抛 ReferencePackAnalysisFailed,调用方自行提示用户重试
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from pipeline.agents import (
    BaseAgent,
    PROMPTS_DIR,
    _api_config,
    get_client_for_model,
    init_api_config,
)
from pipeline.config import MODELS

logger = logging.getLogger(__name__)


class ReferencePackAnalysisFailed(Exception):
    """Raised when reference pack analysis fails (network, parse, empty)."""


_PROMPT_PATH = PROMPTS_DIR / "reference_pack_analyzer.md"


def _load_system_prompt() -> str:
    if not _PROMPT_PATH.exists():
        raise FileNotFoundError(
            f"reference_pack_analyzer.md missing at {_PROMPT_PATH}"
        )
    return _PROMPT_PATH.read_text(encoding="utf-8")


def _build_user_payload(pack: dict) -> dict:
    """Strip base64 cover out of the JSON-serialized text payload so token
    count stays reasonable — the cover goes in a separate image block."""
    return {
        "platform": pack.get("platform") or "unknown",
        "category": pack.get("category") or "unknown",
        "post_title": pack.get("post_title") or "",
        "post_body": pack.get("post_body") or "",
        "top_comments": pack.get("top_comments") or [],
        "_cover_attached": bool(pack.get("cover_image_b64")),
    }


def _guess_image_media_type(b64: str) -> str:
    """Rough media-type sniff based on the first few bytes in base64.
    PNG's first 4 bytes are 89 50 4E 47 → base64 starts with 'iVBOR'.
    JPEG starts FF D8 FF → base64 starts with '/9j/'.
    Fallback: image/png (Anthropic accepts both)."""
    head = (b64 or "")[:8]
    if head.startswith("/9j/"):
        return "image/jpeg"
    if head.startswith("R0lGOD"):
        return "image/gif"
    if head.startswith("UklGR"):
        return "image/webp"
    return "image/png"


def analyze_reference_pack(pack: dict) -> dict[str, Any]:
    """One-shot LLM analysis of a 四位一体证据包.

    Raises ReferencePackAnalysisFailed on any unrecoverable error so the
    calling page can surface a clean error banner to the user.
    """
    if not pack.get("post_title") and not pack.get("post_body"):
        raise ReferencePackAnalysisFailed(
            "证据包至少要有标题或正文,不能两个都空。"
        )

    # Lazy-init API config (the page might be the first thing hit after a
    # cold start / restart — orchestrator.init_api_config hasn't fired yet).
    if not _api_config:
        init_api_config()

    system_prompt = _load_system_prompt()
    user_json = _build_user_payload(pack)
    user_text = (
        "请分析以下参考样本证据包,按 system prompt 的 JSON schema 输出:\n\n"
        + json.dumps(user_json, ensure_ascii=False, indent=2)
    )

    # Assemble the message content: cover image (if any) as an image block,
    # followed by the text block with the structured pack. Anthropic expects
    # image block FIRST for best attention weighting on vision tasks.
    user_content: list[dict] = []
    cover_b64 = (pack.get("cover_image_b64") or "").strip()
    if cover_b64:
        user_content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": _guess_image_media_type(cover_b64),
                "data": cover_b64,
            },
        })
    user_content.append({"type": "text", "text": user_text})

    # Use the analyzer stage's model — fall back to sonnet for cost/latency.
    # This is a one-shot cheap parsing task, not strategy — Sonnet is plenty.
    model = MODELS.get("ministry_works_builder") or "claude-sonnet-4-6"

    # 直接用模块级路由拿 client。此前这里用 BaseAgent.__new__ 造 shim 并给
    # 只读 property `_use_thinking` 赋值,发 API 前必抛 AttributeError,
    # 参考库 AI 分析整体不可用(审计 COR-001)。thinking 本来就按
    # THINKING_STAGES 判定,本分析不在其中,无需任何覆盖。
    client = get_client_for_model(model)

    # Wrap in the shared bounded-backoff retry so a transient 429/5xx/timeout
    # on the relay doesn't drop the pack into the failed bucket on the first
    # blip (the docstring above claimed retry reuse but the call was bare).
    from pipeline.llm_retry import call_with_retry

    try:
        response = call_with_retry(
            lambda: client.messages.create(
                model=model,
                max_tokens=8000,
                system=system_prompt,
                messages=[{"role": "user", "content": user_content}],
            ),
            operation="reference_pack_analyzer",
        )
    except Exception as e:
        logger.exception("[reference_pack_analyzer] API call failed")
        raise ReferencePackAnalysisFailed(
            f"分析调用失败: {type(e).__name__}: {e}"
        ) from e

    # Extract text from response (vision responses can have multiple blocks)
    text_parts = []
    for block in response.content:
        if getattr(block, "type", "") == "text":
            text_parts.append(block.text)
    full_text = "\n".join(text_parts).strip()
    if not full_text:
        raise ReferencePackAnalysisFailed("模型返回空内容。")

    try:
        analysis = BaseAgent._extract_json(full_text)
    except Exception as e:
        logger.warning(
            "[reference_pack_analyzer] JSON parse failed: %s\n--- raw ---\n%s",
            e,
            full_text[:2000],
        )
        raise ReferencePackAnalysisFailed(
            f"模型输出不是有效 JSON: {e}\n\n原始输出前 500 字:\n{full_text[:500]}"
        ) from e

    return analysis
