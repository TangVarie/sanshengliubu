"""独立流水线执行进程(worker)。

`python -m worker` 启动。职责:
  - 轮询 pipeline_runs 里 status='pending' 的 run(UI 在 worker 模式下
    只入队不执行),按项目 CAS 认领后复用 orchestrator 的既有执行机制;
  - 周期扫描心跳过期的 running/paused 僵尸 run,自动重排队接续
    (进程/容器被回收后不再依赖"下次有人打开页面 + 手动点继续");
  - SIGTERM 优雅停机:停止认领,原地等待在跑线程直到平台宽限期结束。
    刻意**不**在停机时立刻重排队 —— 否则滚动发布时新实例可能在旧进程
    还活着时认领同一 run,出现双执行。旧进程真死后心跳停跳,新实例的
    僵尸扫描会接手。

详见 docs/railway-deploy.md。
"""
