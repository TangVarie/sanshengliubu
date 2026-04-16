"""Supabase database client — singleton wrapper with CRUD for all tables."""

from __future__ import annotations

import logging
import streamlit as st
from supabase import create_client, Client
from datetime import datetime, timezone
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class _StageLogsList(list):
    """list subclass carrying a `.partial` flag. get_stage_logs returns this
    so the UI can detect when the DB slim-query fallback kicked in and
    show a warning instead of rendering an empty stage output silently.
    Backward compatible — iterates exactly like a plain list.
    """

    partial: bool = False


class SupabaseClient:
    _instance: SupabaseClient | None = None

    @classmethod
    def get_instance(cls) -> SupabaseClient:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        url = st.secrets["SUPABASE_URL"]
        key = st.secrets["SUPABASE_KEY"]
        self.client: Client = create_client(url, key)

    # ── Projects ───────────────────────────────────────────────────────

    def create_project(
        self,
        name: str,
        free_text: str,
        task_type: str = "new_system",
        base_project_id: str | None = None,
        created_by: str = "default",
    ) -> dict:
        data: dict[str, Any] = {
            "name": name,
            "free_text": free_text,
            "task_type": task_type,
            "created_by": created_by,
            "status": "draft",
        }
        if base_project_id:
            data["base_project_id"] = base_project_id
        resp = self.client.table("projects").insert(data).execute()
        return resp.data[0]

    def get_project(self, project_id: str) -> dict:
        resp = self.client.table("projects").select("*").eq("id", project_id).single().execute()
        return resp.data

    def list_projects(self, limit: int = 50, offset: int = 0) -> list[dict]:
        resp = (
            self.client.table("projects")
            .select("*")
            .order("created_at", desc=True)
            .range(offset, offset + limit - 1)
            .execute()
        )
        return resp.data

    def update_project(self, project_id: str, **fields) -> dict:
        fields["updated_at"] = _now()
        resp = self.client.table("projects").update(fields).eq("id", project_id).execute()
        return resp.data[0]

    def delete_project(self, project_id: str) -> None:
        """Delete a project and all associated runs/logs/outputs (CASCADE)."""
        self.client.table("projects").delete().eq("id", project_id).execute()

    # ── Pipeline Runs ──────────────────────────────────────────────────

    def create_pipeline_run(self, project_id: str) -> dict:
        resp = (
            self.client.table("pipeline_runs")
            .insert({"project_id": project_id, "status": "running", "started_at": _now()})
            .execute()
        )
        return resp.data[0]

    def update_pipeline_run(self, run_id: str, **fields) -> dict:
        resp = self.client.table("pipeline_runs").update(fields).eq("id", run_id).execute()
        return resp.data[0]

    def get_pipeline_run(self, run_id: str) -> dict:
        resp = self.client.table("pipeline_runs").select("*").eq("id", run_id).single().execute()
        return resp.data

    def get_runs_for_project(self, project_id: str) -> list[dict]:
        resp = (
            self.client.table("pipeline_runs")
            .select("*")
            .eq("project_id", project_id)
            .order("created_at", desc=True)
            .execute()
        )
        return resp.data

    # ── Stage Logs ─────────────────────────────────────────────────────

    def create_stage_log(self, run_id: str, stage_name: str, input_data: dict | None = None) -> dict:
        resp = (
            self.client.table("stage_logs")
            .insert({
                "run_id": run_id,
                "stage_name": stage_name,
                "status": "running",
                "input_data": input_data,
            })
            .execute()
        )
        return resp.data[0]

    def update_stage_log(self, log_id: str, **fields) -> dict:
        fields["updated_at"] = _now()
        resp = self.client.table("stage_logs").update(fields).eq("id", log_id).execute()
        return resp.data[0]

    def get_stage_logs(
        self, run_id: str, stage_name: str | None = None
    ) -> _StageLogsList:
        """Load stage_logs for a run, EXCLUDING input_data to keep payload small.

        input_data contains the full shared_skeleton + ministry_outputs +
        cell_plans for every builder/cell_planner call — easily 50-100KB per
        entry. With dozens of retries, total payload exceeds Supabase free
        tier connection limits (httpx.ReadError). output_data (which has the
        agent's response) is much smaller and sufficient for UI rendering.

        Falls back to a lighter query (no output_data) if the first attempt
        fails due to connection issues — at least the UI can show status.
        The returned list carries `.partial = True` when the fallback
        triggered, so the UI can surface a warning instead of rendering
        empty stage outputs silently.

        Args:
            run_id: Pipeline run ID.
            stage_name: Optional filter — only return logs for this stage.
                        When set, avoids loading all stages into memory.
        """
        select_full = (
            "id, run_id, stage_name, status, output_data, error_message, "
            "model_used, tokens_used, duration_seconds, human_intervention, "
            "created_at, updated_at"
        )
        select_light = (
            "id, run_id, stage_name, status, error_message, "
            "model_used, tokens_used, duration_seconds, "
            "created_at, updated_at"
        )
        for attempt, columns in enumerate([select_full, select_light]):
            try:
                query = (
                    self.client.table("stage_logs")
                    .select(columns)
                    .eq("run_id", run_id)
                )
                if stage_name:
                    query = query.eq("stage_name", stage_name)
                resp = query.order("created_at").execute()
                result = _StageLogsList(resp.data or [])
                result.partial = attempt > 0
                if attempt > 0:
                    logging.getLogger(__name__).warning(
                        "get_stage_logs: full query failed, using lightweight "
                        "fallback (no output_data). Stage outputs won't render."
                    )
                return result
            except Exception:
                if attempt == 0:
                    continue  # try lighter query
                raise  # both failed

    def get_stage_log_by_id(self, log_id: str) -> dict | None:
        resp = self.client.table("stage_logs").select("*").eq("id", log_id).execute()
        return resp.data[0] if resp.data else None

    def get_stage_log_by_name(self, run_id: str, stage_name: str) -> dict | None:
        resp = (
            self.client.table("stage_logs")
            .select("*")
            .eq("run_id", run_id)
            .eq("stage_name", stage_name)
            .execute()
        )
        return resp.data[0] if resp.data else None

    def delete_stage_logs_by_names(self, run_id: str, stage_names: list[str]) -> int:
        """Delete stage_logs matching any of the given stage_names in this run.
        Used by the revision flow to force-rerun specific stages."""
        if not stage_names:
            return 0
        resp = (
            self.client.table("stage_logs")
            .delete()
            .eq("run_id", run_id)
            .in_("stage_name", stage_names)
            .execute()
        )
        return len(resp.data or [])

    # ── Outputs ────────────────────────────────────────────────────────

    def save_output(self, run_id: str, prompt_system: dict, final_review: dict, version: int = 1) -> dict:
        resp = (
            self.client.table("outputs")
            .insert({
                "run_id": run_id,
                "prompt_system": prompt_system,
                "final_review": final_review,
                "version": version,
            })
            .execute()
        )
        return resp.data[0]

    def get_output(self, run_id: str) -> dict | None:
        resp = self.client.table("outputs").select("*").eq("run_id", run_id).execute()
        return resp.data[0] if resp.data else None

    # ── Reference Samples ───────────────────────────────────────────

    def save_reference_sample(self, title: str, source_type: str, content_text: str, analysis: dict) -> dict:
        resp = (
            self.client.table("reference_samples")
            .insert({
                "title": title,
                "source_type": source_type,
                "content_text": content_text,
                "analysis": analysis,
            })
            .execute()
        )
        return resp.data[0]

    def list_reference_samples(self, limit: int = 50) -> list[dict]:
        resp = (
            self.client.table("reference_samples")
            .select("id, title, source_type, content_text, analysis, created_at")
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        return resp.data or []

    def delete_reference_sample(self, sample_id: str) -> None:
        self.client.table("reference_samples").delete().eq("id", sample_id).execute()

    def get_reference_samples_by_ids(self, ids: list[str]) -> list[dict]:
        if not ids:
            return []
        resp = (
            self.client.table("reference_samples")
            .select("*")
            .in_("id", ids)
            .execute()
        )
        return resp.data or []

    # ── Outputs ────────────────────────────────────────────────────

    def get_latest_output_for_project(self, project_id: str) -> dict | None:
        resp = (
            self.client.table("pipeline_runs")
            .select("id")
            .eq("project_id", project_id)
            .eq("status", "completed")
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        if not resp.data:
            return None
        run_id = resp.data[0]["id"]
        return self.get_output(run_id)
