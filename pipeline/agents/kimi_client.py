"""Kimi auxiliary client — 辅助层的轻量 LLM 调用口。

v0.32.0 用它替换了 gemini_client.py。接的岗位没变(网感二审 / 结构审 /
图片预转写 / 截图分析),换掉的是底下那家:google-genai + Vertex Express key
→ anthropic SDK + Moonshot anthropic-compat 端点。

为什么辅助层要有独立于 BaseAgent 的调用路径
--------------------------------------------
BaseAgent._call_model 那条路上挂着 stage_log 落库、run 级 token 熔断、
滑动窗口限流、cache_control 400 自愈、no-channel 跨模型降级 —— 主链路需要
这些,辅助层不需要,而且不该被它们影响:

  - 辅助层是 advisory。二审挂了就当没二审,不该把主链路的 run 预算烧掉,
    更不该因为它触发 RunBudgetExceededError 把整条 run 判死。
  - 辅助层的调用不隶属任何 stage,写 stage_log 会污染流水线详情页。

所以这里只留一层薄重试(pipeline/llm_retry.call_with_retry),别的都不要。
代价是辅助层的 token 不进 run 总账 —— 和 v0.31 的 Gemini 层行为一致,
成本单独通过返回值里的 cost_usd 上报给调用方。

路由复用 pipeline.agents.get_client_for_model,不自己拼 base_url —— 辅助层
和主链路用的是同一套 vendor 路由规则,复制一份迟早漂移。

Failure semantics
-----------------
只抛两种异常,POLICY 在调用方:
  KimiNotConfigured — 没配 key / 功能被关 / 模型名解析不出来
  KimiCallFailed    — 调用出错(重试之后仍然失败)
四个岗位的 agent 都把这两种当"跳过",流水线照常走完。
"""

from __future__ import annotations

import base64
import json
import logging
import re
from typing import Any

from pipeline.config import (
    COST_PER_1M_INPUT,
    COST_PER_1M_OUTPUT,
    ENABLE_KIMI_ASSIST,
    KIMI_ASSIST_MAX_OUTPUT_TOKENS,
    KIMI_ASSIST_MODEL,
    KIMI_ASSIST_MODEL_OVERRIDES,
    KIMI_ASSIST_TIMEOUT_SECONDS,
)
from pipeline.logger_utils import mask_secrets

logger = logging.getLogger(__name__)


class KimiNotConfigured(RuntimeError):
    """Raised when an assist call is requested but the backend isn't usable
    (feature flag off, missing API key, unroutable model name)."""


class KimiCallFailed(RuntimeError):
    """Raised when the assist call itself errors after bounded retries."""


# 兼容别名 —— v0.31 的调用方 catch 的是 Gemini* 名字。留着让任何还没改到的
# import 不炸;新代码请用 Kimi* 名字。
GeminiNotConfigured = KimiNotConfigured
GeminiCallFailed = KimiCallFailed


def resolve_assist_model(role: str | None) -> str:
    """岗位名 → 模型 ID。先查 KIMI_ASSIST_MODEL_OVERRIDES,查不到用全局默认。

    role 传 None 或不认识的名字都退到全局默认(而不是报错)—— 辅助层的调用
    方多是"顺手加一个新岗位"的场景,漏配 override 不该炸。
    """
    if role:
        pinned = KIMI_ASSIST_MODEL_OVERRIDES.get(role)
        if pinned:
            return pinned
    return KIMI_ASSIST_MODEL


# v0.31 名字。
resolve_gemini_model = resolve_assist_model


def is_available() -> bool:
    """辅助层现在能用吗?供设置页显示状态、以及调用方短路。

    只做本地检查(feature flag + backend 配置),不发网络请求。
    """
    if not ENABLE_KIMI_ASSIST:
        return False
    try:
        from pipeline.agents import backend_configured
        # 全局默认模型能路由通就算可用。个别岗位 override 到别家(比如
        # critic 钉在 DeepSeek)时,那家没配会在实际调用时报 KimiNotConfigured,
        # 由调用方降级成 skipped —— 不在这里连坐把整层判为不可用。
        return bool(backend_configured(KIMI_ASSIST_MODEL))
    except Exception as e:  # pragma: no cover - 防御 secrets 读取异常
        logger.debug("[kimi_assist] is_available() probe failed: %s", e)
        return False


def _estimate_cost_usd(input_tokens: int, output_tokens: int, model: str) -> float:
    """按 config 的价格表估算单次调用成本。

    模型不在表里就返回 0 —— 和主链路 _estimate_call_cost_usd 的行为一致:
    宁可显示 $0 让运营看出"价格表该补了",也不要拿一个默认费率编个数。
    """
    in_rate = COST_PER_1M_INPUT.get(model, 0.0)
    out_rate = COST_PER_1M_OUTPUT.get(model, 0.0)
    return (input_tokens / 1_000_000) * in_rate + (output_tokens / 1_000_000) * out_rate


