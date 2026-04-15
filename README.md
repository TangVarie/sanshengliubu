# 🏛️ 三省六部 · Prompt Engineering System

一套基于多 Agent 协作的 Prompt 工程生产系统。

**输入**：产品 brief / 场景需求 / 现有 prompt 迭代诉求
**输出**：一套完整的、可直接投入内容生产线使用的 Prompt 系统

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
| 太子 | 结构化用户输入 | Sonnet |
| 中书省 | 策略规划 | Opus |
| 门下省 | 独立审议（最多驳回2次） | Opus |
| 尚书省 | 任务派发 | Sonnet |
| 吏部 | 人设设计 | Sonnet |
| 户部 | 关键词策略 | Sonnet |
| 礼部 | 调性与平台适配 | Sonnet |
| 兵部 | 竞争策略 | Sonnet |
| 刑部 | 合规风控 | Sonnet |
| 工部 | 组装最终系统 | Opus |

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

`.streamlit/secrets.toml` 分两块：**Claude 模型接入** + **Supabase**。
Supabase 永远必填；Claude 接入在下面**二选一**（不要同时填）。

#### Claude 接入 · 二选一

系统启动时会自动判断使用哪种模式——只要检测到 `GCP_PROJECT_ID` 就走
Vertex AI，否则走 Anthropic 直连/中转。逻辑在
`pipeline/agents/__init__.py::init_api_config()`。

**方式 A：Anthropic 直连 / 中转**（最简单，适合个人开发）

```toml
ANTHROPIC_API_KEY = "sk-ant-your-key"
# 可选：走代理/中转站时填入 base URL，不填则直连官方 API
ANTHROPIC_BASE_URL = ""
```

- ✅ 零门槛，API Key 直接用
- ⚠️ 官方配额小，容易限流；中转站的 thinking / prompt caching 支持不一
- 💡 thinking 通过模型名后缀 `-thinking` 路由（中转站需支持）

**方式 B：Vertex AI**（企业/生产，已有 GCP 时推荐）

```toml
GCP_PROJECT_ID = "your-project-id"
GCP_REGION     = "asia-southeast1"   # 或其他支持 Claude 的 region

[gcp_service_account]
# 粘贴 service account JSON 内容（type / project_id / private_key / ... 字段全铺平）
```

- ✅ 配额大、合规性好、adaptive thinking 原生支持
- ⚠️ 需要先在 GCP 申请并开通 Claude on Vertex

#### Supabase（必填）

```toml
SUPABASE_URL = "https://your-project.supabase.co"
SUPABASE_KEY = "your-anon-key"
```

> 同时填了 A 和 B 会优先走 B（Vertex），A 的 key 会被忽略。要切换模式，
> 把不想用的那一块整块删掉或注释掉再重启。

#### Gemini 辅助（可选·推荐）

把 Gemini 当成 Claude 的"第二意见"——专治 Claude critic 给 AI 腔内容发放水票的问题。

```toml
# 从 GCP Console → Vertex AI → Settings → API keys 获得
VERTEX_EXPRESS_API_KEY = "AIzaSy..."
```

开关和模型在 `pipeline/config.py` 里：
- `ENABLE_GEMINI_ASSIST = True`（默认开）
- `GEMINI_MODEL = "gemini-3-pro-preview"`（用 Settings 页的"📋 列出可用 Gemini 模型"按钮先确认你这个 key 能访问到；常见可选：`gemini-3-pro-preview` / `gemini-3-flash-preview` / `gemini-2.5-pro` / `gemini-2.5-flash`）

Gemini 做两件事：
1. **网感二审**：Claude critic 判 pass 的 cell 再过一次 Gemini；任一判 fail → 进重写
2. **结构审**：工部构建后检查 system_prompt 的 5 池 / 人设 / 合规完整性

**失败降级**：Gemini 调用失败（未配置 / 限流 / 模型不存在）→ 打 warn 日志，流水线继续走 Claude 单判，不阻塞。

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
- **Anthropic Claude API** — AI 引擎 (Opus + Sonnet)
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
