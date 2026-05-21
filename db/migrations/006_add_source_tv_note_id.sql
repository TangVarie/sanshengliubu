-- Migration 006: reference_samples 增加 source_truth_vault_note_id 列
--
-- 背景:truth-vault → sanshengliubu 飞轮同步管道(TV 端 sync 脚本)把
-- "已审核结晶 note"作为 reference_pack 写入到 reference_samples。需要
-- 在 sanshengliubu 这边持久化 TV 源 note id,以支持:
--   1. 同步幂等 — 同一 TV note 多次 sync 不产生重复 pack(靠 unique index)
--   2. 反向溯源 — 从 sanshengliubu 的 pack 能追回 truth-vault 上的源 note
--
-- 这一列由 TV sync 脚本写入端填充(service_role key 直连 Supabase),
-- sanshengliubu 自身代码不需要读写这一列;UI 列表 / vibe_loop 检索
-- 路径完全不关心这一列的值,只有"幂等 upsert"路径会用到。
--
-- 幂等:ADD COLUMN IF NOT EXISTS + CREATE UNIQUE INDEX IF NOT EXISTS
--
-- Partial index(WHERE 子句):只对 TV-synced 行强制唯一,sanshengliubu
-- 自建 pack 的 source_truth_vault_note_id IS NULL,可以有多行 NULL,
-- 互不干扰;同时也避免把唯一约束广播到整个 reference_samples 表。

ALTER TABLE reference_samples
    ADD COLUMN IF NOT EXISTS source_truth_vault_note_id TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_reference_samples_tv_note_id_unique
    ON reference_samples(source_truth_vault_note_id)
    WHERE source_truth_vault_note_id IS NOT NULL;
