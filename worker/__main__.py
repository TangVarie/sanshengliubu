"""worker 主循环 —— `python -m worker`。

环境变量(全部可选,除非另注):
  PIPELINE_EXECUTION_MODE      Web 侧必须设为 worker 才会入队;worker 进程
                               本身不读该值(它只消费队列)。
  WORKER_POLL_SECONDS          队列轮询间隔,默认 5。
  WORKER_MAX_CONCURRENT_RUNS   同时执行的 run 数,默认 1(项目级互斥仍由
                               CAS 保证;多 run 共享进程内 limiter/budget)。
  WORKER_ZOMBIE_SCAN_SECONDS   僵尸扫描间隔,默认 30。
  WORKER_AUTO_RESUME           1(默认)=自动重排队心跳过期的僵尸 run;
                               0=只执行队列,不做恢复(留给页面 reaper)。
  另需 SUPABASE_URL / SUPABASE_KEY / MOONSHOT_API_KEY 等业务密钥,
  见 .streamlit/secrets.toml.example 与 docs/railway-deploy.md。
"""

from __future__ import annotations

import logging
import os
import signal
import threading
import time
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("worker")

_STOP = threading.Event()
_FORCE_EXIT = threading.Event()


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


POLL_SECONDS = _env_int("WORKER_POLL_SECONDS", 5)
MAX_CONCURRENT = _env_int("WORKER_MAX_CONCURRENT_RUNS", 1)
ZOMBIE_SCAN_SECONDS = _env_int("WORKER_ZOMBIE_SCAN_SECONDS", 30)
AUTO_RESUME = os.environ.get("WORKER_AUTO_RESUME", "1").strip() != "0"


def _handle_sigterm(signum, frame):  # noqa: ARG001
    if _STOP.is_set():
        # 第二次信号:立即退出(daemon 线程随进程终止,僵尸扫描接手)。
        logger.warning("[worker] 再次收到停止信号,立即退出")
        _FORCE_EXIT.set()
        raise SystemExit(1)
    logger.info(
        "[worker] 收到停止信号:不再认领新任务,等待在跑 run 到平台宽限期。"
        "在跑 run 若被强杀,心跳停跳后会被下一个实例自动接续。"
    )
    _STOP.set()


def _assert_schema_ready(db) -> None:
    """worker 模式依赖 pipeline_runs.queued_action(migration 007)。
    缺列时立刻响亮失败,而不是在第一次入队/认领时抛晦涩的 42703。"""
    try:
        db.client.table("pipeline_runs").select("queued_action").limit(1).execute()
    except Exception as e:  # noqa: BLE001
        raise SystemExit(
            "pipeline_runs.queued_action 列不存在 —— 请先在 Supabase 执行 "
            "db/migrations/007_worker_queue.sql(或重跑 db/schema.sql)。"
            f"原始错误: {e!r}"
        ) from e


def _requeue_zombies(db, active_run_ids: set[str], stale_seconds: int) -> int:
    """把心跳过期的 running/paused 僵尸 run 重排队(条件 CAS,不误杀活体)。

    与页面 reaper(把僵尸标 failed、等人手动继续)相比,worker 的恢复语义
    是自动接续 —— 这正是迁 durable worker 的核心收益(审计 ROB-001)。
    两者可共存:条件更新谁先命中谁生效,都不会碰心跳新鲜的活 run。
    """
    # 容忍多种时间戳格式的解析(worker 侧预筛,真正的判定在 DB 条件里)。
    from db.supabase_client import _parse_ts

    cutoff = datetime.now(timezone.utc) - timedelta(seconds=stale_seconds)
    cutoff_iso = cutoff.isoformat()
    requeued = 0
    try:
        candidates = db.get_reapable_runs(limit=50)
    except Exception:
        logger.warning("[worker] 僵尸扫描读取失败(下轮再试)", exc_info=True)
        return 0
    for run in candidates:
        rid = run.get("id")
        status = run.get("status")
        if not rid or rid in active_run_ids:
            continue
        hb = _parse_ts(run.get("heartbeat_at"))
        if hb is None or hb >= cutoff:
            # 无心跳的老行留给页面 reaper(它有 started_at 兜底);
            # 心跳新鲜的是活体,不碰。
            continue
        try:
            if db.try_requeue_zombie_run(rid, cutoff_iso, from_status=status):
                requeued += 1
                logger.warning(
                    "[worker] 僵尸 run %s(%s,心跳停于 %s)已重排队,将自动接续",
                    rid[:8], status, run.get("heartbeat_at"),
                )
        except Exception:
            logger.warning("[worker] 重排队 %s 失败(下轮再试)", rid[:8], exc_info=True)
    return requeued


