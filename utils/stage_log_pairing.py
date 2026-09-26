"""详情页用:把「跟在某条 stage_log 后面写的伴生记录」配回它所属的那一轮。

网感循环每一轮先写 `vibe_critic`(BaseAgent.run 在调用前建 log、返回前写
output_data),然后才在 orchestrator 里算二审仲裁、判定服务影子,各自另写一条
`vibe_arbitration` / `jev_critic_shadow`。伴生记录没法拿到 vibe_critic 那条 log 的
id,所以按时间顺序配:落在第 i 条锚点之后、第 i+1 条锚点之前的同名记录属于第 i 轮。
get_stage_logs 按 created_at 升序返回,本函数不再排序。

纯函数,不碰 Streamlit、不碰 DB,测试直接调。
"""

from __future__ import annotations

from typing import Iterable


def pair_following(
    logs: Iterable[dict], anchor: str, companions: Iterable[str]
) -> list[tuple[dict, dict[str, dict]]]:
    """返回 [(锚点 log, {伴生名: 该轮第一条同名 log}), ...],顺序同锚点出现顺序。

    锚点之前(第一条锚点之前)出现的伴生记录丢弃 —— 那是上一次跑留下的或者
    写乱了顺序,配给谁都不对。某一轮缺的伴生名不出现在 dict 里。
    """
    names = set(companions)
    out: list[tuple[dict, dict[str, dict]]] = []
    for log in logs:
        name = log.get("stage_name")
        if name == anchor:
            out.append((log, {}))
        elif name in names and out and name not in out[-1][1]:
            out[-1][1][name] = log
    return out


def second_review_summary(arbitration_output: dict | None) -> dict:
    """把 `vibe_arbitration` 的 output_data 摘成详情页要显示的几项(缺字段都给默认值)。"""
    out = arbitration_output or {}
    sr = out.get("second_review") or {}
    verdict = sr.get("verdict") or "unknown"
    added = list(out.get("second_review_added") or [])
    failed = sr.get("failed_cells") or []
    directives = {
        f.get("cell_id"): (f.get("rewrite_directives") or "")
        for f in failed if isinstance(f, dict) and f.get("cell_id")
    }
    return {
        "round": out.get("round"),
        "ran": verdict not in ("skipped", "not_run", "unknown"),
        "verdict": verdict,
        "skip_reason": sr.get("_skip_reason"),
        "main_passed": list(out.get("main_critic_passed") or []),
        "added": added,
        "added_directives": {cid: directives.get(cid, "") for cid in added},
        "reviews": [r for r in (sr.get("cell_reviews") or []) if isinstance(r, dict)],
        "usage": sr.get("_gemini_usage") or {},
        "prose_forced": list((out.get("prose_gate") or {}).get("forced") or []),
        "prose_soft": dict((out.get("prose_gate") or {}).get("soft") or {}),
    }
