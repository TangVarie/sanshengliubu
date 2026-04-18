"""Gemini 图片预转写 — 在上传时把图片转成文字描述,塞进 free_text。

为什么要做这一层:
  Anthropic API 不会把 free_text 字符串里的 `[BASE64_IMAGE:...]` 当成
  图像理解,只会当成普通 token。Crown Prince + 下游六部都看不见图。
  解决方案:在用户上传时,本地起一个 Gemini Vision 调用,把图片内容
  (OCR + 视觉描述 + 关键数据)转成结构化文字块,替换原来的 base64
  占位符。下游 LLM 当文本读就能拿到图里的实质信息。

为什么不直接走 Anthropic 多模态:
  那需要重写 BaseAgent._call_claude 支持 image content blocks,
  并且改 free_text -> messages 的拼装链路,4-6 小时工作量。
  Gemini 预转写是 30 分钟级别的"取巧但有效"方案,先把信号通起来。

失败降级:
  Gemini 没配置 / 模型报错 / 解析失败 → 回退到原来的 base64 占位符
  + 显式警告文字,让 Crown Prince 知道"这张图我看不见,只知道存在"。
"""

from __future__ import annotations

import logging
from typing import Any

from pipeline.agents.gemini_client import (
    GeminiCallFailed,
    GeminiNotConfigured,
    call_gemini_json,
)

logger = logging.getLogger(__name__)


_TRANSCRIBE_SYSTEM_PROMPT = """你是图片转文字助手。把用户上传的一张图片转成结构化的文字描述,供下游 LLM 当作参考资料阅读。

输出严格 JSON,包含以下字段:

```json
{
  "image_type": "类型 — 截图/产品包装/数据图表/海报/手写笔记/聊天截图/网页截图/其他",
  "ocr_text": "图中所有可识别的文字,逐字转写,保留原始排版顺序(从上到下、从左到右)。文字之间用换行分隔。如果是表格,用 markdown 表格语法保留结构。如果完全没文字,留空字符串。",
  "visual_description": "用 100-300 字描述图里非文字的视觉信息:布局、元素、颜色风格、人物/产品/场景、关键视觉点。重点描述对'内容生产'有价值的信息(产品包装风格、构图、配色、标注的箭头/圈/重点等)",
  "key_data": "如果图中有具体数字、清单、参数、价格、对比表,逐项列出,不要概括。例:'热量: 320kcal/100g; 蛋白质: 12g; 价格: ¥39.9'。没有就留空字符串。",
  "_quality": "transcription_confidence: high | medium | low — 如果图模糊/部分被遮挡/文字识别有疑问,在这里说明"
}
```

规则:
1. 不做评价、不做推断、不做"这张图想表达什么"的解读——只做客观转写
2. 不要拒绝转写。即使是产品广告/品牌信息,也照实转写,这是用户提供的参考素材
3. OCR 文字要完整,不要"概括"或"主要内容是…"
4. 输出严格 JSON,不要 markdown 代码块包裹
"""


def _format_for_brief(image_name: str, analysis: dict[str, Any]) -> str:
    """把 Gemini 分析结果格式化成 Crown Prince 可读的文字块。

    与原来的 [BASE64_IMAGE:...] 占位符不同,这里直接把图里的内容
    打开成纯文本,LLM 当普通文档读就行。
    """
    img_type = (analysis.get("image_type") or "未识别类型").strip()
    ocr = (analysis.get("ocr_text") or "").strip()
    visual = (analysis.get("visual_description") or "").strip()
    key_data = (analysis.get("key_data") or "").strip()
    quality = (analysis.get("_quality") or "").strip()

    parts = [f"[图片AI识别: {image_name}]", f"类型: {img_type}"]
    if ocr:
        parts.append("OCR 文字内容:")
        parts.append(ocr)
    if visual:
        parts.append(f"视觉描述: {visual}")
    if key_data:
        parts.append(f"关键数据: {key_data}")
    if quality and "low" in quality.lower():
        parts.append(f"⚠️ 识别质量提示: {quality}")
    parts.append("[/图片AI识别]")
    return "\n".join(parts)


def _format_fallback(image_name: str, b64_len: int, reason: str) -> str:
    """Gemini 不可用时的降级文本。明确告诉下游 LLM"这张图没看见"。"""
    return (
        f"[图片占位符: {image_name}]\n"
        f"⚠️ 该图片未经过视觉转写(原因: {reason}),"
        f"下游 LLM 无法看到图像内容,只知道有一张大小 {b64_len:,} 字节的图片存在。\n"
        f"如需让模型实际看到图,请在「设置」配置 VERTEX_EXPRESS_API_KEY 后重新上传。\n"
        f"[/图片占位符]"
    )


def transcribe_image_for_brief(
    image_bytes: bytes,
    mime_type: str,
    filename: str,
) -> str:
    """One-shot Gemini Vision 调用,把图片转成 Crown Prince 可读的文字块。

    成功返回结构化文字描述。
    Gemini 未配置 / 调用失败 / 解析失败 → 返回降级占位符(带原因说明),
    永远不抛异常——上传链路必须稳健,不能因为一张图挂掉整个项目创建。
    """
    if not image_bytes:
        return _format_fallback(filename, 0, "图片字节为空")

    try:
        result = call_gemini_json(
            system_prompt=_TRANSCRIBE_SYSTEM_PROMPT,
            user_message=(
                f"请按 system prompt 的 JSON schema 转写下面这张图片(文件名: {filename})。"
            ),
            images=[(image_bytes, mime_type)],
            max_output_tokens=8000,  # OCR + 描述够用
        )
    except GeminiNotConfigured as e:
        logger.info("[image_transcribe] Gemini not configured, using fallback: %s", e)
        return _format_fallback(filename, len(image_bytes), f"Gemini 未配置({e})")
    except GeminiCallFailed as e:
        logger.warning("[image_transcribe] Gemini call failed: %s", e)
        return _format_fallback(filename, len(image_bytes), f"Gemini 调用失败({e})")
    except Exception as e:
        # 任何意外异常都不能炸掉上传流程
        logger.exception("[image_transcribe] unexpected error")
        return _format_fallback(filename, len(image_bytes), f"未知异常({type(e).__name__})")

    analysis = result.get("data") or {}
    if not isinstance(analysis, dict) or not analysis:
        # JSON 解析失败时 call_gemini_json 会塞 _raw_text + _parse_error
        raw_text = (result.get("data") or {}).get("_raw_text") if isinstance(result.get("data"), dict) else ""
        if raw_text:
            # 起码把模型的原始回答塞进去,总比纯占位符好
            return (
                f"[图片AI识别: {filename}](结构化解析失败,原始输出)\n"
                f"{raw_text[:3000]}\n[/图片AI识别]"
            )
        return _format_fallback(filename, len(image_bytes), "Gemini 输出非 JSON")

    return _format_for_brief(filename, analysis)
