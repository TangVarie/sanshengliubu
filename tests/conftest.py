"""pytest 公共夹具。

- 仓库根加进 sys.path(本仓没有打包,pipeline/ 按顶层包 import)。
- 判定服务相关的配置一律从干净状态开始:删掉 JUDGE_URL / JUDGE_API_KEY 环境变量,
  并让 secrets_compat 读不到 st.secrets —— 开发机上若有 .streamlit/secrets.toml
  配了 JUDGE_URL,测试也不会真的去连它。
- FakeDB:只记录 stage_log 的写入,其余查询返回空,够 orchestrator 的影子阶段用。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _clean_judge_env(monkeypatch):
    monkeypatch.delenv("JUDGE_URL", raising=False)
    monkeypatch.delenv("JUDGE_API_KEY", raising=False)
    import utils.secrets_compat as sc

    monkeypatch.setattr(sc, "_from_streamlit", lambda key: sc._MISSING)
    yield


class FakeDB:
    """记录型假库。stage_logs 按写入顺序存,`logs(name)` 取某个名字的全部行。"""

    def __init__(self):
        self.stage_logs: list[dict] = []
        self.calls: list[tuple] = []

    # ── stage_log ──
    def create_stage_log(self, run_id, stage_name, input_data=None):
        row = {"id": f"log{len(self.stage_logs) + 1}", "run_id": run_id,
               "stage_name": stage_name, "input_data": input_data,
               "status": "pending", "output_data": None}
        self.stage_logs.append(row)
        self.calls.append(("create_stage_log", stage_name))
        return row

    def update_stage_log(self, log_id, **fields):
        for row in self.stage_logs:
            if row["id"] == log_id:
                row.update(fields)
        self.calls.append(("update_stage_log", log_id))

    def get_stage_logs(self, run_id, stage_name=None):
        return [r for r in self.stage_logs
                if stage_name is None or r["stage_name"] == stage_name]

    def logs(self, name):
        return [r for r in self.stage_logs if r["stage_name"] == name]

    # ── 其余 orchestrator 会碰到的查询 ──
    def get_pipeline_run(self, run_id):
        self.calls.append(("get_pipeline_run", run_id))
        return {"id": run_id, "status": "running"}

    def get_relevant_reference_packs(self, **kw):
        return []

    def update_pipeline_run(self, *a, **kw):
        self.calls.append(("update_pipeline_run", a, kw))


@pytest.fixture
def fake_db():
    return FakeDB()


@pytest.fixture
def orch(fake_db):
    from pipeline.orchestrator import PipelineOrchestrator

    o = PipelineOrchestrator("proj-1", "run-1", fake_db)
    o._direction_index = {}
    o._cell_plan_index = {}
    return o
