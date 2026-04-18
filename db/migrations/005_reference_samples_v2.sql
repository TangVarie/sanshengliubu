-- Migration 005: reference_samples v2 — "证据包" schema
--
-- 背景:URL-only 参考方案不可行(小红书等平台 bot 挡 / JS 渲染 / 登录墙),
-- 而且"网感"是封面 + 标题 + 正文 + 评论区四位一体的 gestalt,单独抓文字
-- 拿不到评论区共振,拿不到 vibe DNA。
--
-- 新方案:人工录入"证据包"(cover/title/body/top_comments),AI 做一次
-- "太子式"结构化分析(ai_analysis JSONB),下游 vibe_rewriter / vibe_critic
-- 按 platform + category 检索命中样本,评论区 DNA 作为核心注入。
--
-- 幂等:所有 ADD COLUMN 都用 IF NOT EXISTS;重复跑无副作用。
-- 原有列(title / content_text / analysis / image_url / tags)保留,
-- 新字段独立命名避免冲突。

ALTER TABLE reference_samples
    ADD COLUMN IF NOT EXISTS post_title     TEXT,                         -- 帖子原标题
    ADD COLUMN IF NOT EXISTS post_body      TEXT,                         -- 帖子正文
    ADD COLUMN IF NOT EXISTS cover_image_b64 TEXT,                        -- 封面 base64(可选,支持视觉分析)
    ADD COLUMN IF NOT EXISTS top_comments   JSONB DEFAULT '[]'::jsonb,    -- top N 评论 [{text, likes?}, ...]
    ADD COLUMN IF NOT EXISTS platform       TEXT,                         -- 小红书 / 抖音 / B站 / 知乎 / 微博
    ADD COLUMN IF NOT EXISTS category       TEXT,                         -- 护肤 / 食品 / 3C 等
    ADD COLUMN IF NOT EXISTS ai_analysis    JSONB,                        -- 太子式分析输出(vibe_tags / hook / tone / comment_dna ...)
    ADD COLUMN IF NOT EXISTS quality_score  INTEGER DEFAULT 0;            -- 用于后续 A/B 反馈加权

-- 检索热路径:vibe_loop 按 platform + category 拉匹配样本
CREATE INDEX IF NOT EXISTS idx_reference_samples_platform_category
    ON reference_samples(platform, category);

-- quality_score 降序 + created_at 降序,用于"优质优先"检索
CREATE INDEX IF NOT EXISTS idx_reference_samples_quality
    ON reference_samples(quality_score DESC, created_at DESC);
