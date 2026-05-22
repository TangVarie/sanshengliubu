# sanshengliubu 2026-05-22 audit followups · 修改报告

**分支**: `claude/pensive-feynman-KXDHG`
**基线**: `main @ d10c15d`
**实施日期**: 2026-05-22
**对照文档**: truth-vault `docs/10-sister-repo-followups.md`

---

## 范围说明

audit 文档列了 8 项 followup,其中和 sanshengliubu 直接相关的是 5 项。
本次实施按"风险换价值"优先级落地了 4 项 (R-022 / R-019 / R-023 / R-026),
另 1 项 (R-028 stage-level resume) 工时 1-2 天且需要 schema 改动 + UI 集成
测试,本轮未做,在「未实施 · 列入后续 sprint」一节解释。

| ID | 主题 | 紧急度 | 本次状态 |
|----|------|--------|----------|
| **R-022** | vibe_rewriter 必须从 DB 注入 reference_samples | **P0 · 飞轮闭环** | ✅ 已实施 |
| **R-019** | 单租户 / 多租户决策声明 | P1 | ✅ 已实施 (Option A 单租户声明) |
| **R-023** | logger secret masking | P1 | ✅ 已实施 |
| **R-026** | LLM retry framework | P2 | ✅ 已实施 (Gemini side) |
| R-018 | daemon thread → 持久 job worker | Sprint 2+ | ⏸ 未实施 (1-2 周工时) |
| R-028 | stage-level resume (cell 粒度) | P3 | ⏸ 未实施 (1-2 天 + schema 改动) |

---

## R-022 (P0) · vibe_rewriter 真正用上 DB 样本

### 问题诊断

audit 文档原文怀疑 `vibe_rewriter.md` 硬编码 6 条假人例子,DB 里
TV-synced 的爆款样本完全没被注入。

**实际复核结果**: orchestrator.py:2659-2673 早就把 `reference_packs_by_platform`
取出并注入到 rewriter input,vibe_rewriter.md:94-107 也写了"如有必用"的指令。
**链路已通,但仍有 3 个隐患让飞轮事实上失效**:

1. **优先级错位**: 静态样本在 prompt 顶部独立一段(第 37-46 行),长度 8 条且
   位置显赫;DB 样本则在第 94 行才出场,标记为"可选,如有必用"。LLM 看 system
   prompt 时静态样本"先入为主",DB 样本被视作可补充。
2. **来源混淆**: 没标 source_type,LLM 无法在 rewrite_summary 里说清楚锚点来自
   TV 飞轮还是用户手工录入,审计追不回 "飞轮跑起来了吗"。
3. **0 命中无告警**: `retrieve_samples.py` 拿不到 packs 时只 `logger.info`,运营
   永远不会注意到"我那个平台数据库里 0 条爆款,vibe_rewriter 在裸跑静态兜底"。

### 实施

#### a) 重新框定 vibe_rewriter prompt 的样本来源优先级

文件: `pipeline/prompts/vibe_rewriter.md`

- 第 37-47 行的「真人参照样本」段重写为「真人参照样本 · 来源优先级」,
  明确 🥇 PRIMARY = `reference_packs_by_platform`、🥈 FALLBACK = 静态样本
- 加铁律:DB 有命中时**禁止主要锚点用静态样本**;必须在 `rewrite_summary` 写
  锚点来源 (`源:数据库样本 #<id>` 或 `源:静态兜底 #<编号>` 并附原因)
- 第 94 行的 `reference_packs_by_platform` 字段描述从"可选,如有必用"改为
  "**飞轮主源,必用**"
- 新增 `reference_packs_summary` 字段说明 (告诉 LLM 哪些平台 hit / miss)

#### b) retrieve_samples 加 source_type + summary

文件: `pipeline/retrieve_samples.py`

- `_shape_for_rewriter()` 新增 `source_type` 字段,从
  `source_truth_vault_note_id` 推导 `truth_vault` / `manual`
- 0 packs 命中从 `logger.info` 升级为 `logger.warning`,给运营暴露飞轮失效信号
- 命中日志新增 `tv_synced=N, manual=N` 拆分,可视察"飞轮 vs 手工"占比
- **新增** `summarize_packs_by_platform()` 函数:
  - 返回 `total_packs / platforms_hit / platforms_missed / tv_synced_total / manual_total`
  - 用作 prompt input 的 metadata,LLM 据此决定单 cell 走 PRIMARY 还是 FALLBACK