# ── 发送前图片降采样 ────────────────────────────────────────────────
# 手机截图动辄 1-2MB(3x 分辨率),对 OCR/视觉理解纯属浪费;截图分析还把
# 最多 10 张打包进【一次】请求,base64 后 body 可达 8MB+ —— 上行慢的线路
# 上表现为"传不完、无限等"(Railway 排障实录,2026-08-28)。压到长边
# 1568px(视觉模型的常见有效上限)的 JPEG 后,单张通常只剩 100-300KB。
# Pillow 不可用时原样发送:降采样是优化,不是功能前提。
_IMAGE_MAX_EDGE = 1568
_IMAGE_JPEG_QUALITY = 85
_IMAGE_SHRINK_THRESHOLD_BYTES = 400 * 1024  # 小于 400KB 的小图不折腾


def _shrink_image(raw: bytes, mime: str) -> tuple[bytes, str]:
    """把过大的图压成长边 ≤1568px 的 JPEG;失败/无 Pillow/压不小则原样返回。"""
    if not raw or len(raw) <= _IMAGE_SHRINK_THRESHOLD_BYTES:
        return raw, mime
    try:
        import io
        from PIL import Image
    except Exception:
        return raw, mime  # 无 Pillow:原样发送
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
        w, h = img.size
        scale = _IMAGE_MAX_EDGE / float(max(w, h))
        if scale < 1.0:
            img = img.resize(
                (max(1, int(w * scale)), max(1, int(h * scale))),
                Image.LANCZOS,
            )
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")  # 丢 alpha:OCR 不需要透明度
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=_IMAGE_JPEG_QUALITY, optimize=True)
        out = buf.getvalue()
        if len(out) < len(raw):
            logger.info(
                "[kimi_client] 图片降采样 %dKB → %dKB(%dx%d → 长边≤%d)",
                len(raw) // 1024, len(out) // 1024, w, h, _IMAGE_MAX_EDGE,
            )
            return out, "image/jpeg"
        return raw, mime  # 极端情况(纯色 PNG 等)压缩反而变大:用原图
    except Exception:
        logger.warning("[kimi_client] 图片降采样失败,原图发送", exc_info=True)
        return raw, mime


def _build_content(
    user_message: str, images: list[tuple[bytes, str]] | None
) -> list[dict[str, Any]] | str:
    """把文本 + 图片拼成 Anthropic content blocks。

    没有图片时直接返回字符串(省掉一层包装,也让 prompt cache 的 key 更稳)。
    """
    if not images:
        return user_message

    blocks: list[dict[str, Any]] = []
    for raw, mime in images:
        if not raw:
            continue
        raw, mime = _shrink_image(raw, mime)
        blocks.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    # mime 为空时按 png 处理 —— 和 BaseAgent._build_content_blocks
                    # 的兜底一致。
                    "media_type": mime or "image/png",
                    "data": base64.b64encode(raw).decode("ascii"),
                },
            }
        )
    if user_message:
        blocks.append({"type": "text", "text": user_message})
    return blocks or user_message


def call_kimi_text(
    system_prompt: str,
    user_message: str,
    *,
    model: str | None = None,
    max_output_tokens: int | None = None,
    images: list[tuple[bytes, str]] | None = None,
    max_attempts: int = 3,
) -> dict[str, Any]:
    """跑一次辅助调用,返回**原始文本**不做 JSON 解析。

    v0.33.3 新增,给批量采样(pipeline/batch_sampler.py)用:采样要的是执行
    模型按 system_prompt 写出来的**一篇内容**,那本来就不是 JSON,套
    `call_kimi_json` 会在 `_extract_json` 那里退化成 `_parse_error`。

    返回:
      - "text":          str,模型输出的原文(已 strip,保证非空)
      - "input_tokens" / "output_tokens" / "cost_usd" / "model": 同 call_kimi_json

    异常契约和 `call_kimi_json` 一致:只抛 KimiNotConfigured / KimiCallFailed。
    """
    return _call_kimi_raw(
        system_prompt,
        user_message,
        model=model,
        max_output_tokens=max_output_tokens,
        images=images,
        max_attempts=max_attempts,
    )


def call_kimi_json(
    system_prompt: str,
    user_message: str,
    *,
    model: str | None = None,
    max_output_tokens: int | None = None,
    images: list[tuple[bytes, str]] | None = None,
) -> dict[str, Any]:
    """跑一次辅助调用并把响应解析成 JSON。

    images: (raw_bytes, mime_type) 列表,用于多模态调用(截图 OCR + 分析)。
    传了图片就必须用支持视觉的模型 —— K2.6 原生多模态,DeepSeek 那两档没有
    视觉输入,把 image_transcriber / screenshot_analyzer 钉到 DeepSeek 会
    在这里拿到 400。

    返回:
      - "data":          解析出的 JSON(成功时保证是 dict)
      - "input_tokens":  int,非缓存输入
      - "output_tokens": int
      - "cost_usd":      float,本次调用的成本估算
      - "model":         实际调用的模型 ID
      - "grounding_urls": 恒为 []。v0.31 的 Gemini 层会在开 Google Search
                          grounding 时回填引用 URL;Kimi 这条路没有等价的
                          联网检索(网上取数统一走 SocialDataX),但 key 保留
                          着让调用方不用改结构。

    只抛 KimiNotConfigured / KimiCallFailed。JSON 解析失败【不】抛异常 ——
    退化成 {"_parse_error": ..., "_raw_text": ...} 交给调用方决定是忽略还是
    当软失败,和 v0.31 的契约一致。
    """
    raw = _call_kimi_raw(
        system_prompt,
        user_message,
        model=model,
        max_output_tokens=max_output_tokens,
        images=images,
    )
    return {
        "data": _extract_json(raw["text"]),
        "input_tokens": raw["input_tokens"],
        "output_tokens": raw["output_tokens"],
        "cost_usd": raw["cost_usd"],
        "model": raw["model"],
        "grounding_urls": [],
    }


