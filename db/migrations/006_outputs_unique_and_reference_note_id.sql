-- Migration 006: 对齐 schema.sql 与迁移路径的漂移(审计 COR-013)
--
-- 背景:schema.sql 与 migrations/ 是两条并行安装路径,审计发现三处不等价:
--   1. outputs(run_id) 唯一索引只在 migration 003;老 schema.sql 只建了
--      普通索引,而 save_output 现在用原子 upsert(on_conflict=run_id),
--      必须有唯一约束(审计 COR-007)。
--   2. source_truth_vault_note_id 列 + 其 partial unique index 只在
--      schema.sql;只跑迁移的库缺该列,list_reference_packs 必选它,
--      查询直接 42703。
--   3. pipeline_runs.heartbeat_at 列只在 schema.sql;只跑迁移的库缺列,
--      心跳/僵尸收割整条链路失效。
-- 本迁移把三者补齐。全部幂等,可重复执行。

-- 1) outputs(run_id) 唯一化:先清历史重复行(保留每个 run_id 最新一行),
--    再建唯一索引;老的普通索引被唯一索引取代。
DELETE FROM outputs o
USING outputs o2
WHERE o.run_id = o2.run_id
  AND o.id <> o2.id
  AND (o.created_at < o2.created_at
       OR (o.created_at = o2.created_at AND o.id < o2.id));

CREATE UNIQUE INDEX IF NOT EXISTS idx_outputs_run_id_unique
    ON outputs (run_id);

DROP INDEX IF EXISTS idx_outputs_run;

-- 2) truth-vault 来源列 + 幂等唯一索引(与 schema.sql 对齐)
ALTER TABLE reference_samples
    ADD COLUMN IF NOT EXISTS source_truth_vault_note_id TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_reference_samples_tv_note_id_unique
    ON reference_samples(source_truth_vault_note_id)
    WHERE source_truth_vault_note_id IS NOT NULL;

-- 3) 心跳列(与 schema.sql 对齐;reaper 依赖它区分活/僵尸 run)
ALTER TABLE pipeline_runs
    ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMPTZ;
