"""流水线启动分发层 —— thread(进程内线程)/ worker(独立执行进程)双模式。

Railway 迁移的控制面出入口:pages/ 不再直接调 orchestrator 的
start/resume/revise,统一走这里。

  - thread 模式(默认):保持历史行为 —— 在当前进程里起 daemon 线程执行。
    本地开发、单进程 Streamlit Cloud 部署用这个,零依赖。
  - worker 模式(PIPELINE_EXECUTION_MODE=worker):UI 只把 run 写成
    status='pending' + queued_action 入队,由独立的 worker 进程
    (`python -m worker`,部署在 Railway 等常驻环境)认领执行。
    Web 容器重启/重发布不再杀死在跑的流水线。

worker 模式需要 pipeline_runs.queued_action 列(db/migrations/007)。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from utils.secrets_compat import get_secret

if TYPE_CHECKING:  # 仅类型标注;运行时由调用方传入实例,不强加导入依赖
    from db.supabase_client import SupabaseClient

logger = logging.getLogger(__name__)


def execution_mode() -> str:
    """当前执行模式:'thread'(默认)或 'worker'。"""
    mode = str(get_secret("PIPELINE_EXECUTION_MODE", "thread") or "thread")
    mode = mode.strip().lower()
    return mode if mode in ("thread", "worker") else "thread"


def launch_new_run(project_id: str, db: "SupabaseClient") -> dict:
    """新建一条 run 并启动(或入队)。返回 run 行。

    thread 模式沿用旧行为:create(status=running)→claim→起线程。
    worker 模式:入队前先查同项目有没有 pending/running 的 run ——
    队列语义下重复入队不会被 claim 挡住(会排队依次全跑一遍,重复烧钱),
    所以这里前置防重;真正的互斥仍由 worker 认领时的项目 CAS 保证。
    """
    from pipeline.orchestrator import (
        PipelineAlreadyRunningError,
        start_pipeline_in_background,
    )

    if execution_mode() == "worker":
        if db.has_active_run(project_id):
            raise PipelineAlreadyRunningError(
                f"项目 {project_id[:8]} 已有排队或在跑的 run,不重复入队。"
            )
        run = db.create_pipeline_run(
            project_id, status="pending", queued_action="start"
        )
        logger.info("[launcher] run %s 已入队(start)", run["id"][:8])
        return run

    run = db.create_pipeline_run(project_id)
    start_pipeline_in_background(project_id, run["id"], db)
    return run


def launch_resume(project_id: str, run_id: str, db: "SupabaseClient") -> None:
    """恢复执行一条 failed / needs_revision 的 run(或入队等待 worker)。"""
    from pipeline.orchestrator import (
        PipelineAlreadyRunningError,
        resume_pipeline_in_background,
    )

    if execution_mode() == "worker":
        ok = db.try_enqueue_run(
            run_id,
            action="resume",
            from_statuses=("failed", "needs_revision"),
        )
        if not ok:
            raise PipelineAlreadyRunningError(
                f"run {run_id[:8]} 当前状态不可恢复(可能已在排队/在跑,"
                f"或已完成)。刷新页面查看最新状态。"
            )
        logger.info("[launcher] run %s 已入队(resume)", run_id[:8])
        return

    resume_pipeline_in_background(project_id, run_id, db)


def launch_revise(project_id: str, run_id: str, db: "SupabaseClient") -> None:
    """应用终审修订意见并重跑(或入队)。

    修订的准备工作(项目 CAS、写 _revision_context、删下游 stage_logs)
    是纯 DB 操作,两种模式都在当前进程完成 —— 它必须原子地抢到项目锁才能
    安全删除。区别只在最后一步:thread 起线程,worker 置 pending 交给
    worker 认领(项目锁已被本 run 持有,故 queued_action=resume_preclaimed)。
    """
    from pipeline.orchestrator import revise_and_resume_pipeline_in_background

    revise_and_resume_pipeline_in_background(
        project_id, run_id, db,
        enqueue_only=(execution_mode() == "worker"),
    )
