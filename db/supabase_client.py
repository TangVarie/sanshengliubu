"""Supabase database client — singleton wrapper with CRUD for all tables."""

from __future__ import annotations

import logging
import streamlit as st
from supabase import create_client, Client
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


_WRITE_RETRIES = 2
_WRITE_BACKOFF_SECONDS = (0.4, 1.0)


def _with_write_retry(fn):
    """Run a DB write with bounded retry on transient network errors. The
    write methods had NO retry: a single httpx.ReadError on update_stage_log
    would bubble up — and in the agent retry loop it was even misclassified
    as a model failure and re-ran an expensive LLM call. Wrapping the write
    itself keeps a transient blip from becoming a run failure / re-billed
    call. Non-network errors (RLS, 4xx) are NOT retried — they won't heal."""
    import time as _time

    import httpx

    _TRANSIENT = (
        httpx.ReadError,
        httpx.ReadTimeout,
        httpx.ConnectError,
        httpx.RemoteProtocolError,
        httpx.PoolTimeout,
        httpx.WriteError,
    )
    last: Exception | None = None
    for i in range(1 + _WRITE_RETRIES):
        try:
            return fn()
        except _TRANSIENT as e:
            last = e
            if i < _WRITE_RETRIES:
                _time.sleep(
                    _WRITE_BACKOFF_SECONDS[min(i, len(_WRITE_BACKOFF_SECONDS) - 1)]
                )
                continue
            raise
    if last is not None:  # pragma: no cover — loop always returns/raises
        raise last


