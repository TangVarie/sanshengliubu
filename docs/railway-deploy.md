# Railway 部署指南(Web + Worker 双服务)

执行层已从 Streamlit 进程剥离:UI 只入队(`pipeline_runs.status='pending'`),
独立 worker 进程认领执行。Web 容器重启/重发布不再杀死在跑的流水线;worker
被回收后,下一个实例会自动接续心跳停跳的 run(无需有人打开页面手动点继续)。

## 0. 前置:数据库迁移

在 Supabase SQL Editor 执行(幂等):

```
db/migrations/007_worker_queue.sql
```

(或重跑最新的 `db/schema.sql`。)worker 启动时会自检该列,缺失会响亮报错。

## 1. 创建两个服务(同一个 GitHub 仓库)

在 Railway 项目里 **New Service → GitHub Repo** 各建一次:

| 服务 | Start Command(Settings → Deploy) | 说明 |
|---|---|---|
| `web` | `streamlit run app.py --server.port $PORT --server.address 0.0.0.0` | 控制面 UI;需要开 Public Networking 生成域名 |
| `worker` | `python -m worker` | 执行面;**不要**开 Public Networking |

仓库根目录的 `Procfile` 已带 `web:` 进程,web 服务不填 Start Command 也可。

建议把 worker 的 **Deploy → Restart Policy** 保持默认(Always),
**Deploy → Overlap / Draining** 的宽限期(graceful shutdown)调到 300 秒:
SIGTERM 后 worker 停止认领、原地等在跑 run 到宽限期结束,能多完成一个
stage 就少一次重跑。

**Replicas 保持 1**:项目级互斥有 CAS 保护,但限流器/预算是进程内的,
多副本会各自持有完整 RPM 配额(审计 ROB-014)。

## 2. 环境变量(两个服务都配,Shared Variables 最省事)

| 变量 | 必填 | 说明 |
|---|---|---|
| `SUPABASE_URL` | ✅ | 同 secrets.toml |
| `SUPABASE_KEY` | ✅ | 同 secrets.toml |
| `MOONSHOT_API_KEY` | ✅* | Kimi 主链路(*与 DeepSeek 至少一个) |
| `DEEPSEEK_API_KEY` | ✅* | DeepSeek 对抗/廉价档 |
| `MOONSHOT_BASE_URL` | | 仅 .ai 站 key 需要覆盖 |
| `DEEPSEEK_BASE_URL` | | 一般不用 |
| `SOCIALDATAX_API_KEY` | | 趋势取样(PRE 默认 required,不配会导致新 run 失败,见 pipeline/config.py) |
| `PIPELINE_EXECUTION_MODE` | ✅ | **web 服务设为 `worker`**(让 UI 入队而不是起线程)。worker 进程不读它。 |
| `WORKER_POLL_SECONDS` | | worker 轮询间隔,默认 5 |
| `WORKER_MAX_CONCURRENT_RUNS` | | 默认 1 |
| `WORKER_ZOMBIE_SCAN_SECONDS` | | 僵尸扫描间隔,默认 30 |
| `WORKER_AUTO_RESUME` | | `0` 关闭自动接续(默认开) |

密钥解析次序:st.secrets(若有 secrets.toml)→ 环境变量。Railway 上不需要
也不建议再放 secrets.toml。

## 3. 行为差异速查

| 场景 | thread 模式(旧) | worker 模式 |
|---|---|---|
| 新建/重跑/补充重跑 | UI 进程起 daemon 线程 | run 入队(`pending`/「排队中」),worker 认领 |
| Web 容器重启 | 在跑 run 全部死亡,僵尸等 reaper | 不受影响(执行在 worker) |
| worker 重启/回收 | — | 心跳停跳 ≥75s 后被新实例自动重排队接续 |
| 强制取消 | run→failed,线程下个检查点自杀 | 同左;排队中(pending)的 run 也会被取消 |
| 澄清请旨 | 页面提交 human_intervention | 完全相同(worker 轮询 DB) |

回滚:把 web 的 `PIPELINE_EXECUTION_MODE` 删掉或设 `thread`,并停掉 worker
服务,即回到单进程模式;队列里残留的 pending run 用详情页「强制终止」清理。

## 4. 已知限制(v1)

- 双执行防护依赖「停机时不重排队 + 心跳过期才接续」:不要同时跑两个
  worker 副本,也不要 worker 模式和 thread 模式混用同一个数据库。
- 等待澄清(paused_for_review)的 run 若恰逢 worker 被强杀,复活后该
  澄清阶段会重新执行并再次请旨(已提交过的答复不会自动重放)。
- 页面 reaper 与 worker 自动接续并存,条件更新谁先命中谁生效:若 reaper
  先把僵尸标成 failed,仍需手动点「继续执行」(与旧行为一致)。
