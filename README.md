# 🏛️ 三省六部 · Prompt Engineering System

> ⚠️ **单租户假设**(2026-05-22 audit R-019 确认)
>
> 本项目当前 schema (`db/schema.sql`) 对 5 张主表
> (`projects` / `pipeline_runs` / `stage_logs` / `outputs` / `reference_samples`)
> 显式 `DISABLE ROW LEVEL SECURITY`。这是 **单租户 MVP 假设** —
> 所有登录到同一个 Supabase project 的用户共享同一份数据。
>
> 适合:个人 / 单工作室自用,数据互见不是问题。
>
> 不适合以下场景,如果你要上,请先打开 RLS 并加 `workspace_id` 列后再部署:
> - 多个客户 / 品牌共用一个 Supabase 实例
> - 需要 workspace 级别的数据边界
> - 监管 / 内审要求数据访问审计
>
> 多租户改造的迁移脚本可见姐妹仓 truth-vault
> `sanshengliubu-patches/005_multi_tenant_workspaces.sql`,
> 上线之前请在 staging 跑 + 把现有 `auth.users` 加进默认 workspace。

一套基于多 Agent 协作的 Prompt 工程生产系统。

**输入**：产品 brief / 场景需求 / 现有 prompt 迭代诉求
**输出**：一套完整的、可直接投入内容生产线使用的 Prompt 系统

> 📐 新人入职 / 想理解跨模块设计选择 → 看 [`docs/architecture.md`](docs/architecture.md):
> 三套 LLM 重试为什么不统一、R-022 飞轮 audit 怎么落库、单租户 vs 多租户的
> 边界、Secret masking 与 truth-vault 的对齐策略。

## 架构

系统模拟中国古代"三省六部"政府运作：

```
用户（皇上）→ 太子（分拣）→ 中书省（策略）→ 门下省（审议）→ 尚书省（派发）
                                                    ↓
                         ┌──────┬──────┬──────┬──────┬──────┐
                         吏部   户部   礼部   兵部   刑部    （并行执行）
                         └──────┴──────┴──────┴──────┴──────┘
                                        ↓
                                  工部（组装）→ 门下省（终审）→ 产出
```

| 角色 | 职责 | 模型 |
|------|------|------|
| 太子 | 结构化用户输入 | `kimi-k3` |
| 中书省 | 策略规划 | `kimi-k3` |
| 门下省 | 独立审议（最多驳回2次） | `deepseek-v4-flash` |
| 尚书省 | 任务派发 | `kimi-k2.6` |
| 吏部 | 人设设计 | `kimi-k2.6` |
| 户部 | 关键词策略 | `kimi-k2.6` |
| 礼部 | 调性与平台适配 | `kimi-k2.6` |
| 兵部 | 竞争策略 | `deepseek-v4-flash` |
| 刑部 | 合规风控 | `kimi-k2.6` |
| 工部 | 组装最终系统 | `kimi-k3` |

三档模型的分工（完整逐 stage 映射见 `pipeline/config.py::KIMI_DEEPSEEK_MAP`，
运行时实际分配见「设置」页的模型配置区）：

| 档位 | 模型 | 单价 in/out (USD/1M) | 用在哪 | 为什么 |
|------|------|--------------------|--------|--------|
| 旗舰 | `kimi-k3` | $3 / $15 | 太子 · 中书省 · 工部架构 · 终审 | 只给"这步错了下游全废"的 4 个决策上游。太子丢素材→所有人看不到原始信号；中书省定错方向→六部在错方向上精耕；工部架构错→每个 cell 都长歪；终审放行→直接出货 |
| 主力 | `kimi-k2.6` | $0.95 / $4 | 内容生成 · 网感判断 · 五部 · 画像 | 中文写作和"人味"是选它的主要理由，内容侧不降档 |
| 廉价/异厂家 | `deepseek-v4-flash` | $0.14 / $0.28 | 门下省 · 兵部 · 红蓝守方 · 画像·alt · 网感二审 | 辩论/对抗环节故意换一家：同厂家同色彩就成了自言自语 |

> 换厂前主链路是 Claude Opus 4.7（$15/$75）打底，同样一次 run 现在便宜约一个数量级。

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置 Supabase