#### c) orchestrator 把 summary 串到 3 条注入路径

文件: `pipeline/orchestrator.py`

- 第 56 行 import 同时引入 `summarize_packs_by_platform`
- 第 2659 行附近:vibe_loop 内每次构建 `reference_packs_summary`,3 处注入
  (`critic_input` / `structural_input` / `rewriter_input` 包括 legacy path)
- 第 2674 行起新增 telemetry:`tv_synced`/`manual` 分项 + `platforms_missed`
  写入 INFO 日志;全部 miss 升级 WARNING (飞轮失效信号)

### 验证

- AST 解析全通过
- `summarize_packs_by_platform({'小红书': [TV×2, manual×1], '抖音': [manual×1]}, ['小红书','抖音','B站'])` 返回
  `{total_packs:4, platforms_hit:{'小红书':3,'抖音':1}, platforms_missed:['B站'], tv_synced_total:2, manual_total:2}`
- `_shape_for_rewriter` 对 `source_truth_vault_note_id` 有值/无值正确推导
  `source_type`

### 影响

- LLM 现在在 vibe_loop 每个 cell 都能 *看到* DB 样本是 PRIMARY,prompt 不再
  让静态样本"喧宾夺主"
- `rewrite_summary` 锚点来源被强制要求 — 上线后 grep `源:数据库样本` /
  `源:静态兜底` 就能审计飞轮真实命中率
- 运营 1 天内能看到"该平台 0 命中"的 WARNING,及时排查 TV sync 或手工录入

---

## R-019 (P1) · 单租户声明

### 选择路径

文档给了 Option A (单租户声明,10 分钟) 和 Option B (多租户 RLS,1-2 天)。
README 现状 + 用户实际部署都是单用户/单工作室,**走 Option A**。Option B 改造
留在 README 顶部 + schema.sql 注释里指向 truth-vault 的 005 migration,届时
切换可以零迷路。

### 实施

#### a) README.md 顶部加单租户警告条

文件: `README.md`

在 `# 🏛️ 三省六部 · Prompt Engineering System` 之后插入 `> ⚠️ 单租户假设`
段落,明确:
- 5 张主表 DISABLE RLS 是有意选择
- 哪些场景适合 / 不适合
- 多租户改造需要的迁移 (`sanshengliubu-patches/005_multi_tenant_workspaces.sql`)

#### b) db/schema.sql 在 DISABLE RLS 块附近加注释

文件: `db/schema.sql`

第 117-122 行的 `ALTER TABLE ... DISABLE ROW LEVEL SECURITY` 5 行上方加 13 行
注释块,内容:R-019 audit 决策、适用场景、多租户改造怎么做。改这块代码之前
运维必须先看 README。

#### c) app.py 启动 sidebar banner

文件: `app.py`

`show_version_badge()` 之后加一行 `st.sidebar.caption("🔓 单租户模式...")`,
让运营每次打开页面都看见。万一接第二个客户忘了切换,立刻能想起来。

### 验证

- README diff 顶部清晰展示警告
- schema.sql 注释包含 truth-vault 文档路径
- AST 解析 app.py 通过

---

## R-023 (P1) · logger secret masking

### 实施

#### a) 新建 `pipeline/logger_utils.py`

提供:
- `mask_secrets(s)` 纯函数:对 Anthropic / Supabase / JWT / Google /
  OpenAI / 通用 sk-* token 模式做正则替换为 `***REDACTED***`
- `SecretMaskingFormatter` 包装现有 logging Formatter,保留 datefmt/format
  字符串,只对最终渲染的 message 做 mask
- `install_secret_masking_on_root_logger()` idempotent,在 app 启动时调用

#### b) 把 mask_secrets 应用到 3 个泄密通道

| 文件 | 位置 | 风险来源 |
|------|------|----------|
| `pages/3_pipeline_detail.py:46` | `render_stage_error` | `error_message` 可能含 traceback 中的 API key |
| `pages/3_pipeline_detail.py:1014/1270` | `st.code(_raw, ...)` × 2 | Gemini 返回原文可能含用户输入里的 URL token |
| `pipeline/agents/gemini_client.py:441` | `logger.info("[gemini] raw text...")` | 写入 server stderr 的 raw_text 可能带 token |

