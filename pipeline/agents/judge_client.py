"""判定服务(judge)客户端 —— Jev 闭集判定的薄 HTTP 口(v0.37.0)。

judge 是 BYWOOD 三仓共用的判定服务(独立仓库,Railway 单独一个服务):同一个
Jev 模型、同一套题库纪律、同一张账本(truth_vault.note_feature_answers)。
三省六部这边只放这一个薄客户端,题库和服务都不在本仓。

为什么不照抄 kimi_client
------------------------
kimi_client 是「调一个生成式模型」的口子,这里是「调一个 HTTP 判定服务」,
能复用的只有两样(设计审查 docs/02 §5):

  - 重试:`pipeline.llm_retry.call_with_retry`。它按**异常文本**判瞬时故障
    (`_is_transient` 找 429 / 502 / 504 / timeout / connection),所以抛进去的
    异常文本必须带 HTTP 状态码。反过来,**不该重试的状态绝不能以异常形式进
    重试器** —— 422 的 detail 里常带 subject_id,而 subject_id 含 run 的 UUID,
    UUID 里随时可能出现「503」「429」这样的子串,抛进去就会被当成瞬时故障白等。
    所以单次请求对非瞬时状态**返回**(status, body),只对瞬时状态抛异常。
  - 失败语义:只抛 `JudgeNotConfigured` / `JudgeCallFailed` 两种,调用方一律
    当「跳过」,流水线照常走完。

不能照抄的三样:
  - `is_available` 绑在 `backend_configured(模型名)` 上 —— `jev-1.13.0` 会被
    `_model_vendor` 归到 claude vendor,判成「没配」。这里只看 JUDGE_URL。
  - `_call_kimi_raw` 是 `client.messages.create`,这里是 POST {JUDGE_URL}/judge。
  - `_estimate_cost_usd` 查 COST_PER_1M_* 表,查不到 jev 会记 $0。这里按
    Jev 的官方价单独算(输入 0.042 美元 / 百万 token,输出免费)。

超时按 8 秒(docs/00 #4 的口径),**不要**套 kimi_client 那套 120 秒 + 60 秒墙钟:
判定服务单次判定亚秒级,8 秒还没回来就当这次没判,影子期宁可少一条记录,
也不能拖住流水线尾段。

HTTP 契约(POST {JUDGE_URL}/judge,header X-Judge-Key)
------------------------------------------------------
  请求:{"bank", "run_tag", "write", "return_rows": false, "project", "category"?,
        "published"?, "subjects": [...]}。三省六部发的全是**未发布**内容,orchestrator
        每个请求都带 project(ssll:<projects.id>)和 published=false,能认出品类时带
        category(见 jev_shadow.judge_scope)。
  200 → {"results": [...每个 subject 的 items,或单个 subject 的 {"error"}],
         "errors": n, "written": n|null, "policy": {...}}
  401 / 503 → 服务不可用(key 不对 / 服务端没配)  → JudgeNotConfigured
  403 且 detail 以 "policy:" 开头 → 数据出境策略拒绝(处方药项目的未发布稿)
        → JudgePolicyRefused(是 JudgeCallFailed 的子类),不重试
  404 → 题库不在服务端(新题库还没部署)          → JudgeCallFailed(kind=bank_missing)
  422 → 请求形状不对(例如缺 project)             → JudgeCallFailed(kind=bad_request)
  502 → Jev 全部失败;429 / 504 / 超时 / 连不上 → 重试一次后 JudgeCallFailed

成本不在这里记账:本模块只**算**(`estimate_cost_usd`),记账由 orchestrator 调
`accumulate_auxiliary_cost(run_id, cost_usd, …, source="jev_judge")`,和二审 /
结构审 / 批量采样三处同一个做法。
"""

from __future__ import annotations

import http.client
import json
import logging
import socket
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from pipeline.config import (
    JUDGE_CLIENT_CONCURRENCY,
    JUDGE_COST_PER_1M_INPUT,
    JUDGE_COST_PER_1M_OUTPUT,
    JUDGE_MAX_ATTEMPTS,
    JUDGE_MAX_SUBJECTS_PER_REQUEST,
    JUDGE_RETRY_INITIAL_WAIT,
    JUDGE_RETRY_MAX_WAIT,
    JUDGE_RUN_TAG,
    JUDGE_TIMEOUT_SECONDS,
)
from pipeline.logger_utils import mask_secrets

