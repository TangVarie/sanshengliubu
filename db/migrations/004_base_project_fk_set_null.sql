-- Migration 004: base_project_id 的外键改为 ON DELETE SET NULL
--
-- 原 schema（v0.1）:
--   base_project_id UUID REFERENCES projects(id)
-- 默认策略是 NO ACTION，删除父项目时会报错，导致 delete_project() 对有
-- 迭代子项目的记录直接失败；也可能留下指向已删除父项目的悬挂引用（被
-- SUPABASE 手工删除时）。
--
-- 改为 ON DELETE SET NULL：父项目被删除时，子项目（迭代）保留但 base_project_id
-- 置空——迭代项目可以独立运行，没必要跟父一起消失。
--
-- 幂等：PostgreSQL 不支持直接 ALTER CONSTRAINT 修改 ON DELETE 动作，所以
-- 先 DROP 再 ADD。drop 用 IF EXISTS 容错。约束名在 schema.sql 创建时由
-- PostgreSQL 自动命名为 projects_base_project_id_fkey（表名 + 列名 + fkey）。

ALTER TABLE projects
    DROP CONSTRAINT IF EXISTS projects_base_project_id_fkey;

ALTER TABLE projects
    ADD CONSTRAINT projects_base_project_id_fkey
    FOREIGN KEY (base_project_id)
    REFERENCES projects(id)
    ON DELETE SET NULL;