#### c) app.py 启动时挂 SecretMaskingFormatter

文件: `app.py`

主入口 `install_secret_masking_on_root_logger()` 在 import 顺序最早处调用,
确保后续任何模块的 `logger.exception` 写入 stderr 之前都已经过 mask。

### 验证 (单元自检)

```
mask_secrets cases:
  ✓ sk-ant-api03-XYZabcDEF123456789012345 → ***REDACTED***
  ✓ sb_secret_1234567890abcdefghij1234567890 → ***REDACTED***
  ✓ eyJxxx.eyJxxx.signature (JWT) → ***REDACTED***
  ✓ AIzaSyBxxxx0123456789012345abcdef → ***REDACTED***
  ✓ sk-proj-1234567890abcdefghijklmnop → ***REDACTED***
Edge cases: None/empty/non-string/clean text 全部正确
```

### 影响

- 运维登录看日志、截图发群、Streamlit Cloud 日志收集器抓走,都不再泄漏凭据
- 添加新 secret 模式只要在 `_SECRET_PATTERNS` tuple 加一行即可

---

## R-026 (P2) · Gemini LLM retry framework

### 问题

Claude 调用在 `BaseAgent.run()` 已经有 MAX_RETRIES + 指数退避 (3 次 / 3s基底),
但 Gemini 调用 (`pipeline/agents/gemini_client.py:382` 的
`client.models.generate_content`) **裸跑**,一次 429/503 直接挂掉整条 vibe_loop。

### 实施

#### a) 新建 `pipeline/llm_retry.py`

提供:
- `call_with_retry(fn, *args, max_attempts=3, initial_wait=2.0, max_wait=60.0, operation="llm_call")`
- `acall_with_retry` 异步版本 (asyncio.sleep, 不阻塞 event loop)
- `_is_transient(exc)` 启发式:匹配
  `429 / 503 / 504 / 502 / timeout / connection / overloaded / rate limit /
  resource exhausted / deadline exceeded / unavailable` 等子串
- 非 transient 异常立即 raise,不烧 retry budget
- backoff 上限 `max_wait=60s` 防爆炸

#### b) gemini_client.call_gemini_json 包一层 retry

文件: `pipeline/agents/gemini_client.py:382-391`

把原直接调用 `client.models.generate_content(...)` 改为:

```python
from pipeline.llm_retry import call_with_retry
def _do_generate():
    return client.models.generate_content(...)
response = call_with_retry(
    _do_generate,
    max_attempts=3,
    initial_wait=2.0,
    max_wait=30.0,
    operation=f"gemini.{model_id}",
)
```

参数选 `max_wait=30s` 而不是默认 60s — Gemini 抖动通常 5-15s 内恢复,
30s 上限避免单次卡太久。

### 验证 (单元自检)

```
_is_transient classification:
  ✓ 429 / timeout / Connection reset / 503 → True
  ✓ 401 unauthorized / invalid model → False (fail-fast)
fail-fast: 1 call, 0ms (no backoff sleep)
retry-then-succeed: 3 attempts (2 transient + 1 success), returned 'ok'
exhaust: 3 attempts, then re-raise last transient exception
```

### 影响

- Gemini 短暂 429/503 不再断流水线,vibe_loop 的总成本(已经花掉的上游
  token)不再因瞬时抖动浪费
- 新的 LLM backend 接入时直接复用 `call_with_retry`,无需再发明轮子
- Claude 路径不动 (`BaseAgent.run()` 已经有独立 retry 实现,改它风险更大且无
  净收益)

---

## 未实施 · 列入后续 sprint

### R-018 · daemon thread → 持久 job worker (1-2 周)

文档建议:加 `jobs` 表 + 独立 worker 进程 + UI 轮询。
**为什么本轮不做**:

- 工时 1-2 周,需要 schema migration + worker 进程部署 (systemd/supervisor) +
  Streamlit UI 重写 + Phase 1/2/3/4 分阶段开关
- 当前 sanshengliubu 的 pipeline 是 `async def run()` 在 Streamlit 内部跑,
  浏览器关闭时确实会被 streamlit 强制收掉 — 但运营反馈这不是日常痛点
