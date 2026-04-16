-- Migration 003: Add missing indexes for common query patterns
--
-- Problems addressed:
--   1. Dashboard filters by projects.status → full table scan
--   2. Stuck detection queries stage_logs.status → no index
--   3. outputs table allows duplicate (run_id, version) pairs

-- Index on projects.status for dashboard filtering
CREATE INDEX IF NOT EXISTS idx_projects_status
  ON projects (status);

-- Index on stage_logs.status for finding failed/stuck stages
CREATE INDEX IF NOT EXISTS idx_stage_logs_status
  ON stage_logs (status);

-- Unique constraint on outputs (run_id) — one output per run
-- (version column doesn't exist yet in current schema, so just
-- ensure one output per run_id)
CREATE UNIQUE INDEX IF NOT EXISTS idx_outputs_run_id_unique
  ON outputs (run_id);
