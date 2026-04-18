-- Migration 001: add composite (run_id, stage_name) index on stage_logs.
--
-- Run this ONCE against your existing Supabase instance (SQL Editor) if
-- you set up the database before v0.10.0. New installs pick it up via
-- schema.sql directly.
--
-- Why: resume / revise / cell-level recovery all filter stage_logs by
-- (run_id, stage_name). The single-column indexes already on the table
-- work but postgres prefers the composite — noticeable on runs that have
-- accumulated dozens of builder/cell_planner batch entries.
--
-- Safe to run repeatedly — IF NOT EXISTS makes it idempotent.

CREATE INDEX IF NOT EXISTS idx_stage_logs_run_stage
    ON stage_logs(run_id, stage_name);
