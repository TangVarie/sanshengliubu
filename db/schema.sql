-- 三省六部 Prompt Engineering System — Supabase Schema
-- Run this in the Supabase SQL Editor (https://supabase.com/dashboard → SQL Editor)
--
-- This schema is idempotent (IF NOT EXISTS on every object), so re-running
-- is safe and will only add what's missing. Existing installs upgrading
-- across versions can re-run this file instead of hunting individual
-- migration scripts — though migration files under db/migrations/ are
-- still kept for reference and as minimal targeted alternatives.

-- 项目表
CREATE TABLE IF NOT EXISTS projects (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    status TEXT DEFAULT 'draft',           -- draft | running | completed | failed
    task_type TEXT DEFAULT 'new_system',   -- new_system | iteration | extension
    brief JSONB,                           -- 太子产出的结构化 brief
    free_text TEXT,                        -- 用户原始输入
    base_project_id UUID REFERENCES projects(id) ON DELETE SET NULL,  -- 迭代关联；父项目删除后保留子项目但断开引用
    created_by TEXT DEFAULT 'default',
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- 流水线运行记录
CREATE TABLE IF NOT EXISTS pipeline_runs (
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
CREATE TABLE IF NOT EXISTS stage_logs (
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
CREATE TABLE IF NOT EXISTS outputs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id UUID NOT NULL REFERENCES pipeline_runs(id) ON DELETE CASCADE,
    prompt_system JSONB NOT NULL,          -- 完整 prompt 系统
    final_review JSONB,                    -- 终审报告
    version INTEGER DEFAULT 1,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- 索引
CREATE INDEX IF NOT EXISTS idx_pipeline_runs_project ON pipeline_runs(project_id);
CREATE INDEX IF NOT EXISTS idx_stage_logs_run ON stage_logs(run_id);
CREATE INDEX IF NOT EXISTS idx_stage_logs_stage ON stage_logs(stage_name);
-- Composite covers the hot path in resume/revise/cell-recovery: filter
-- by (run_id, stage_name) to skip stages already completed. Single-column
-- indexes above would still work but postgres prefers the composite for
-- this exact query shape. Added in v0.10.0.
CREATE INDEX IF NOT EXISTS idx_stage_logs_run_stage ON stage_logs(run_id, stage_name);
CREATE INDEX IF NOT EXISTS idx_outputs_run ON outputs(run_id);

-- 参考爆文样本库
CREATE TABLE IF NOT EXISTS reference_samples (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title TEXT NOT NULL DEFAULT '未命名样本',
    source_type TEXT DEFAULT 'screenshot',  -- screenshot | text | url | pack
    content_text TEXT,                       -- legacy: OCR'd text or user-pasted
    analysis JSONB,                          -- legacy: Gemini screenshot analysis
    image_url TEXT,                          -- legacy: Supabase storage URL
    tags TEXT[] DEFAULT '{}',                -- legacy: user tags
    -- v2 "证据包" fields (migration 005) — 封面+标题+正文+评论区 四位一体
    post_title TEXT,                         -- 原帖标题
    post_body TEXT,                          -- 原帖正文
    cover_image_b64 TEXT,                    -- 封面 base64(支持视觉分析)
    top_comments JSONB DEFAULT '[]'::jsonb,  -- [{text, likes?}, ...]
    platform TEXT,                           -- 小红书 / 抖音 / B站 / ...
    category TEXT,                           -- 护肤 / 食品 / 3C / ...
    ai_analysis JSONB,                       -- 太子式分析(vibe_tags/hook/tone/comment_dna)
    quality_score INTEGER DEFAULT 0,         -- A/B 反馈加权(可选)
    source_truth_vault_note_id TEXT,         -- truth-vault sync 来源 note id;非 TV-synced 行为 NULL
    created_by TEXT DEFAULT 'default',
    created_at TIMESTAMPTZ DEFAULT now()
);

-- ⚠ schema.sql 升级路径补丁:CREATE TABLE IF NOT EXISTS 对已存在的表是
-- no-op,新增列必须通过 ALTER TABLE 单独补,否则下面的 partial unique
-- index 会因列不存在报 42703。每次给 reference_samples 加新列时都要在
-- CREATE TABLE 块和 CREATE INDEX 块之间补一行 ALTER。
ALTER TABLE reference_samples
    ADD COLUMN IF NOT EXISTS source_truth_vault_note_id TEXT;

CREATE INDEX IF NOT EXISTS idx_reference_samples_created
    ON reference_samples(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_reference_samples_platform_category
    ON reference_samples(platform, category);
CREATE INDEX IF NOT EXISTS idx_reference_samples_quality
    ON reference_samples(quality_score DESC, created_at DESC);
-- TV sync 幂等性:同一 truth-vault note 重复 sync 不产生重复 pack。
-- Partial index — 只对 TV-synced 行强制唯一,自建 pack(IS NULL)不受影响。
CREATE UNIQUE INDEX IF NOT EXISTS idx_reference_samples_tv_note_id_unique
    ON reference_samples(source_truth_vault_note_id)
    WHERE source_truth_vault_note_id IS NOT NULL;

-- ════════════════════════════════════════════════════════════════════
-- 2026-05-22 audit R-019: 本块代码假设【单租户运营】——所有登录到同一
-- 个 Supabase project 的用户共享同一份数据,没有 workspace 级隔离。
--
-- 适合: 个人 / 单工作室自用, 数据互见不是问题。
-- 不适合: 多客户/品牌共用、合规审计、需要 workspace 边界的场景。
--
-- 如果你需要多租户隔离, 请改跑姐妹仓 truth-vault 提供的迁移:
--     sanshengliubu-patches/005_multi_tenant_workspaces.sql
-- 它会把 workspace_id 列加到所有主表 + 开启 RLS + 加 policy + 同步
-- auth.users 到默认 workspace。**先在 staging 验证, 再上生产。**
--
-- 如果你不确定走哪条路线, 默认就停在单租户。改这块代码前请先看 README。
-- ════════════════════════════════════════════════════════════════════
-- 禁用 RLS（单租户假设；多租户场景见上方注释）
ALTER TABLE projects DISABLE ROW LEVEL SECURITY;
ALTER TABLE pipeline_runs DISABLE ROW LEVEL SECURITY;
ALTER TABLE stage_logs DISABLE ROW LEVEL SECURITY;
ALTER TABLE outputs DISABLE ROW LEVEL SECURITY;
ALTER TABLE reference_samples DISABLE ROW LEVEL SECURITY;
