-- Migration 002: add reference_samples table for the 爆文样本库 feature.
--
-- Run this ONCE against your existing Supabase instance if you set up
-- the database before v0.20.0. New installs pick it up via schema.sql.
-- Safe to run repeatedly — IF NOT EXISTS.

CREATE TABLE IF NOT EXISTS reference_samples (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title TEXT NOT NULL DEFAULT '未命名样本',
    source_type TEXT DEFAULT 'screenshot',
    content_text TEXT,
    analysis JSONB,
    image_url TEXT,
    tags TEXT[] DEFAULT '{}',
    created_by TEXT DEFAULT 'default',
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_reference_samples_created
    ON reference_samples(created_at DESC);

-- Match the project's RLS setting (currently disabled for dev)
ALTER TABLE reference_samples DISABLE ROW LEVEL SECURITY;