def _call_kimi_raw(
    system_prompt: str,
    user_message: str,
    *,
    model: str | None = None,
    max_output_tokens: int | None = None,
    images: list[tuple[bytes, str]] | None = None,
    max_attempts: int = 3,
) -> dict[str, Any]:
    """共享的调用主体 —— 建 client、重试、抽文本、算成本。

    `call_kimi_json` 和 `call_kimi_text` 的唯一区别是拿到 text 之后解不解析,
    所以那部分留在各自的外壳里,这里只做到"拿到非空文本"为止。
    """
    if not ENABLE_KIMI_ASSIST:
        raise KimiNotConfigured(
            "辅助层已在 pipeline/config.py 里禁用(ENABLE_KIMI_ASSIST=False)"
        )

    model_id = model or KIMI_ASSIST_MODEL
    max_tokens = max_output_tokens or KIMI_ASSIST_MAX_OUTPUT_TOKENS

    try:
        from pipeline.agents import get_client_for_model
        client = get_client_for_model(model_id)
    except RuntimeError as e:
        # get_client_for_model 对"没配 key"抛的就是 RuntimeError,原文案已经
        # 指明该去 secrets.toml 加哪个字段,直接转成 NotConfigured 往上带。
        raise KimiNotConfigured(str(e)) from e
    except Exception as e:
        raise KimiNotConfigured(f"无法为模型 {model_id!r} 构造 client: {e}") from e

    content = _build_content(user_message, images)

    def _do_call():
        # 辅助层多数在用户同步等待的路径上跑(上传转写/截图分析),不能沿用
        # client 构造时的 900s 主链路超时 —— 端点不可达时页面会冻死一刻钟。
        # per-request 覆盖成 KIMI_ASSIST_TIMEOUT_SECONDS,挂死快速失败降级。
        return client.messages.create(
            model=model_id,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": content}],
            timeout=KIMI_ASSIST_TIMEOUT_SECONDS,
        )

    try:
        from pipeline.llm_retry import call_with_retry
        response = call_with_retry(
            _do_call,
            max_attempts=max_attempts,
            initial_wait=2.0,
            max_wait=30.0,
            operation=f"kimi_assist:{model_id}",
        )
    except Exception as e:
        raise KimiCallFailed(
            f"辅助调用失败 (model={model_id}): "
            f"{type(e).__name__}: {mask_secrets(str(e))[:400]}"
        ) from e

    # 取文本 —— thinking 开着时 content 里会有多个 block,只收 text。
    text_parts: list[str] = []
    for block in getattr(response, "content", None) or []:
        if getattr(block, "type", None) == "text":
            text_parts.append(getattr(block, "text", "") or "")
    text = "".join(text_parts).strip()

    usage = getattr(response, "usage", None)
    input_tokens = int(getattr(usage, "input_tokens", 0) or 0) if usage else 0
    output_tokens = int(getattr(usage, "output_tokens", 0) or 0) if usage else 0

    if not text:
        # 空响应不当成"解析失败"—— 那是两回事。max_tokens 被 thinking 吃光、
        # 或者模型只回了 tool_use block 时会走到这里,报出来比返回空 dict 好查。
        raise KimiCallFailed(
            f"模型 {model_id} 返回了空文本 "
            f"(stop_reason={getattr(response, 'stop_reason', '?')}, "
            f"in={input_tokens}, out={output_tokens})。"
            "多半是 max_tokens 太小被截断。"
        )

    return {
        "text": text,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": _estimate_cost_usd(input_tokens, output_tokens, model_id),
        "model": model_id,
    }


# v0.31 名字。
call_gemini_json = call_kimi_json


def _extract_json(text: str) -> dict[str, Any]:
    """宽容的 JSON 提取。

    和 BaseAgent._extract_json 不同,这里【不】尝试修补被截断的 JSON ——
    辅助层是 advisory,一个残缺响应返回 {_parse_error} 让调用方当"没说话"
    比硬补出一个看似完整实则缺字段的 dict 安全得多(补出来的东西会被当成
    真实审查结论用)。
    """
    # 直接解析
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # ```json ... ``` 围栏
    m = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass

    # 最外层大括号
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass

    return {
        "_parse_error": "could_not_extract_json",
        "_raw_text": text[:2000],
    }