logger = logging.getLogger(__name__)

USER_AGENT = "sanshengliubu-judge-client/1"

# 只有这几个状态走重试器(文本里带状态码,_is_transient 认得出来)。
# 503 不在里面:judge 的 503 是「服务端没配 JUDGE_API_KEY / Jev 密钥 / 写库」,
# 等几秒也不会好,按不可用处理。
_RETRY_STATUSES = frozenset({429, 502, 504})


class JudgeNotConfigured(RuntimeError):
    """判定服务不可用:没配 JUDGE_URL / key 不对(401)/ 服务端没配(503)。"""

    def __init__(self, message: str, *, kind: str = "not_configured", status: int | None = None):
        super().__init__(message)
        self.kind = kind
        self.status = status


class JudgeCallFailed(RuntimeError):
    """调用失败(重试之后)。kind 给日志和 stage_log 用,调用方一律当跳过。"""

    def __init__(self, message: str, *, kind: str = "call_failed", status: int | None = None):
        super().__init__(message)
        self.kind = kind
        self.status = status


class JudgePolicyRefused(JudgeCallFailed):
    """数据出境策略拒绝(403 policy:…)。不是故障,不重试,记 policy_blocked。"""

    def __init__(self, message: str, *, status: int | None = 403):
        super().__init__(message, kind="policy_blocked", status=status)


class _Transient(RuntimeError):
    """只在 call_with_retry 内部流转的瞬时故障。文本里必须带状态码 / timeout /
    connection,llm_retry._is_transient 靠文本判断。"""

    def __init__(self, message: str, *, kind: str, status: int | None = None):
        super().__init__(message)
        self.kind = kind
        self.status = status


# ── 配置 ──────────────────────────────────────────────────────────────

def _settings() -> tuple[str, str]:
    """(JUDGE_URL, JUDGE_API_KEY)。st.secrets 优先,环境变量兜底(同 secrets_compat)。"""
    try:
        from utils.secrets_compat import get_secret
        url = str(get_secret("JUDGE_URL", "") or "").strip().rstrip("/")
        key = str(get_secret("JUDGE_API_KEY", "") or "").strip()
    except Exception:  # pragma: no cover - 读配置本身出错就当没配
        return "", ""
    return url, key


def is_configured() -> bool:
    """只做本地检查,不发网络请求。没配 JUDGE_URL = 不可用,所有影子阶段零副作用跳过。

    JUDGE_API_KEY 可以不配:服务端显式 JUDGE_ALLOW_ANONYMOUS=1 的本地开发场景
    不需要它;生产服务端 fail-closed,漏配会拿到 401/503 → JudgeNotConfigured。
    """
    return bool(_settings()[0])


def estimate_cost_usd(usage: dict | None) -> float:
    """按 Jev 官方价算:输入 0.042 美元 / 百万 token,输出免费(docs/31 §7.1)。"""
    u = usage or {}
    return (
        int(u.get("input_tokens", 0) or 0) * JUDGE_COST_PER_1M_INPUT
        + int(u.get("output_tokens", 0) or 0) * JUDGE_COST_PER_1M_OUTPUT
    ) / 1_000_000


def _sum_usage(results: list) -> dict:
    tot = {"input_tokens": 0, "output_tokens": 0}
    for r in results or []:
        u = (r or {}).get("usage") if isinstance(r, dict) else None
        if not isinstance(u, dict):
            continue
        for k in tot:
            try:
                tot[k] += int(u.get(k, 0) or 0)
            except (TypeError, ValueError):
                pass
    return tot


def _detail(raw: bytes | str | None) -> str:
    if raw is None:
        return ""
    text = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
    try:
        obj = json.loads(text)
        if isinstance(obj, dict) and "detail" in obj:
            d = obj["detail"]
            text = d if isinstance(d, str) else json.dumps(d, ensure_ascii=False)
    except (ValueError, TypeError):
        pass
    return mask_secrets(text.strip().replace("\n", " "))[:400]


# ── 单次请求 ──────────────────────────────────────────────────────────