1. 注册 [Supabase](https://supabase.com)（免费）
2. 新建 Project
3. 在 SQL Editor 中运行 `db/schema.sql`
4. 记下 Project URL 和 anon key

> **已有数据库升级？**
> `db/schema.sql` 本身已是幂等的（所有对象都用 `IF NOT EXISTS`），直接重跑不会报错、只会补齐新增的索引/字段。
> 如果你希望最小化改动、只打补丁，也可以按版本逐个跑 `db/migrations/` 下的脚本：
> - `001_add_stage_logs_composite_index.sql` — v0.10.0 新增的 `(run_id, stage_name)` 复合索引，加速 resume/revise 的热路径。

### 3. 配置 Secrets

```bash
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
```

`.streamlit/secrets.toml` 要填四块：**Kimi** + **DeepSeek** + **SocialDataX** + **Supabase**。
完整带注释的模板在 `.streamlit/secrets.toml.example`。

#### Kimi / Moonshot（必填）

主链路 24 个 stage 里 20 个跑 Kimi，辅助层（识图 / 截图分析 / 结构审）也用同一个 key。
没有它整条流水线起不来。

```toml
MOONSHOT_API_KEY = "sk-..."
# 不填默认 https://api.moonshot.cn/anthropic（国内站）
# MOONSHOT_BASE_URL = "https://api.moonshot.ai/anthropic"
```

> ⚠️ **国内站和国际站是两套独立账号体系，key 不通用。**
> `platform.moonshot.cn` 的 key 只能配 `api.moonshot.cn`，`platform.moonshot.ai`
> 的 key 只能配 `api.moonshot.ai`。配错会报 401/404，而报错文案不一定指向
>「站点搞错了」这个真实原因。部署在 Streamlit Cloud（美国机房）且手里是国际站
> key 的话，走国际站延迟低不少。

走的是 Moonshot 的 **Anthropic 兼容端点**（`/anthropic/v1/messages`），所以整条
调用链仍然是 anthropic SDK，`thinking` 用标准的 `{"type":"adaptive"}` 参数。

#### DeepSeek（强烈建议填）

门下省 / 兵部 / 红蓝守方 / 画像模拟·alt / 网感二审 走这里。这几个位置**故意**跟
Kimi 用不同厂家——辩论双方同色彩就成了自言自语。

```toml
DEEPSEEK_API_KEY = "sk-..."
# 不填默认 https://api.deepseek.com/anthropic
```

不填也能跑：这几个 stage 会按 `MODEL_FALLBACK_CHAIN` 降级回 Kimi 把流水线走完，
但跨厂家对抗的价值就没了。

#### SocialDataX（趋势取样必填）

两个用途：(1) 跑策略前抓真实小红书爆款当校准样本——默认 **REQUIRED**，取不到样会
直接终止 run；(2) 把用户粘贴的对标帖 URL 取成结构化正文 + 真实话题标签（advisory，
失败只跳过）。

参考帖抓取走各平台的 detail 工具，优先 by-URL 变体（`xhs_get_note_detail_by_note_url`
等），失败才退回 by-ID。工具名和参数名在 `pipeline/config.py::SOCIALDATAX_NOTE_DETAIL_TOOLS`。

> 踩过的坑：XHS detail 返回的正文字段是 **`content`**、标签是 **`topic_tags`**，
> 跟搜索结果那边的 `desc` / `tag_list` 不一样。按后者取值不会报错，只会静默拿到
> 空正文 + 空标签。代码里已经两边都认，改这块时留意别改回去。

```toml
SOCIALDATAX_API_KEY = "..."
```

#### Supabase（必填）

```toml
SUPABASE_URL = "https://your-project.supabase.co"
SUPABASE_KEY = "your-anon-key"
```

### 换厂迁移（从 v0.31 及更早升上来）

v0.32.0 把 Claude / GPT / Gemini 三家全部换掉了。从老版本升级需要动 secrets：

**要加的**

```toml
MOONSHOT_API_KEY = "sk-..."     # 新增，主链路全靠它
```

**可以删的**（删不删都不影响运行，没有代码再读它们）

| 字段 | 原用途 | 现状 |
|------|--------|------|
| `VERTEX_EXPRESS_API_KEY` | Gemini 辅助层 | 辅助层已迁到 Kimi，`google-genai` 依赖也从 requirements 移除 |
| `VECTORENGINE_API_KEY` | 门下省 / 兵部的 gpt-5.5 | 改走 `deepseek-v4-flash` |
| `[claude_relay_presets.*]` / `ANTHROPIC_API_KEY` | Claude 中转 | 没有任何 stage 默认指向 `claude-*` |
| `GCP_PROJECT_ID` + `[gcp_service_account]` | Claude on Vertex | 同上 |

**不用动的**：`DEEPSEEK_API_KEY`、`SOCIALDATAX_API_KEY`、`SUPABASE_*` 原样保留。

几个迁移期的注意点：

- **没有 Claude 配置也能正常启动**。`init_api_config()` 不再强制要求
  `[claude_relay_presets.*]`——只要 `MOONSHOT_API_KEY` / `DEEPSEEK_API_KEY`
  至少有一个就能跑。
- **`model_overrides` 要清干净**。老 preset 里逐 stage 钉死的 `claude-opus-4-6-*`
  之类会**盖过** `pipeline/config.py` 的新默认值，导致换厂没生效却报「没配 Claude 接入」。
- **历史 run 的成本回显不受影响**。`COST_PER_1M_*` 里 Claude / GPT / Gemini 的旧条目
  特意保留着，只为让 DB 里已有的 stage_log 还能算出金额；没有任何新调用会用到它们。
- **想留 Claude 做对照**：保留 preset，再用 `model_overrides` 把想对比的 stage 钉回
  `claude-*` 即可，路由层仍然支持。

#### 辅助层（Kimi · 可选但默认开）

主链路之外的一层轻量复核，专治「主 critic 给 AI 腔内容发放水票」。开关在
`pipeline/config.py`：`ENABLE_KIMI_ASSIST = True`。

做四件事：

1. **网感二审**：主链路 critic 判 pass 的 cell 再过一遍；任一判 fail → 进重写
2. **结构审**：工部构建后检查 system_prompt 的 5 池 / 人设 / 合规完整性
3. **图片预转写**：上传的图转成文字进 brief（Vision）
4. **截图分析**：小红书截图等（Vision）

按岗位分模型（`KIMI_ASSIST_MODEL_OVERRIDES`）：**二审钉在 DeepSeek**——主链路的
vibe_critic 已经是 `kimi-k2.6`，二审再用同一个模型等于自己复核自己，分歧仲裁就
失去意义了；两个 Vision 岗位必须留在 Kimi，DeepSeek 这两档不接受图片输入。

**失败降级**：辅助层调用失败（未配置 / 限流 / 模型不存在）→ 打 warn 日志，流水线
继续走主判单判，不阻塞。它也不占用主链路的 run token 预算。

### 4. 启动

```bash
streamlit run app.py
```

## 项目结构

```
├── app.py                          # Streamlit 入口
├── pages/
│   ├── 1_dashboard.py              # 项目总览
│   ├── 2_new_project.py            # 新建项目
│   ├── 3_pipeline_detail.py        # 流水线详情
│   ├── 4_output_center.py          # 产出中心
│   └── 5_settings.py               # 设置
├── pipeline/
│   ├── orchestrator.py             # 主编排器
│   ├── config.py                   # 模型配置
│   ├── agents/                     # 各 Agent 实现
│   │   ├── crown_prince.py
│   │   ├── secretariat.py
│   │   ├── chancellery.py
│   │   ├── dispatcher.py
│   │   └── ministries/
│   └── prompts/                    # System Prompt (.md)
├── db/
│   ├── schema.sql                  # Supabase 表结构
│   └── supabase_client.py          # 数据库客户端
└── utils/
    ├── schema_models.py            # Pydantic 数据模型
    └── export.py                   # 导出工具
```

## 技术栈

- **Streamlit** — 全栈 Web 框架
- **Kimi (Moonshot) + DeepSeek** — AI 引擎，两家都走 Anthropic 兼容端点
- **SocialDataX MCP** — 第一方社媒数据（趋势取样 / 参考帖抓取）
- **Supabase** — 托管 PostgreSQL 数据库
- **Pydantic** — 数据校验
- **asyncio** — 并行执行六部任务

## 部署

### Streamlit Cloud（推荐）

1. 推送代码到 GitHub
2. 在 [streamlit.io](https://streamlit.io) 关联仓库
3. 在 Streamlit Cloud Secrets 中填入 API Keys
4. 自动部署

### Railway

1. 连接 GitHub 仓库
2. 填入环境变量
3. 按用量计费，$5/月起
