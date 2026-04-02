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

### 3. 配置 Secrets

```bash
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
```

编辑 `.streamlit/secrets.toml`：

```toml
ANTHROPIC_API_KEY = "sk-ant-your-key"
SUPABASE_URL = "https://your-project.supabase.co"
SUPABASE_KEY = "your-anon-key"
```

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
