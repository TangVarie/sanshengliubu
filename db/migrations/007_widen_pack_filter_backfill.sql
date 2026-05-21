-- Migration 007: 回填 truth-vault sync 历史 pack 的命名契约
--
-- 背景:truth-vault sync 脚本早期版本(TV 端契约对齐之前)写入时用了
-- 跟 sanshengliubu 端读取契约不匹配的值:
--   1. source_type = 'truth_vault_sync'
--      → 打不中 list_reference_packs 的 .eq("source_type","pack") 过滤
--      → 这些 TV-injected pack 在 reference_library 页面看不到
--      → vibe_loop 检索拿不到 TV 来源样本
--   2. platform = 'xiaohongshu'(英文 slug)
--      → 跟 sanshengliubu UI / 流水线检索用的中文 '小红书' 不匹配
--      → get_relevant_reference_packs 按 platform 严格等值,跨不过去
--
-- TV 端写入契约现在已经对齐(写 source_type='pack' + platform='小红书'),
-- 这个 migration 把存量历史数据也对齐过来,完成 P0 #1 / P0 #2 的修复。
--
-- 安全性 / 不误伤:
--   - source_type='truth_vault_sync' 只在 TV 早期写入路径出现,sanshengliubu
--     自建 pack 一直走 save_reference_pack(source_type='pack'),不会用这个值
--   - platform='xiaohongshu'(英文)只在 TV 早期写入路径出现,sanshengliubu
--     自身代码(pages/6_reference_library.py / vibe_loop)从来用'小红书'
--   所以两条 UPDATE 都精确锁定 TV 早期遗留行,无误伤风险。
--
-- 幂等:跑完之后没有 source_type='truth_vault_sync' 或 platform='xiaohongshu'
-- 的行,二次执行 UPDATE 不会改任何行(0 rows affected)。

UPDATE reference_samples
   SET source_type = 'pack'
 WHERE source_type = 'truth_vault_sync';

UPDATE reference_samples
   SET platform = '小红书'
 WHERE platform = 'xiaohongshu';