- 直接迁移风险大;建议先在 staging 跑 Phase 1 (worker 只注册 noop handler)
  验证 sweeper / 心跳无误,再做 Phase 2

**前置 prep**:可以同时:
- 在 sanshengliubu/db/migrations/ 备份一份 `006_jobs_table.sql` (从 truth-vault
  仓 `sanshengliubu-patches/004_jobs_table.sql` 拷过来),migration 文件不影响
  代码运行
- 把 `app.py` 启动 pipeline 的入口点抽函数,以便未来切到 job-insert 时改动小

### R-028 · stage-level / cell-level resume (1-2 天 + schema 改动)

文档建议:`stage_logs` 加 `cell_status JSONB` 列,记录复合 stage 内每个 cell
的 success / failed / pending,resume 跳过已成功的。

**为什么本轮不做**:

- 需要 schema migration + orchestrator 内 strategy_loop / vibe_loop 等所有
  复合 stage 改造 + UI resume 入口加 cell 选择器
- 工时 1-2 天且必须配合完整 e2e 测试 — 重跑半小时的流水线验一次
- 已有的 `done[stage_name]` 跳过粒度对小 batch 够用;cell-level resume 在
  matrix > 30 cells 时才显著省钱

**前置 prep**:列入下一个 sprint;在那之前,运营 resume 时直接接受重跑整个
复合 stage 的成本。

---

## 文件 diff 一览

| 文件 | 变更类型 | 说明 |
|------|---------|------|
| `README.md` | 修改 | 顶部加单租户警告条 (R-019) |
| `app.py` | 修改 | 启动时调用 `install_secret_masking_on_root_logger`、添加 sidebar 单租户提醒 (R-019/R-023) |
| `db/schema.sql` | 修改 | DISABLE RLS 块上方加 13 行 audit 注释 (R-019) |
| `pipeline/orchestrator.py` | 修改 | import + vibe_loop 串入 `reference_packs_summary` + telemetry 升级 (R-022) |
| `pipeline/retrieve_samples.py` | 修改 | 加 `source_type` 字段;新增 `summarize_packs_by_platform`;0-pack 升级为 WARNING (R-022) |
| `pipeline/prompts/vibe_rewriter.md` | 修改 | 重写"真人参照样本"段:PRIMARY/FALLBACK 分层、强制 source 追溯 (R-022) |
| `pipeline/agents/gemini_client.py` | 修改 | `generate_content` 包 `call_with_retry`、raw_text log 走 `mask_secrets` (R-023/R-026) |
| `pages/3_pipeline_detail.py` | 修改 | `render_stage_error` + 2 处 `st.code(_raw, ...)` 走 `mask_secrets` (R-023) |
| `pipeline/logger_utils.py` | **新增** | `mask_secrets` + `SecretMaskingFormatter` + root logger installer (R-023) |
| `pipeline/llm_retry.py` | **新增** | `call_with_retry` / `acall_with_retry` / `_is_transient` (R-026) |
| `docs/2026-05-22-audit-followups-report.md` | **新增** | 本报告 |

---

## 自检结果

| 检查 | 结果 |
|------|------|
| 所有改动文件 AST 解析 | ✅ 7/7 OK |
| `mask_secrets` 5 种 token 模式 | ✅ 5/5 全被替换 |
| `mask_secrets` 边界条件 (None / 空 / 非字符串 / 干净文本) | ✅ 4/4 OK |
| `_is_transient` 分类 (429/503/timeout/auth/validation) | ✅ 6/6 OK |
| `call_with_retry` fail-fast 路径 (非 transient) | ✅ 1 次调用, <1ms |
| `call_with_retry` retry-then-succeed | ✅ 3 次调用,返回正确 |
| `call_with_retry` 耗尽重试 | ✅ 3 次调用,re-raise |
| `_shape_for_rewriter` source_type 推导 | ✅ TV / manual 两种正确 |
| `summarize_packs_by_platform` 输出形状 | ✅ 与文档约定一致 |

---

## 推荐运营动作

