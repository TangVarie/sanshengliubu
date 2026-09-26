"""批量采样:正文全文落库(判定服务要判的就是它)。"""

from __future__ import annotations

import asyncio

import pipeline.agents as agents
import pipeline.agents.kimi_client as kc
from pipeline import batch_sampler, jev_shadow


def test_sample_one_cell_keeps_full_body(monkeypatch):
    bodies = {}

    def fake_text(system_prompt, user_message, **kw):
        seed = user_message.split("差异化种子：")[-1].split("\n")[0]
        text = f"周三晚上在出差酒店,我拆开第{seed}个快递,当场愣住三秒。\n第二段写得很长" + "。" * 3
        bodies[seed] = text
        return {"text": text, "input_tokens": 10, "output_tokens": 20, "cost_usd": 0.001,
                "model": "kimi-k2.6"}

    monkeypatch.setattr(kc, "call_kimi_text", fake_text)
    monkeypatch.setattr(agents, "_get_active_limiter", lambda: None)
    cell = {"cell_id": "D1_xhs", "platform": "小红书", "system_prompt": "你是……"}

    rep = asyncio.run(batch_sampler.sample_one_cell(cell, {}, 3, asyncio.Semaphore(3)))
    assert rep["status"] == "ok"
    for ps in rep["per_sample"]:
        assert ps["body"] == bodies[str(ps["seed"])]
        assert ps["chars"] == len(ps["body"])
        assert ps["opening"] and len(ps["opening"]) <= 60

    # orchestrator 这一层拼账本 id:<run_id>:<cell>:<代际>:<seed>
    sampling = {"status": "ok", "per_cell": [rep]}
    assert jev_shadow.assign_sample_subject_ids(sampling, "run-9", "g20260924T000000Z") == 3
    assert [ps["subject_id"] for ps in rep["per_sample"]] == [
        "run-9:D1_xhs:g20260924T000000Z:1",
        "run-9:D1_xhs:g20260924T000000Z:21",
        "run-9:D1_xhs:g20260924T000000Z:41",
    ]
