"""Reference-post analyzer — 抓取用户在第 2 页粘贴的对标帖 URL。

和 socialdatax_trend_scout 的区别:那个是按关键词自己找爆款,这个是抓用户
明确指定的几条帖子。两者共用同一套 SocialDataX 归一化逻辑。

为什么这里没有 LLM 调用
----------------------
v0.31 这个岗位跑在 Gemini 上:因为 Gemini 的 `url_context` 工具能让模型自己
去取 URL,所以"抓取"这件事只能由模型完成,模型顺带把页面文本整理成结构化
JSON。那条路的老问题是小红书大量 JS 渲染 + 登录墙,url_context 经常只拿到
og:title,prompt 里因此写了一大段"抓不到就诚实标注 fetch_status,别编"。

v0.32.0 换厂后 Kimi 没有等价的"模型自己取 URL"能力,于是这个岗位改走
SocialDataX —— 而 SocialDataX 返回的本来就是第一方结构化数据(真正文、真
互动量)。既然数据已经是结构化的,再让 LLM 转述一遍只会引入幻觉风险和
token 成本,所以整个 LLM 环节直接去掉,抽取改成确定性代码。

副作用是这条路比原来更可靠:不再有"抓到 og:title 就当抓到了"的半吊子结果,
fetch_status 由真实的 API 结果决定而不是由模型自述。

⚠️ detail 工具名待核对 —— 见 config.SOCIALDATAX_NOTE_DETAIL_TOOLS 的注释。
名字不对不会炸:该 stage 是 advisory-only,工具不存在时 MCP 报错 → 这条 URL
记 fetch_status="not_accessible",其余照常,流水线不受影响。

Advisory-only failure semantics —— 所有错误都表现为 skipped / 单条失败,
流水线在没有参考帖数据的情况下继续。
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import parse_qs, urlparse

from pipeline.agents.socialdatax_client import (
    SocialDataXCallFailed,
    SocialDataXNotConfigured,
    open_session,
    resolve_platform_id,
)
from pipeline.agents.socialdatax_trend_scout import (
    _find_note_list,
    _normalize_note,
    _usage,
)
from pipeline.config import (
    REFERENCE_ANALYZER_MAX_URLS,
    SOCIALDATAX_NOTE_DETAIL_TOOLS,
)

logger = logging.getLogger(__name__)


# URL host → SocialDataX platform id。用户可能贴的是任意平台的链接,而
# brief 里的 platform 字段说的是"这次要投哪个平台"—— 两者未必一致(拿抖音
# 爆款当小红书的对标参考是常见操作),所以优先按 URL 本身判断。
_HOST_PLATFORM_HINTS = (
    ("xiaohongshu.com", "xhs"),
    ("xhslink.com", "xhs"),
    ("douyin.com", "douyin"),
    ("iesdouyin.com", "douyin"),
    ("kuaishou.com", "kuaishou"),
    ("chenzhongtech.com", "kuaishou"),
    ("weibo.com", "weibo"),
    ("weibo.cn", "weibo"),
    ("channels.weixin.qq.com", "wechat"),
)

# 从 URL path 里抠 note/video id。各平台的形态:
#   小红书  /explore/<24位hex>  /discovery/item/<id>  /search_result/<id>
#   抖音    /video/<数字id>
#   快手    /short-video/<id>   /f/<id>
#   微博    /<uid>/<mid>        /detail/<mid>
# 统一策略:取 path 里最后一段"看起来像 id"的组件(≥8 位的 hex/数字/字母
# 混合),抠不出来就放弃这条 URL 并如实标注,不猜。
_ID_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_-]{8,}$")


def _infer_platform_id(url: str, fallback_platform: str) -> str | None:
    host = (urlparse(url).netloc or "").lower()
    for needle, pid in _HOST_PLATFORM_HINTS:
        if needle in host:
            return pid
    # 认不出 host(短链跳转、企业自建域名等)就退回 brief 声明的平台
    return resolve_platform_id(fallback_platform)


def _extract_note_id(url: str) -> str:
    path = urlparse(url).path or ""
    segments = [s for s in path.split("/") if s]
    for seg in reversed(segments):
        if _ID_SEGMENT_RE.match(seg):
            return seg
    return ""


def _detail_arguments(url: str, note_id: str, platform_id: str) -> dict[str, Any]:
    """拼 detail 工具的入参。

    xsec_token 必须带上 —— 小红书的分享链接里那个 query 参数是访问凭证,
    丢了它就算 note_id 正确也取不到内容。其它平台没有这个概念,带上也无害
    (未知参数由服务端忽略),但只在真的存在时才塞进去。
    """
    args: dict[str, Any] = {"note_id": note_id}
    qs = parse_qs(urlparse(url).query or "")
    for key in ("xsec_token", "xsec_source"):
        vals = qs.get(key) or []
        if vals and vals[0]:
            args[key] = vals[0]
    # 多数平台的 detail 工具用 note_id;视频类平台常用 video_id。两个都给,
    # 服务端取它认识的那个 —— 比按平台维护一张参数名映射表更耐得住改版。
    if platform_id in ("douyin", "kuaishou", "wechat"):
        args["video_id"] = note_id
    return args


def _record(
    url: str,
    *,
    fetch_status: str,
    title: str = "",
    body_fragment: str = "",
    cover_image_url: str = "",
    notes: str = "",
) -> dict[str, Any]:
    """统一的单条输出记录。字段名和 v0.31 保持一致 —— 下游(orchestrator 注
    入 brief、pages/3 展示)都按这个 schema 读。"""
    return {
        "url": url,
        "title": title,
        "body_fragment": body_fragment,
        "cover_image_url": cover_image_url,
        "fetch_status": fetch_status,
        "notes": notes,
    }


async def run_reference_analyzer(
    urls: list[str],
    *,
    platform: str = "小红书",
) -> dict[str, Any]:
    """抓取用户粘贴的参考帖 URL,返回逐条结果。

    Returns:
      {
        "verdict": "all_pass | partial | no_access | skipped",
        "posts": [...每条 URL 一条记录...],
        "_summary_stats": {attempted, ok, partial, failed},
        "_skip_reason": null | "...",
        "_gemini_usage": {...},   # 键名是历史契约,见 _usage() 注释
      }

    每条输入 URL 都恰好产出一条记录(失败也有,靠 fetch_status 区分)——
    UI 上"你给了我 5 条,每条的结果是什么"这个账要对得上。
    """
    urls = [u.strip() for u in (urls or []) if u and isinstance(u, str)]
    if not urls:
        return {
            "verdict": "skipped",
            "posts": [],
            "_skip_reason": "no urls provided",
        }

    dropped = 0
    if len(urls) > REFERENCE_ANALYZER_MAX_URLS:
        dropped = len(urls) - REFERENCE_ANALYZER_MAX_URLS
        urls = urls[:REFERENCE_ANALYZER_MAX_URLS]
        # 静默截断会让"我贴了 30 条怎么只分析了 10 条"变成一个查不出的谜,
        # 所以既 warning 也在返回值里记。
        logger.warning(
            "[ref_analyzer] 收到 %d 条 URL,超过 REFERENCE_ANALYZER_MAX_URLS=%d,"
            "只抓前 %d 条,丢弃 %d 条",
            len(urls) + dropped, REFERENCE_ANALYZER_MAX_URLS, len(urls), dropped,
        )

    # 按平台分组:SocialDataX 的 MCP 端点是 per-platform 的,同一平台的多条
    # URL 共用一个 session(一次 TCP+TLS+handshake 跑完一批)。
    by_platform: dict[str, list[str]] = {}
    posts_by_url: dict[str, dict[str, Any]] = {}
    for url in urls:
        pid = _infer_platform_id(url, platform)
        if not pid:
            posts_by_url[url] = _record(
                url,
                fetch_status="not_accessible",
                notes=f"无法判断平台(host 不认识,brief 平台 {platform!r} 也不在支持列表)",
            )
            continue
        if pid not in SOCIALDATAX_NOTE_DETAIL_TOOLS:
            posts_by_url[url] = _record(
                url,
                fetch_status="not_accessible",
                notes=f"平台 {pid} 没有配 detail 工具(见 SOCIALDATAX_NOTE_DETAIL_TOOLS)",
            )
            continue
        by_platform.setdefault(pid, []).append(url)

    api_calls = 0
    for pid, plat_urls in by_platform.items():
        tool = SOCIALDATAX_NOTE_DETAIL_TOOLS[pid]
        try:
            async with open_session(pid) as call:
                for url in plat_urls:
                    note_id = _extract_note_id(url)
                    if not note_id:
                        posts_by_url[url] = _record(
                            url,
                            fetch_status="not_accessible",
                            notes="URL 里解析不出 note_id(短链请先展开成完整链接)",
                        )
                        continue
                    try:
                        api_calls += 1
                        payload = await call(
                            tool, _detail_arguments(url, note_id, pid)
                        )
                    except SocialDataXCallFailed as e:
                        # 单条失败不影响同批其它条 —— 帖子被删/仅粉丝可见/
                        # 工具名不对,都是这一条的事。
                        logger.warning(
                            "[ref_analyzer] %s 抓取失败 (%s): %s",
                            url, tool, str(e)[:200],
                        )
                        posts_by_url[url] = _record(
                            url,
                            fetch_status="not_accessible",
                            notes=f"SocialDataX 调用失败: {str(e)[:160]}",
                        )
                        continue

                    posts_by_url[url] = _payload_to_record(url, payload, pid)
        except SocialDataXNotConfigured as e:
            logger.debug("[ref_analyzer] SocialDataX 未配置: %s", e)
            for url in plat_urls:
                posts_by_url.setdefault(
                    url,
                    _record(
                        url,
                        fetch_status="not_accessible",
                        notes=f"SocialDataX 未配置: {str(e)[:160]}",
                    ),
                )
        except SocialDataXCallFailed as e:
            # 建 session 就失败(鉴权/网络/端点不通)—— 这一平台整批都没戏。
            logger.warning(
                "[ref_analyzer] 平台 %s 建立会话失败: %s", pid, str(e)[:200]
            )
            for url in plat_urls:
                posts_by_url.setdefault(
                    url,
                    _record(
                        url,
                        fetch_status="not_accessible",
                        notes=f"SocialDataX 会话失败: {str(e)[:160]}",
                    ),
                )

    # 按输入顺序输出,让 UI 里的顺序和用户粘贴的顺序一致。
    validated = [
        posts_by_url.get(u) or _record(u, fetch_status="unknown")
        for u in urls
    ]

    ok_count = sum(1 for p in validated if p["fetch_status"] == "ok")
    partial_count = sum(1 for p in validated if p["fetch_status"] == "partial")
    effective_count = ok_count + partial_count
    if effective_count == len(urls):
        verdict = "all_pass"
    elif effective_count > 0:
        verdict = "partial"
    else:
        verdict = "no_access"

    usage = _usage(api_calls, "+".join(sorted(by_platform)) or "none")

    logger.info(
        "[ref_analyzer] verdict=%s, got %d/%d (ok=%d, partial=%d), "
        "%d 次 API 调用, cost=$%.4f",
        verdict, effective_count, len(urls), ok_count, partial_count,
        api_calls, usage["cost_usd"],
    )

    stats = {
        "attempted": len(urls),
        "ok": ok_count,
        "partial": partial_count,
        "failed": len(urls) - effective_count,
    }
    if dropped:
        stats["dropped_over_limit"] = dropped

    return {
        "verdict": verdict,
        "posts": validated,
        "_summary_stats": stats,
        "_gemini_usage": usage,
    }


def _payload_to_record(url: str, payload: Any, platform_id: str) -> dict[str, Any]:
    """把 detail 工具的返回归一化成一条 post 记录。

    detail 返回的形状没有正式文档,可能是 {note: {...}}、{data: {...}}、
    直接一个 note dict,或者(某些实现)一个只有一个元素的 list。复用
    trend_scout 的 _find_note_list 做同一套容错遍历,拿不到就退回把 payload
    本身当 note 试一次。
    """
    candidates = _find_note_list(payload)
    raw = candidates[0] if candidates else (payload if isinstance(payload, dict) else None)
    note = _normalize_note(raw, platform_id) if raw is not None else None

    if not note:
        return _record(
            url,
            fetch_status="not_accessible",
            notes="detail 返回里找不到可用的帖子字段(接口形状可能变了)",
        )

    body = note.get("snippet") or ""
    title = note.get("title") or ""
    # 拿到互动量说明是真的取到了这条帖子而不是一个占位骨架 —— 用它区分
    # ok / partial,比"标题非空"这种弱信号可靠。
    eng = note.get("engagement") or {}
    has_engagement = any(int(eng.get(k, 0) or 0) > 0 for k in eng)

    if body and has_engagement:
        status = "ok"
        notes = note.get("engagement_display") or ""
    elif title or body or note.get("cover_image_url"):
        status = "partial"
        notes = "只拿到部分字段(可能是仅粉丝可见 / 已删除 / 接口返回残缺)"
    else:
        status = "not_accessible"
        notes = "接口返回为空"

    return _record(
        url,
        fetch_status=status,
        title=title,
        body_fragment=body,
        cover_image_url=note.get("cover_image_url") or "",
        notes=notes,
    )