1. **本周**:观察生产日志,关注:
   - `[retrieve_samples] 0 packs for platform=...` (WARNING) — 哪些平台库存空
   - `[vibe_loop] NO reference_packs for any platform` (WARNING) — 全部静态兜底
   - `[R-022 audit] N/M vibe cells missing 源:* tag` (WARNING) — vibe_rewriter 漏写源标记
   - `[R-022 audit] some platforms used 源:静态兜底 more than DB pack exhaustion would justify` (WARNING) — 飞轮被忽略,按 per-platform 配额规则识别
2. **下次 brief 跑通后**:抽 1 个 cell 的 vibe_rewriter 输出,验证
   `rewrite_summary` 是否真带了 `源:数据库样本 #<id>` 标签
3. **多租户决策点**:接第二个客户之前,跑 truth-vault 仓的
   `sanshengliubu-patches/005_multi_tenant_workspaces.sql` 到 staging,
   按 README 警告条的指引推进
4. **后续 sprint 排期**:R-018 (job worker) 和 R-028 (cell-level resume)
   分别评估业务紧迫度

---

## 2026-05-22 follow-up · review 反馈处理

PR 第一轮 review 标了 5 个问题。下面是每条的现状+实施:

### 1. R-022 没有 E2E 飞轮验证 ⭐

**review**: unit test 只覆盖形状/推导,没有"DB → vibe_loop → rewrite_summary
带源标记"的真实端到端验证。LLM 可能不按 prompt 老实写"源:..."标记。

**实施**: 在 `pipeline/orchestrator.py` 新增 `_audit_rewrite_source_tags`
函数,vibe_loop 每轮 rewriter 输出后跑一遍。它做 3 件事:
- **missing_tag**: `rewrite_summary` 没有"源:"标记 → WARNING + cell_id 列表
- **wrong_source**: LLM 写了"源:静态兜底"但该 platform 在 DB 里有命中 →
  WARNING(这是 R-022 想堵的具体漏洞)
- **INFO 汇总**: `db=N, static=N, missing=N (of 总数)` 让运营 grep 出实际比例

不抛错的理由: LLM 漏写一个标记 ≠ 重写失败,抛错会让飞轮信号缺失变成可用性
故障。运营只需在日志里持续监控 missing/wrong 比例,降到 0 之前继续在 prompt
里加强。

**自检**: 4 个分支(全好/缺标记/错源/空)全跑过,输出符合预期。

### 2. mask_secrets 与 TV 模式数对齐

**review**: 报告里"5 patterns",TV 是 7 个。

**澄清**: 代码实际有 **7 个模式**(含 `sbp_*` 和 generic
`sk-[A-Za-z0-9]{40,}`);报告之前的 unit-test 列表只展示了 5 个例子,造成歧义。

**实施**:
- `pipeline/logger_utils.py` 顶部加 `Shadow-aligned with truth-vault's
  scripts/_common.py::_SECRET_PATTERNS` 显式注释,提醒未来 TV 加新 vendor
  时同步过来
- 重跑自检:7/7 patterns 全部 mask 成功 (Anthropic / Supabase service /
  Supabase PAT / JWT / Google / OpenAI scoped / generic sk-)

### 3. R-026 跨 backend retry 不一致

**review**: TV `annotate_essence_pass` 14s 上限、Gemini 30s、Claude 走
BaseAgent.run() 完全另一套——未来诊断难。

**实施**: 在 `pipeline/llm_retry.py` 顶部 docstring 加跨 backend 重试对照表
(Claude / Gemini / DeepSeek / OpenAI 4 条路径各自走哪个 retry 源、参数是什么),
并解释为何**不**强行统一:BaseAgent._call_claude 已包含 retry + budget +
rate limiter + cache fallback 4 项耦合状态机,migrating 到 llm_retry 是
另一个 PR 的工作量,而不是 R-026 的承诺。

Gemini 的 max_wait=30s 也写明是**有意**比 TV 的 14s 宽 — Vertex grounding
429 恢复周期通常 10-20s,14s 会"还没等到就放弃"。

### 4. acall_with_retry 是否真用 → 删除

**review**: 异步版本是 dead code,YAGNI。

**实施**: 已删除 `acall_with_retry`,只留同步版。在删除处加注释说明"所有
当前 LLM 调用站点都是 sync,BaseAgent 的 async run() 是用 asyncio.to_thread
跨边界的,不是 await async LLM 调用。哪天真有 `async def call_xxx_async`
再加回来"。`asyncio` import 也一并去掉。

