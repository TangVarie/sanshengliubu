"""judge_client:契约映射、重试口径、切块、没配置时零网络。

起一个本地 HTTP 服务按脚本回状态码,真走 urllib + llm_retry.call_with_retry,
不打桩客户端内部 —— 要证明的正是「哪些状态进了重试器、哪些没进」。
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from pipeline.agents import judge_client as jc


class _Script:
    def __init__(self):
        self.responses: list = []      # [(status, body_dict | None, sleep_s)]
        self.requests: list[dict] = []
        self.lock = threading.Lock()

    def next(self):
        with self.lock:
            if len(self.responses) > 1:
                return self.responses.pop(0)
            return self.responses[0]


@pytest.fixture
def server(monkeypatch):
    script = _Script()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # 安静
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n).decode("utf-8"))
            with script.lock:
                script.requests.append({"path": self.path, "headers": dict(self.headers), "json": body})
            status, payload, sleep_s = script.next()
            if sleep_s:
                time.sleep(sleep_s)
            if payload is None:
                payload = _ok_payload(body)
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    monkeypatch.setenv("JUDGE_URL", f"http://127.0.0.1:{httpd.server_address[1]}/")
    monkeypatch.setenv("JUDGE_API_KEY", "k-test")
    # 重试等待别真睡(只换掉 llm_retry 模块里的 time,别碰全局 time.sleep ——
    # 服务端线程还要靠它模拟慢响应)
    import types
    import pipeline.llm_retry as lr
    monkeypatch.setattr(lr, "time", types.SimpleNamespace(sleep=lambda s: None))
    yield script
    httpd.shutdown()
    httpd.server_close()


def _ok_payload(req: dict) -> dict:
    return {
        "bank": req["bank"],
        "results": [
            {"subject_id": s["subject_id"], "subject_type": s["subject_type"],
             "items": {"q": {"answer": "是", "p": 0.9, "ambiguous": False}},
             "usage": {"input_tokens": 500_000, "output_tokens": 10}}
            for s in req["subjects"]
        ],
        "errors": 0, "written": None, "policy": {"published": False},
    }


def _subjects(n: int) -> list[dict]:
    return [{"subject_type": "ssll_sample", "subject_id": f"run-1:D1_x:g1:{i}", "raw_content": "正文"}
            for i in range(n)]


# ── 没配置 ────────────────────────────────────────────────────────────

def test_not_configured_never_touches_network(monkeypatch):
    def _boom(*a, **kw):
        raise AssertionError("没配 JUDGE_URL 却发了请求")

    monkeypatch.setattr(jc.urllib.request, "urlopen", _boom)
    assert jc.is_configured() is False
    with pytest.raises(jc.JudgeNotConfigured):
        jc.judge("feature_questions_v0_1", _subjects(1))
    with pytest.raises(jc.JudgeNotConfigured):
        jc.judge_many("feature_questions_v0_1", _subjects(7))


# ── 200 ──────────────────────────────────────────────────────────────

def test_200_payload_headers_and_cost(server):
    server.responses = [(200, None, 0)]
    out = jc.judge("feature_questions_v0_1", _subjects(2), write=True,
                   project="ssll:proj-1", category="保健品", published=False)
    req = server.requests[0]
    assert req["path"] == "/judge"
    assert req["headers"].get("X-Judge-Key") == "k-test"
    body = req["json"]
    assert body["write"] is True and body["return_rows"] is False
    assert body["project"] == "ssll:proj-1" and body["category"] == "保健品"
    assert body["published"] is False and body["run_tag"] == "primary"
    assert len(out["results"]) == 2
    assert out["usage"] == {"input_tokens": 1_000_000, "output_tokens": 20}
    # 0.042 美元 / 百万输入 token,输出免费
    assert out["cost_usd"] == pytest.approx(0.042)


def test_category_and_published_omitted_when_none(server):
    server.responses = [(200, None, 0)]
    jc.judge("feature_questions_v0_1", _subjects(1), project="ssll:p")
    body = server.requests[0]["json"]
    assert "category" not in body and "published" not in body


# ── 不重试的状态 ──────────────────────────────────────────────────────

def test_policy_403_is_refused_without_retry(server):
    server.responses = [(403, {"detail": "policy: 处方药项目 ssll:p 的未发布稿不出境"}, 0)]
    with pytest.raises(jc.JudgePolicyRefused) as ei:
        jc.judge("feature_questions_v0_1", _subjects(1), project="ssll:p")
    assert ei.value.kind == "policy_blocked"
    assert isinstance(ei.value, jc.JudgeCallFailed)   # 调用方按 CallFailed 当跳过
    assert len(server.requests) == 1


def test_422_detail_with_status_like_digits_is_not_retried(server):
    # subject_id 里的 UUID 随时可能含「503」「429」这样的子串 —— 422 绝不能进重试器
    server.responses = [(422, {"detail": "run-5034291-abc:D1: 题库需要 raw_content (timeout?)"}, 0)]
    with pytest.raises(jc.JudgeCallFailed) as ei:
        jc.judge("feature_questions_v0_1", _subjects(1), project="ssll:p")
    assert ei.value.kind == "bad_request"
    assert len(server.requests) == 1


@pytest.mark.parametrize("status,kind", [(401, "unauthorized"), (503, "unavailable")])
def test_401_503_mean_not_available(server, status, kind):
    server.responses = [(status, {"detail": "JUDGE_API_KEY 未配置"}, 0)]
    with pytest.raises(jc.JudgeNotConfigured) as ei:
        jc.judge("feature_questions_v0_1", _subjects(1), project="ssll:p")
    assert ei.value.kind == kind
    assert len(server.requests) == 1


def test_404_bank_missing(server):
    server.responses = [(404, {"detail": "没有这个题库：ssll_critic_v0.1"}, 0)]
    with pytest.raises(jc.JudgeCallFailed) as ei:
        jc.judge("ssll_critic_v0.1", _subjects(1), project="ssll:p")
    assert ei.value.kind == "bank_missing" and len(server.requests) == 1


# ── 重试的状态 ────────────────────────────────────────────────────────

def test_502_retried_then_jev_failed(server):
    server.responses = [(502, {"detail": "Jev 调用失败"}, 0)]
    with pytest.raises(jc.JudgeCallFailed) as ei:
        jc.judge("feature_questions_v0_1", _subjects(1), project="ssll:p")
    assert ei.value.kind == "jev_failed"
    assert len(server.requests) == jc.JUDGE_MAX_ATTEMPTS == 2


@pytest.mark.parametrize("status", [429, 504])
def test_transient_then_ok(server, status):
    server.responses = [(status, {"detail": "busy"}, 0), (200, None, 0)]
    out = jc.judge("feature_questions_v0_1", _subjects(1), project="ssll:p")
    assert len(out["results"]) == 1 and len(server.requests) == 2


def test_timeout_is_retried_and_reported(server):
    server.responses = [(200, None, 1.0)]
    t0 = time.monotonic()
    with pytest.raises(jc.JudgeCallFailed) as ei:
        jc.judge("feature_questions_v0_1", _subjects(1), project="ssll:p", timeout=0.2)
    assert ei.value.kind == "timeout"
    assert len(server.requests) == 2
    assert time.monotonic() - t0 < 5


def test_unreachable(monkeypatch):
    monkeypatch.setenv("JUDGE_URL", "http://127.0.0.1:9")   # discard 端口,没人听
    import types
    import pipeline.llm_retry as lr
    monkeypatch.setattr(lr, "time", types.SimpleNamespace(sleep=lambda s: None))
    with pytest.raises(jc.JudgeCallFailed) as ei:
        jc.judge("feature_questions_v0_1", _subjects(1), project="ssll:p")
    assert ei.value.kind in ("unreachable", "timeout")


def test_bad_url_is_not_configured(monkeypatch):
    monkeypatch.setenv("JUDGE_URL", "judge.internal")      # 漏了 scheme
    with pytest.raises(jc.JudgeNotConfigured) as ei:
        jc.judge("feature_questions_v0_1", _subjects(1), project="ssll:p")
    assert ei.value.kind == "bad_url"


# ── 切块 ─────────────────────────────────────────────────────────────

def test_judge_many_chunks(server):
    server.responses = [(200, None, 0)]
    out = jc.judge_many("feature_questions_v0_1", _subjects(12), chunk_size=5,
                        max_workers=2, project="ssll:p")
    assert out["requests"] == 3 and len(out["results"]) == 12
    assert sorted(len(r["json"]["subjects"]) for r in server.requests) == [2, 5, 5]
    assert out["cost_usd"] == pytest.approx(12 * 500_000 * 0.042 / 1e6)
    assert out["chunk_failures"] == []


def test_judge_many_aborts_after_first_failure(server):
    server.responses = [(403, {"detail": "policy: 不出境"}, 0)]
    with pytest.raises(jc.JudgePolicyRefused):
        jc.judge_many("feature_questions_v0_1", _subjects(12), chunk_size=5,
                      max_workers=1, project="ssll:p")
    assert len(server.requests) == 1


def test_judge_many_keeps_partial_success(server):
    server.responses = [(200, None, 0), (422, {"detail": "bad"}, 0)]
    out = jc.judge_many("feature_questions_v0_1", _subjects(12), chunk_size=5,
                        max_workers=1, project="ssll:p")
    assert out["requests"] == 1 and len(out["results"]) == 5
    kinds = [f["kind"] for f in out["chunk_failures"]]
    assert kinds == ["bad_request", "aborted"]
    assert len(server.requests) == 2


def test_skip_record_shape():
    rec = jc.skip_record(jc.JudgePolicyRefused("policy: x"))
    assert rec["status"] == "skipped" and rec["reason"] == "policy_blocked"
    assert rec["http_status"] == 403