def _parse_ts(raw: Any) -> datetime | None:
    """Parse a Supabase ISO timestamp into an aware datetime, or None.
    Tolerant of trailing 'Z' and missing tz (assumes UTC)."""
    if not raw:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    try:
        s = str(raw).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _first_row(resp, *, op: str, table: str) -> dict:
    """Return resp.data[0] or raise a diagnostic error.

    Supabase insert/update normally returns the affected row. An empty
    resp.data means the write hit zero rows (RLS rejected, id mismatch,
    network partial) — in that case `resp.data[0]` would IndexError
    several frames up in unrelated code. Raise a clear error here so the
    UI error banner shows the actual cause.
    """
    if not resp.data:
        raise RuntimeError(
            f"Supabase {op} on '{table}' returned no rows. "
            "Likely causes: row-level-security rejected the write, the "
            "target id no longer exists, or the connection dropped mid-call."
        )
    return resp.data[0]


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
        return _first_row(resp, op="insert", table="projects")

    def get_project(self, project_id: str) -> dict:
        resp = self.client.table("projects").select("*").eq("id", project_id).single().execute()
        return resp.data

    def list_projects(
        self, limit: int = 50, offset: int = 0, light: bool = False
    ) -> list[dict]:
        # light=True selects only the columns the dashboard renders — avoids
        # dragging every project's full free_text / brief (which can carry
        # base64 screenshots, hundreds of KB each) across the wire just to
        # show name + status. Big win when the list refreshes on every load.
        cols = (
            "id, name, status, task_type, created_at"
            if light else "*"
        )
        resp = (
            self.client.table("projects")
            .select(cols)
            .order("created_at", desc=True)
            .range(offset, offset + limit - 1)
            .execute()
        )
        return resp.data

    _PROJECT_UPDATABLE_FIELDS = frozenset({
        "name", "status", "task_type", "brief", "free_text",
        "base_project_id", "updated_at",
    })

    def update_project(self, project_id: str, **fields) -> dict:
        unknown = set(fields) - self._PROJECT_UPDATABLE_FIELDS
        if unknown:
            raise ValueError(
                f"update_project: refusing to write unknown field(s) {sorted(unknown)}. "
                f"Allowed: {sorted(self._PROJECT_UPDATABLE_FIELDS)}"
            )
        fields["updated_at"] = _now()
        # Write-retry: update_project carries the brief overwrite (crown_prince
        # output, revision context, etc.). A transient httpx blip here must not
        # bubble up and fail the whole run — mirror update_pipeline_run.
        def _do():
            resp = self.client.table("projects").update(fields).eq("id", project_id).execute()
            return _first_row(resp, op="update", table="projects")
        return _with_write_retry(_do)

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
        return _first_row(resp, op="insert", table="pipeline_runs")

    def update_pipeline_run(self, run_id: str, **fields) -> dict:
        def _do():
            resp = self.client.table("pipeline_runs").update(fields).eq("id", run_id).execute()
            return _first_row(resp, op="update", table="pipeline_runs")
        return _with_write_retry(_do)

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

    def get_quality_score_history(
        self, project_id: str, *, limit: int = 10, exclude_run_id: str | None = None
    ) -> list[dict]:
        """取同一项目最近若干条 run 的质量分(stage_name='quality_score')。

        回归哨兵用:拿当前 run 的分数跟历史比,掉出噪声带就报警。
        单条 run 的分数没有意义 —— 只有跟自己的历史比才知道是涨是跌。

        返回按时间倒序的 output_data 列表(已过滤掉空的)。取不到就返回 []
        —— 哨兵是可观测性,查询失败不能影响出货。

        为什么不做成一次 join:PostgREST 的嵌套查询在跨表过滤 + 排序时行为
        不稳定(而且 stage_logs 的 output_data 很大)。分两步查、只取 run id
        列表再按 in_ 过滤,语义确定、payload 可控。
        """
        try:
            # ⚠️ 不能先 `[:limit]` 再查分数。失败 / 被取消 / 中断的 run 也占
            # 位置,它们没有 quality_score —— 先切片就等于把额度花在这些空 run
            # 上,查完过滤掉,更早的**有效**分数则永远进不来。一个项目连着挂
            # 几次之后,基线会永久凑不满 3 条,回归检测直接失效。
            #
            # 改成分页往前捞,直到集满 limit 条【真的有分数】的 run 为止。
            collected: list[dict] = []
            seen_runs: set[str] = set()
            offset, page = 0, max(limit * 3, 30)
            while len(collected) < limit and offset < 300:
                runs = (
                    self.client.table("pipeline_runs")
                    .select("id,created_at")
                    .eq("project_id", project_id)
                    .order("created_at", desc=True)
                    .range(offset, offset + page - 1)
                    .execute()
                ).data or []
                if not runs:
                    break
                offset += page
                run_ids = [
                    r["id"] for r in runs
                    if r.get("id") and r["id"] != exclude_run_id
                ]
                if not run_ids:
                    continue

                rows = (
                    self.client.table("stage_logs")
                    .select("run_id,created_at,output_data")
                    .eq("stage_name", "quality_score")
                    .in_("run_id", run_ids)
                    .order("created_at", desc=True)
                    .execute()
                ).data or []

                # ⚠️ 一条 run 可能有**多条** quality_score:失败重续和修订重跑
                # 复用同一个 run_id,而 persist_quality_score 每次都插新行。
                # 不去重的话,同一条 run 会在中位数里被重复加权,甚至靠自己
                # 几条记录就凑满 BASELINE_MIN_RUNS —— 基线变成"跟自己比"。
                # rows 已按时间倒序,每个 run_id 取第一条 = 最新那条。
                for r in rows:
                    rid = r.get("run_id")
                    od = r.get("output_data")
                    if not rid or rid in seen_runs:
                        continue
                    if not isinstance(od, dict) or not od.get("total_cells"):
                        continue
                    seen_runs.add(rid)
                    collected.append(od)
                    if len(collected) >= limit:
                        break
            return collected
        except Exception:
            logger.exception(
                "[quality_history] 查询失败(non-fatal),回归哨兵本轮跳过"
            )
            return []

    def try_claim_project_running(
        self, project_id: str, allowed_from: list[str] | None = None
    ) -> dict | None:
        """Atomically flip a project to 'running' IFF it isn't already running
        (or, when allowed_from is given, IFF its current status is in that
        set). Returns the updated project dict if we claimed it, else None.

        This is the authoritative gate against two clicks / two tabs racing
        to start the SAME project: a read-then-write guard has a TOCTOU
        window (the status only flips much later inside the run thread), so
        both racers pass and start two orchestrators that clobber each other.
        A conditional UPDATE ... WHERE status <> 'running' is atomic in
        Postgres — exactly one racer's update affects a row.
        """
        q = (
            self.client.table("projects")
            .update({"status": "running"})
            .eq("id", project_id)
        )
        if allowed_from:
            q = q.in_("status", list(allowed_from))
        else:
            q = q.neq("status", "running")
        resp = q.execute()
        rows = resp.data or []
        return rows[0] if rows else None

    def get_running_runs(self, limit: int = 200) -> list[dict]:
        resp = (
            self.client.table("pipeline_runs")
            .select("*")
            .eq("status", "running")
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        return resp.data or []

    # Statuses whose row a killed process strands as a permanent zombie.
    # 'paused_for_review' is included because a run waiting on human
    # clarification keeps ticking its heartbeat (the beat is an INDEPENDENT
    # daemon thread, not gated on stage progress) — so a *stale* heartbeat on
    # a paused run means the process died mid-wait (a zombie the reaper must
    # collect), while a *fresh* heartbeat means it is legitimately still
    # waiting for the user and must NOT be reaped (enforced by the cutoff
    # check in reap_stale_runs).
    _REAPABLE_STATUSES = ("running", "paused_for_review")

    def get_reapable_runs(self, limit: int = 200) -> list[dict]:
        resp = (
            self.client.table("pipeline_runs")
            .select("*")
            .in_("status", list(self._REAPABLE_STATUSES))
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        return resp.data or []

    def reap_stale_runs(self, stale_seconds: int) -> int:
        """Mark reapable runs ('running' or 'paused_for_review') whose
        heartbeat (or start time when heartbeat is absent, e.g. rows from
        before the column existed) is older than stale_seconds as failed.
        Returns the count reaped.

        This is the ONLY thing that cleans up zombie runs: when Streamlit
        Cloud SIGKILLs the process, the background thread dies without
        running its except/finally, so the DB stays 'running'/'paused' forever.
        Call this on app load. The heartbeat ticks every
        PIPELINE_HEARTBEAT_INTERVAL from an INDEPENDENT daemon while a run is
        genuinely alive — even while it is paused_for_review waiting on human
        clarification — so a stale heartbeat means the process is dead, not
        that a stage is slow or the user is slow to answer.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=stale_seconds)
        reaped = 0
        for run in self.get_reapable_runs():
            ts = _parse_ts(
                run.get("heartbeat_at")
                or run.get("started_at")
                or run.get("created_at")
            )
            if ts is not None and ts >= cutoff:
                continue  # heartbeat still fresh — genuinely alive, leave it
            rid = run.get("id")
            if not rid:
                continue
            _was_paused = run.get("status") == "paused_for_review"
            try:
                self.update_pipeline_run(
                    rid, status="failed", completed_at=_now()
                )
                if run.get("project_id"):
                    self.update_project(run["project_id"], status="failed")
                log = self.create_stage_log(
                    rid, "_pipeline_error", {"reaped_at": _now()}
                )
                if _was_paused:
                    _msg = (
                        "⛔ 这条 run 被自动收割:它停在 paused_for_review(等待"
                        "澄清补充)但心跳已停止"
                        f"（超过 {stale_seconds}s 无更新）,说明运行进程在等待期间"
                        "已被 Cloud 回收/重启,后台线程当场中断、无法再自动恢复。"
                        "状态已重置为 failed —— 若你此前已提交澄清答复,请点"
                        "「重跑流水线」重来并重新补充。"
                    )
                else:
                    _msg = (
                        "⛔ 这条 run 被自动收割:它标记为 running 但心跳已停止"
                        f"（超过 {stale_seconds}s 无更新）,说明运行进程已被 "
                        "Cloud 回收/重启,后台线程当场中断、来不及自己落库。"
                        "状态已重置为 failed,可以点「重跑流水线」重来。"
                    )
                self.update_stage_log(
                    log["id"], status="failed", error_message=_msg
                )
                reaped += 1
            except Exception as e:
                logger.warning("[reap] failed to reap run %s: %r", rid, e)
        if reaped:
            logger.info(
                "[reap] reaped %d stale (zombie) run(s) [running/paused]", reaped
            )
        return reaped

    # ── Stage Logs ─────────────────────────────────────────────────────

    def create_stage_log(self, run_id: str, stage_name: str, input_data: dict | None = None) -> dict:
        def _do():
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
            return _first_row(resp, op="insert", table="stage_logs")
        return _with_write_retry(_do)

    def update_stage_log(self, log_id: str, **fields) -> dict:
        fields["updated_at"] = _now()
        def _do():
            resp = self.client.table("stage_logs").update(fields).eq("id", log_id).execute()
            return _first_row(resp, op="update", table="stage_logs")
        return _with_write_retry(_do)

    def get_stage_logs(
        self, run_id: str, stage_name: str | None = None
    ) -> _StageLogsList:
        """Load stage_logs for a run, EXCLUDING input_data to keep payload small
        (except for the small `_batch_info` sub-object, projected out separately
        — see the comment on select_batchinfo below).

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
        # ── 为什么要单独捞 _batch_info ────────────────────────────────
        # 工部构建/格子规划一个 run 会产生几十条 stage_log(每个格子 1 条,
        # 加上 batch 重试和单 cell 重试)。pages/3 的 _batch_label() 本来设计
        # 成显示 "批次 15 · initial [D6_xiaohongshu]" 这种自带批次号 + 轮次 +
        # 格子 id 的标签,数据源是 input_data["_batch_info"]。
        #
        # 但上面那条"排除 input_data"的优化把整列去掉了,于是 _batch_label
        # 永远读不到 _batch_info,永远退回 fallback 的 "构建批次 {i+1}" ——
        # 一个跨所有调用(含重试)的流水号。用户看到 "构建批次 66" 会以为
        # 有 66 个格子,实际那是第 66 次调用。这条 label 分支从优化落地那天
        # 起就是死代码。
        #
        # 修法:不捞整个 input_data(几十 KB/条),只用 PostgREST 的 JSON 路径
        # 投影把 _batch_info 这一个小对象取出来,别名成 batch_info。
        select_batchinfo = (
            "id, run_id, stage_name, status, output_data, error_message, "
            "model_used, tokens_used, duration_seconds, human_intervention, "
            "created_at, updated_at, batch_info:input_data->_batch_info"
        )
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
        # Only fall back to the lighter query on network-ish errors that
        # plausibly go away if we drop output_data from the payload. Auth,
        # permission, and 4xx errors won't be fixed by selecting fewer
        # columns — propagate those so the caller sees the real cause.
        import httpx
        _FALLBACK_EXC = (
            httpx.ReadError,
            httpx.ReadTimeout,
            httpx.ConnectError,
            httpx.RemoteProtocolError,
            httpx.PoolTimeout,
        )
        import time as _time
        # 三级降级:带 _batch_info 投影 → 去掉投影 → 再去掉 output_data。
        # 第一级额外吞掉【任何】异常:JSON 路径投影是 PostgREST 的语法,万一
        # 部署的版本不认(或以后语法变了)会返回 4xx 而不是网络错误,那种情况
        # 下应该静默退回今天的行为,而不是把整个流水线详情页打挂 —— 标签好不
        # 好看远不如页面能打开重要。
        for attempt, columns in enumerate(
            [select_batchinfo, select_full, select_light]
        ):
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
                # partial 的语义是"output_data 没捞到,页面会渲染空" —— 只有
                # 退到第 3 级才成立。第 2 级只是少了 batch 标签,内容完整。
                result.partial = attempt >= 2
                if attempt == 1:
                    logging.getLogger(__name__).info(
                        "get_stage_logs: _batch_info JSON 投影不可用,批次标签"
                        "退回流水号(不影响内容渲染)"
                    )
                elif attempt >= 2:
                    logging.getLogger(__name__).warning(
                        "get_stage_logs: full query failed, using lightweight "
                        "fallback (no output_data). Stage outputs won't render."
                    )
                return result
            except Exception as _e:
                # 第一级(JSON 投影)对任何异常都降级 —— 见上面的注释。
                if attempt == 0:
                    logging.getLogger(__name__).info(
                        "get_stage_logs: 带 _batch_info 的查询失败(%s),降级重试",
                        type(_e).__name__,
                    )
                    continue
                if not isinstance(_e, _FALLBACK_EXC):
                    raise
                if attempt == 1:
                    # Brief pause so the second attempt doesn't hit the same
                    # TCP reset / server-side hiccup. 500ms is enough for most
                    # transient supabase pool blips without noticeably slowing
                    # the UI refresh path.
                    _time.sleep(0.5)
                    continue
                raise

    def get_stage_statuses(self, run_id: str) -> list[dict]:
        """Ultra-light status poll: only the columns needed for the progress
        row + liveness (NO output_data). Used by the auto-refresh watcher so
        the 3-second poll doesn't drag the full stage payload (matrix / posts
        / cell plans — hundreds of KB) across the wire every tick."""
        resp = (
            self.client.table("stage_logs")
            .select("id, stage_name, status, updated_at, created_at")
            .eq("run_id", run_id)
            .order("created_at")
            .execute()
        )
        return resp.data or []

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
        # Revision reruns REUSE the same run_id (revise_and_resume only deletes
        # downstream stage_logs, never the outputs row) and reach save_output
        # again. outputs(run_id) is NOT unique, so a plain insert left TWO rows
        # for one run_id — and get_output returned data[0] with no ordering, so
        # the STALE pre-revision output could win and the user would never see
        # the revised result (silent data loss). Delete any prior row for this
        # run_id first so exactly one (latest) row survives. Wrapped in
        # write-retry: this is the heaviest write in the pipeline (full
        # prompt_system jsonb) and a transient httpx blip must not fail the run.
        def _do():
            self.client.table("outputs").delete().eq("run_id", run_id).execute()
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
            return _first_row(resp, op="insert", table="outputs")
        return _with_write_retry(_do)

    def get_output(self, run_id: str) -> dict | None:
        # order+limit is defensive: even if legacy duplicate rows exist for a
        # run_id (from before save_output deduped), always return the latest.
        resp = (
            self.client.table("outputs")
            .select("*")
            .eq("run_id", run_id)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
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
        return _first_row(resp, op="insert", table="reference_samples")

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

    # ── Reference Pack (v2) ───────────────────────────────────────────
    # "证据包" schema: cover + title + body + top_comments + ai_analysis.
    # See db/migrations/005_reference_samples_v2.sql.

    _REFERENCE_PACK_UPDATABLE = frozenset({
        "title", "post_title", "post_body", "cover_image_b64",
        "top_comments", "platform", "category", "ai_analysis",
        "quality_score", "tags",
    })

    def save_reference_pack(self, pack: dict) -> dict:
        """Insert a v2 证据包. Required: post_title OR post_body, platform.
        Other fields optional. Returns the inserted row."""
        if not (pack.get("post_title") or pack.get("post_body")):
            raise ValueError("证据包至少要有 post_title 或 post_body。")
        payload = {
            "title": pack.get("title") or (pack.get("post_title") or "")[:80] or "未命名样本",
            "source_type": "pack",
            # legacy compat: mirror post_body into content_text so the old
            # list_reference_samples readers still see some content
            "content_text": pack.get("post_body") or "",
            "post_title": pack.get("post_title"),
            "post_body": pack.get("post_body"),
            "cover_image_b64": pack.get("cover_image_b64"),
            "top_comments": pack.get("top_comments") or [],
            "platform": pack.get("platform"),
            "category": pack.get("category"),
            "ai_analysis": pack.get("ai_analysis"),
            "quality_score": int(pack.get("quality_score") or 0),
            "tags": pack.get("tags") or [],
        }
        resp = self.client.table("reference_samples").insert(payload).execute()
        return _first_row(resp, op="insert", table="reference_samples")

    def update_reference_pack(self, sample_id: str, **fields) -> dict:
        """Partial-update a pack. Allowlisted to the v2 columns."""
        unknown = set(fields) - self._REFERENCE_PACK_UPDATABLE
        if unknown:
            raise ValueError(
                f"update_reference_pack: refusing unknown field(s) {sorted(unknown)}"
            )
        resp = (
            self.client.table("reference_samples")
            .update(fields)
            .eq("id", sample_id)
            .execute()
        )
        return _first_row(resp, op="update", table="reference_samples")

    def list_reference_packs(
        self,
        *,
        platform: str | None = None,
        category: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """List v2 packs, most-recent first (with quality-score tiebreak).
        Empty filters return all packs."""
        q = (
            self.client.table("reference_samples")
            .select(
                "id, title, post_title, post_body, cover_image_b64, "
                "top_comments, platform, category, ai_analysis, "
                "quality_score, tags, created_at, "
                # 必须选出这列:_shape_for_rewriter 用它算 source_type
                # (非空=飞轮 TV 样本,空=manual)。漏选会让所有样本恒判 manual、
                # R-022 飞轮审计 tv_synced 恒为 0。
                "source_truth_vault_note_id"
            )
            .eq("source_type", "pack")
        )
        if platform:
            q = q.eq("platform", platform)
        if category:
            q = q.eq("category", category)
        resp = (
            q.order("quality_score", desc=True)
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        return resp.data or []

    def get_relevant_reference_packs(
        self,
        platform: str,
        category: str | None = None,
        limit: int = 6,
    ) -> list[dict]:
        """Retrieval helper for vibe_loop. Priority:
          1. exact platform + exact category match (best)
          2. exact platform match (category mismatch or missing)
          3. nothing else — do NOT cross platforms (小红书 vs 抖音 vibe
             diverges too much to be useful as reference)
        Within each tier, order by quality_score DESC, created_at DESC.
        """
        if not platform:
            return []
        tier_a = self.list_reference_packs(
            platform=platform, category=category, limit=limit,
        ) if category else []
        if len(tier_a) >= limit:
            return tier_a[:limit]
        # Fill remaining slots from platform-only pool, dedup by id
        seen = {r["id"] for r in tier_a}
        tier_b = [
            r for r in self.list_reference_packs(platform=platform, limit=limit)
            if r["id"] not in seen
        ]
        return (tier_a + tier_b)[:limit]

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