### 5. vibe_rewriter.md 内部一致性

**review**: 旧 prompt L126 "证据包为空时...依然按静态样本执行" vs 新铁律
"必须写 #id" 在 0 命中情况下可能让 LLM 困惑(没 id 怎么写)。

**实施**: 在新铁律段下方加**三态决策表**,逐行列出 PRIMARY 状态 / 锚点来源 /
`rewrite_summary` 必写内容 / 备注:

| PRIMARY 状态 | 锚点 | rewrite_summary | 备注 |
|--------------|------|-----------------|------|
| 非空且匹配 | DB 证据包 | `源:数据库样本 #<id>` | id 用证据包 id |
| 非空但已被本批占用 | 次优证据包 / FALLBACK | 各自源标记 | 不能共用 id |
| 空 (0 命中) | 静态兜底 | `源:静态兜底 #<编号>(原因:该平台 0 命中)` | **不报错、不略过** |

这样"静态兜底必须写编号 + 原因"和"证据包为空不报错"在同一张表里说清楚,
LLM 不会在边界条件上摇摆。

---

## 2026-05-22 second-round review · `_audit_rewrite_source_tags` 误报修复

第二轮 review (PR #28) 在 `_audit_rewrite_source_tags` 上发现 2 个 false-positive
路径:

### P1 · structural_rewriter 输出被混入审计

**review**: "audit 跑在 `new_cells_by_id` 上,后者合并了 vibe + structural
两路输出,但 structural_rewriter 的 rewrite_summary format 不要求 `源:*`
标记。带 structural cells 的运行会系统性地拉高 missing_tag false positive,
运营无法分辨真漏洞和预期行为。"

**实施**:
- orchestrator vibe_loop 维护一个 `vibe_only_cells_by_id` 变量
  (modern 路径 = `vibe_new_by_id`、legacy 路径 = `new_cells_by_id`)
- audit 调用从 `new_cells_by_id` 改成 `vibe_only_cells_by_id`,
  structural 输出彻底不参与审计
- 函数 docstring 显式声明范围只限 vibe_rewriter

### P2 · 包用尽时的静态兜底被误报

**review**: "wrong_source 把任何 `源:静态兜底` 都标错,只要该平台 DB 里有
≥1 个包。但更新过的 vibe_rewriter prompt 明确允许包用尽时走静态。在多 cell
同平台批次里,合法 fallback 会被误报。"

**实施**: 重写检查规则为 **per-platform 配额比对**:
- 平台 P 有 `K` 个可用 DB 包、本批进 vibe_rewriter 的 P 平台 cell 有 `N` 个
- prompt 允许的最多静态使用 = `max(0, N - K)` (包用尽时合法)
- 实际静态使用 > 该上限 → 超出部分为真漏洞
- 不再做单 cell 级 wrong_source 判断 (因为不知道具体哪个 cell 拿到 pack)

新分类名 `excess_static_use`,日志带 per-platform 详细数字
(`vibe_cells_in_batch / available_packs / allowed_static_at_most /
actual_static / excess`)便于运营直接看为何报警。

### 顺手修的 unicode 冒号 bug

LLM 可能输出 `源:` (U+003A ASCII 冒号) 或 `源：` (U+FF1A 全角冒号)。原实现的
分支因为 Edit 折叠 unicode 退化成同一字符,只识别 ASCII。改写为模块顶部定义
两个显式常量 + `_has(text, suffix)` helper,两种冒号都被识别。

### 自检 (7 个分支)

| 场景 | 期望 | 结果 |
|------|------|------|
| A 纯 vibe cells, 全合规 | 无 WARNING | ✅ |
| B 包用尽 (3 cells / 2 packs / 1 static) | 无 WARNING | ✅ allowed=1, actual=1 |
| C 一偷懒 + 一 legit (1 db / 2 static, 2 packs) | WARNING excess=1 | ✅ |
| D missing_tag | WARNING missing | ✅ |
| E 全角冒号 U+FF1A | 识别为 db | ✅ |
| F 空输入 | no-op | ✅ |
| G 0-pack 平台全静态 | 无 WARNING (allowed=N) | ✅ |