def _try_dispatch_one(db, active: list[dict]) -> bool:
    """从队列认领一条可执行的 run 并启动。返回是否有认领动作。"""
    from pipeline.orchestrator import (
        PipelineAlreadyRunningError,
        start_pipeline_in_background,
    )

    try:
        pending = db.get_pending_runs(limit=10)
    except Exception:
        logger.warning("[worker] 读取队列失败(下轮再试)", exc_info=True)
        return False

    active_run_ids = {a["run_id"] for a in active}
    active_project_ids = {a["project_id"] for a in active}

    for run in pending:
        rid = run.get("id")
        pid = run.get("project_id")
        action = (run.get("queued_action") or "start").strip()
        if not rid or not pid:
            continue
        if rid in active_run_ids or pid in active_project_ids:
            continue  # 同项目串行;本进程已在跑的跳过

        # 先 CAS 认领(pending→running+心跳),赢不到就是别人拿走/被取消。
        if not db.try_claim_pending_run(rid):
            continue

        # resume_preclaimed:项目锁已被该 run 持有(revise 入队/僵尸复活),
        # 不再重复抢占;其余动作走正常项目 CAS。
        claim = action != "resume_preclaimed"
        try:
            thread = start_pipeline_in_background(pid, rid, db, claim=claim)
        except PipelineAlreadyRunningError:
            # 项目被占(同项目另一 run 在跑)→ 放回队列排队等待。
            db.try_unclaim_run(rid, action=action)
            logger.info(
                "[worker] run %s 的项目 %s 被占,放回队列等待", rid[:8], pid[:8]
            )
            continue
        except Exception:
            logger.exception("[worker] 启动 run %s 失败,标记 failed", rid[:8])
            try:
                db.update_pipeline_run(
                    rid,
                    status="failed",
                    completed_at=datetime.now(timezone.utc).isoformat(),
                )
            except Exception:
                logger.warning("[worker] 标记 failed 也失败(留给 reaper)")
            continue

        active.append({"thread": thread, "run_id": rid, "project_id": pid})
        logger.info("[worker] 已认领 run %s(action=%s,claim=%s)", rid[:8], action, claim)
        return True
    return False


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # 日志脱敏必须先装(与 app.py 同一道防线):worker 的 stdout 会进
    # Railway 日志,traceback 里的 key/token 一样要剥。
    from pipeline.logger_utils import install_secret_masking_on_root_logger

    install_secret_masking_on_root_logger()

    from db.supabase_client import SupabaseClient
    from pipeline.agents import init_api_config
    from pipeline.config import RUN_STALE_SECONDS, VERSION

    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGINT, _handle_sigterm)

    db = SupabaseClient.get_instance()
    _assert_schema_ready(db)
    init_api_config()

    logger.info(
        "[worker] 启动 %s · poll=%ss · concurrent=%s · zombie_scan=%ss · auto_resume=%s",
        VERSION, POLL_SECONDS, MAX_CONCURRENT, ZOMBIE_SCAN_SECONDS, AUTO_RESUME,
    )

    active: list[dict] = []
    last_zombie_scan = 0.0

    while not _STOP.is_set():
        # 收割已结束的执行线程
        active = [a for a in active if a["thread"].is_alive()]

        # 僵尸恢复(启动即扫一次 → "进程起来就恢复",不等页面访问)
        now = time.monotonic()
        if AUTO_RESUME and now - last_zombie_scan >= ZOMBIE_SCAN_SECONDS:
            last_zombie_scan = now
            _requeue_zombies(db, {a["run_id"] for a in active}, RUN_STALE_SECONDS)

        # 认领:一轮尽量填满并发额度
        while len(active) < MAX_CONCURRENT and not _STOP.is_set():
            if not _try_dispatch_one(db, active):
                break

        _STOP.wait(POLL_SECONDS)

    # ── 优雅停机:等待在跑线程(平台宽限期内能跑完多少是多少)──
    active = [a for a in active if a["thread"].is_alive()]
    if active:
        logger.info(
            "[worker] 停机等待 %d 条在跑 run:%s(被强杀的部分心跳停跳后"
            "由下一实例自动接续)",
            len(active), ", ".join(a["run_id"][:8] for a in active),
        )
        while active and not _FORCE_EXIT.is_set():
            for a in active:
                a["thread"].join(timeout=2)
            active = [a for a in active if a["thread"].is_alive()]
    logger.info("[worker] 退出")


if __name__ == "__main__":
    main()