def _post_once(url: str, key: str, data: bytes, timeout: float) -> tuple[int, bytes]:
    """发一次。非瞬时状态**返回** (status, body);瞬时故障抛 _Transient。"""
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    if key:
        headers["X-Judge-Key"] = key
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(getattr(resp, "status", 200) or 200), resp.read()
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read()
        except Exception:
            body = b""
        if exc.code in _RETRY_STATUSES:
            raise _Transient(
                f"judge HTTP {exc.code}: {_detail(body)[:200]}",
                kind="jev_failed" if exc.code == 502 else "http_transient",
                status=exc.code,
            ) from exc
        return int(exc.code), body
    except (socket.timeout, TimeoutError) as exc:
        raise _Transient(
            f"judge request timed out after {timeout:g}s", kind="timeout"
        ) from exc
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, (socket.timeout, TimeoutError)):
            raise _Transient(
                f"judge request timed out after {timeout:g}s", kind="timeout"
            ) from exc
        raise _Transient(
            f"judge connection error: {mask_secrets(str(reason))[:200]}",
            kind="unreachable",
        ) from exc
    except (ConnectionError, http.client.HTTPException) as exc:
        raise _Transient(
            f"judge connection error: {type(exc).__name__}: {mask_secrets(str(exc))[:200]}",
            kind="unreachable",
        ) from exc


# ── 公开接口 ──────────────────────────────────────────────────────────

