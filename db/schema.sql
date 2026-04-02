-- 三省六部 Prompt Engineering System — Supabase Schema
-- Run this in the Supabase SQL Editor (https://supabase.com/dashboard → SQL Editor)

-- 项目表
CREATE TABLE projects (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    status TEXT DEFAULT 'draft',           -- draft | running | completed | failed
    task_type TEXT DEFAULT 'new_system',   -- new_system | iteration | extension
    brief JSONB,                           -- 太子产出的结构化 brief
    free_text TEXT,                        -- 用户原始输入
    base_project_id UUID REFERENCES projects(id),  -- 迭代关联
    created_by TEXT DEFAULT 'default',
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- 流水线运行记录
CREATE TABLE pipeline_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    status TEXT DEFAULT 'running',         -- running | completed | failed | paused_for_review
    started_at TIMESTAMPTZ DEFAULT now(),
    completed_at TIMESTAMPTZ,
    total_tokens INTEGER DEFAULT 0,
    total_cost_usd NUMERIC(10,4) DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- 各环节执行日志
CREATE TABLE stage_logs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id UUID NOT NULL REFERENCES pipeline_runs(id) ON DELETE CASCADE,
    stage_name TEXT NOT NULL,              -- crown_prince | secretariat | chancellery_1 | ...
    status TEXT DEFAULT 'pending',         -- pending | running | completed | failed | skipped
    input_data JSONB,
    output_data JSONB,
    model_used TEXT,
    tokens_used INTEGER DEFAULT 0,
    duration_seconds NUMERIC(8,2),
    human_intervention JSONB,              -- 人工介入内容 (nullable)
    error_message TEXT,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- 最终产出
CREATE TABLE outputs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id UUID NOT NULL REFERENCES pipeline_runs(id) ON DELETE CASCADE,
    prompt_system JSONB NOT NULL,          -- 完整 prompt 系统
    final_review JSONB,                    -- 终审报告
    version INTEGER DEFAULT 1,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- 索引
CREATE INDEX idx_pipeline_runs_project ON pipeline_runs(project_id);
CREATE INDEX idx_stage_logs_run ON stage_logs(run_id);
CREATE INDEX idx_stage_logs_stage ON stage_logs(stage_name);
CREATE INDEX idx_outputs_run ON outputs(run_id);

-- 禁用 RLS（开发环境；生产环境请改用适当的 policy）
ALTER TABLE projects DISABLE ROW LEVEL SECURITY;
ALTER TABLE pipeline_runs DISABLE ROW LEVEL SECURITY;
ALTER TABLE stage_logs DISABLE ROW LEVEL SECURITY;
ALTER TABLE outputs DISABLE ROW LEVEL SECURITY;
