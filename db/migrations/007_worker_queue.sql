-- Migration 007: worker 执行模式的队列列(Railway 迁移第一步)
--
-- 背景:执行层从 Streamlit 进程剥离为独立 worker(`python -m worker`)。
-- UI 在 PIPELINE_EXECUTION_MODE=worker 时只把 run 写成 status='pending'
-- 入队;worker 轮询认领(pending→running 的条件 CAS)后执行。
--
-- queued_action 语义:
--   'start'             — 全新 run,worker 认领时先做项目级 CAS 抢占
--   'resume'            — 从 failed/needs_revision 恢复,同样先抢占项目
--   'resume_preclaimed' — 项目锁已被该 run 持有(修订入队 / 僵尸复活),
--                         worker 认领时跳过项目 CAS
--
-- 幂等,可重复执行。thread 模式不写该列,老部署不受影响。

ALTER TABLE pipeline_runs
    ADD COLUMN IF NOT EXISTS queued_action TEXT;

-- worker 轮询与 reaper/看板按状态筛选的热路径。
CREATE INDEX IF NOT EXISTS idx_pipeline_runs_status
    ON pipeline_runs (status);