def judge(
    bank: str,
    subjects: list[dict],
    *,
    write: bool = False,
    project: str | None = None,
    category: str | None = None,
    published: bool | None = None,
    run_tag: str | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """POST /judge 一次。返回服务端的响应,外加本地算的 usage / cost_usd / elapsed_ms。

    published=False 让服务端的数据出境策略把这批按**未发布稿**处理 —— 预埋评论
    的 subject_type 是 comment(服务端默认当公开内容),必须显式带上;三省六部发出
    去的东西全是没发布的,orchestrator 一律带 False。None = 不发这个字段。

    只抛 JudgeNotConfigured / JudgeCallFailed(含 JudgePolicyRefused)。
    """
    url, key = _settings()
    if not url:
        raise JudgeNotConfigured("没配 JUDGE_URL,判定服务影子跑全部跳过")
    if not subjects:
        return {"results": [], "errors": 0, "written": None, "policy": None,
                "usage": {"input_tokens": 0, "output_tokens": 0}, "cost_usd": 0.0,
                "elapsed_ms": 0}

    payload: dict[str, Any] = {
        "bank": bank,
        "run_tag": run_tag or JUDGE_RUN_TAG,
        "write": bool(write),
        "return_rows": False,
        "subjects": subjects,
    }
    if project:
        payload["project"] = project
    if category:
        payload["category"] = category
    if published is not None:
        payload["published"] = bool(published)
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    _timeout = float(timeout or JUDGE_TIMEOUT_SECONDS)

    t0 = time.monotonic()
    try:
        from pipeline.llm_retry import call_with_retry
        status, raw = call_with_retry(
            _post_once,
            f"{url}/judge",
            key,
            data,
            _timeout,
            max_attempts=JUDGE_MAX_ATTEMPTS,
            initial_wait=JUDGE_RETRY_INITIAL_WAIT,
            max_wait=JUDGE_RETRY_MAX_WAIT,
            operation=f"judge:{bank}",
        )
    except _Transient as exc:
        raise JudgeCallFailed(
            f"判定服务调用失败(bank={bank}): {exc}", kind=exc.kind, status=exc.status
        ) from exc
    except ValueError as exc:
        # urllib 对不合法的 URL(没写 scheme 等)抛 ValueError —— 配置问题,不是故障
        raise JudgeNotConfigured(
            f"JUDGE_URL 不可用: {mask_secrets(str(exc))[:200]}", kind="bad_url"
        ) from exc
    except Exception as exc:  # noqa: BLE001 — 只许抛两种异常
        raise JudgeCallFailed(
            f"判定服务调用失败(bank={bank}): {type(exc).__name__}: "
            f"{mask_secrets(str(exc))[:300]}",
            kind="client_error",
        ) from exc
    elapsed_ms = round((time.monotonic() - t0) * 1000)

    if status == 200:
        try:
            body = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise JudgeCallFailed(
                f"判定服务返回的不是 JSON(bank={bank})", kind="bad_response", status=200
            ) from exc
        if not isinstance(body, dict) or not isinstance(body.get("results"), list):
            raise JudgeCallFailed(
                f"判定服务返回缺 results(bank={bank})", kind="bad_response", status=200
            )
        usage = _sum_usage(body["results"])
        body["usage"] = usage
        body["cost_usd"] = estimate_cost_usd(usage)
        body["elapsed_ms"] = elapsed_ms
        return body

    detail = _detail(raw)
    if status in (401, 503):
        raise JudgeNotConfigured(
            f"判定服务不可用(HTTP {status}): {detail}",
            kind="unauthorized" if status == 401 else "unavailable",
            status=status,
        )
    if status == 403 and detail.startswith("policy:"):
        raise JudgePolicyRefused(f"数据出境策略拒绝: {detail}", status=403)
    if status == 404:
        raise JudgeCallFailed(
            f"判定服务没有题库 {bank}(HTTP 404): {detail}", kind="bank_missing", status=404
        )
    if status == 422:
        raise JudgeCallFailed(
            f"判定服务拒收请求(HTTP 422): {detail}", kind="bad_request", status=422
        )
    raise JudgeCallFailed(
        f"判定服务返回 HTTP {status}: {detail}", kind="http_error", status=status
    )


def judge_many(
    bank: str,
    subjects: list[dict],
    *,
    chunk_size: int | None = None,
    max_workers: int | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """把 subjects 切块并发调 judge()(同步函数,调用方用 asyncio.to_thread 包)。

    切块是因为服务端单请求上限 200 个 subject,而且 8 秒超时装不下太大的一批
    (每篇 1–2 次 Jev 调用、服务端 4 路并行)。

    任何一块失败就**不再发**后面的块(fail fast):不可用 / 策略拒绝 / 请求形状
    不对 / Jev 挂了,换一块也是同样结果,影子阶段不值得拖住流水线尾段。
    已经成功的块照样返回。

    全部块都没成功时抛第一块的异常(JudgeNotConfigured / JudgeCallFailed);
    部分成功时返回 {"results", "chunk_failures", "usage", "cost_usd", ...}。
    """
    n = max(1, int(chunk_size or JUDGE_MAX_SUBJECTS_PER_REQUEST))
    chunks = [subjects[i : i + n] for i in range(0, len(subjects or []), n)]
    empty = {"results": [], "errors": 0, "written": None, "policy": None,
             "chunk_failures": [], "usage": {"input_tokens": 0, "output_tokens": 0},
             "cost_usd": 0.0, "elapsed_ms": 0, "requests": 0}
    if not chunks:
        return empty

    abort = threading.Event()

    def _run(chunk: list[dict]):
        if abort.is_set():
            return "aborted", None
        try:
            return "ok", judge(bank, chunk, **kwargs)
        except (JudgeNotConfigured, JudgeCallFailed) as exc:
            abort.set()
            return "failed", exc

    workers = max(1, min(int(max_workers or JUDGE_CLIENT_CONCURRENCY), len(chunks)))
    if workers == 1:
        outs = [_run(c) for c in chunks]
    else:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            outs = list(ex.map(_run, chunks))

    out = dict(empty)
    out["results"] = []
    out["chunk_failures"] = []
    first_exc: Exception | None = None
    written = 0
    any_written = False
    for (state, res), chunk in zip(outs, chunks):
        ids = [s.get("subject_id") for s in chunk]
        if state == "ok":
            out["requests"] += 1
            out["results"].extend(res.get("results") or [])
            out["errors"] += int(res.get("errors") or 0)
            if res.get("written") is not None:
                any_written = True
                written += int(res.get("written") or 0)
            if out["policy"] is None:
                out["policy"] = res.get("policy")
            for k in ("input_tokens", "output_tokens"):
                out["usage"][k] += int((res.get("usage") or {}).get(k, 0) or 0)
            out["elapsed_ms"] = max(out["elapsed_ms"], int(res.get("elapsed_ms") or 0))
        elif state == "failed":
            first_exc = first_exc or res
            out["chunk_failures"].append({
                "kind": getattr(res, "kind", "call_failed"),
                "status": getattr(res, "status", None),
                "error": str(res)[:300],
                "subject_ids": ids,
            })
        else:
            out["chunk_failures"].append({"kind": "aborted", "subject_ids": ids})
    if out["requests"] == 0 and first_exc is not None:
        raise first_exc
    out["written"] = written if any_written else None
    out["cost_usd"] = estimate_cost_usd(out["usage"])
    return out


def skip_record(exc: BaseException) -> dict[str, Any]:
    """把两种异常压成 stage_log / 诊断字段用的跳过记录。"""
    return {
        "status": "skipped",
        "reason": getattr(exc, "kind", "call_failed"),
        "http_status": getattr(exc, "status", None),
        "detail": mask_secrets(str(exc))[:400],
    }
