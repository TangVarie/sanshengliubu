"""批量采样验收 —— 用 N>1 的样本验收一个「批量生成用的」prompt。

## 这个模块补的是什么缺口

交付物是 system_prompt,它的工作是产出 N≥10 篇。但在此之前,**每一道质量闸
看的都是每个 cell 唯一那篇 `demo_output`**:红蓝看它、画像模拟看它、网感
critic 看它、二审看它、消费者模拟看它、终审看它。11 道闸,1 个样本。

这带来三个后果:

1. **5 池 + 人设轮换机制从来没被验证过。** 决定"第 7 篇会不会和第 3 篇一个味"
   的就是这套机制,而它在流水线里唯一受到的检查是 `kimi_structure_reviewer`
   数一数"这 5 个池的文字在不在 prompt 里"。**存在 ≠ 有效。**
2. **n=1 测的是 demo 的质量,不是 prompt 的分布。** 那篇 demo 经过红蓝精修、
   网感重写、结构补漏最多 3 轮打磨 —— 它反映的是"这 8 道工序能把一篇文章修到
   多好",不是"这段 prompt 平均能生成什么"。这两件事的相关性远比直觉低。
3. **优化方向是反的。** 目标是上限(100 篇里 5 篇爆款 > 100 篇都 85 分),
   但红蓝精炼和网感重写这两个最贵的环节干的事恰恰是把唯一那篇样本往均值推。
   在 n=1 下,没有任何机制能区分"抹掉了尾部烂篇"和"抹掉了头部爆款"。

## 做法

拿建好的 system_prompt 真跑 N 篇(默认 5),**只变 `{{seed}}`**,其余变量固定。
只变 seed 是有意的:那正是 `works_builder.md` 批量生成规则里承诺的差异化开关
(「相邻两篇的 seed 值建议间隔 ≥ 20」)。如果变了 seed 产出还是一个味,说明
5 池轮转是写在纸上的 —— 这是这个模块最想抓的东西。

## 观测,不拦

采样结果**不参与出货判决**:不阻塞、不触发重写、不影响 verdict。

理由是本仓库现在还没有历史基线。没有分布数据就设阈值,等于凭猜调参 —— 那正是
这一整轮改造要治的病。先攒几条 run 的真实分布,再决定阈值定在哪、要不要升级成
闸门。

## 成本

采样调用很便宜:system_prompt 走 prompt cache(K2.6 命中价省 83%),每次只出
一篇内容(几百字输出)。12 格子 × 5 篇 ≈ 60 次调用 ≈ $0.15-0.3/run。

走辅助层(`kimi_client`)而不是主链路 BaseAgent,和二审/结构审同一条路径:
不占 `MAX_TOKENS_PER_RUN` 预算、失败一律降级不阻塞。采样是 advisory,
绝不能因为采样失败把一条已经跑完的 run 判死。
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from pipeline.config import (
    BATCH_SAMPLE_CONCURRENCY,
    BATCH_SAMPLE_MAX_CELLS,
    BATCH_SAMPLE_MAX_OUTPUT_TOKENS,
    BATCH_SAMPLE_N,
    BATCH_SAMPLE_SEED_STEP,
    PRIMARY_MODEL,
)
from pipeline.quality_metrics import (
    check_craft,
    check_redlines,
    first_sentence,
    _strip_trailing_hashtags,
)

logger = logging.getLogger(__name__)


# ── 多样性度量(全部纯 Python,零成本)────────────────────────────────────

def _shingles(text: str, k: int = 3) -> set[str]:
    """字符级 k-gram。中文没有词边界,字符 trigram 是最省事又够用的相似度基底。"""
    t = re.sub(r"\s+", "", text)
    if len(t) < k:
        return {t} if t else set()
    return {t[i : i + k] for i in range(len(t) - k + 1)}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def measure_diversity(samples: list[str]) -> dict[str, Any]:
    """量一批样本之间的差异化程度。

    三个指标,对应 `works_builder.md` 批量生成规则里三条可验证的承诺:

    - `unique_opening_ratio` — 对应「N 篇内容的第一句话不可重复」
    - `max_pairwise_similarity` — 对应「相邻两篇的组合至少有 3 个维度不同」。
      用字符 trigram 的 Jaccard 近似:两篇正文 trigram 重合度越高,说明轮转
      越没起作用。
    - `mean_pairwise_similarity` — 整批的趋同程度。看**均值**是因为单看最大值
      会被一对异常样本主导。

    这里**只报数字不判定合格与否** —— 阈值要等真实分布攒出来再定,现在设
    等于凭猜。
    """
    bodies = [_strip_trailing_hashtags(s) for s in samples if s and s.strip()]
    n = len(bodies)
    if n < 2:
        return {
            "n": n,
            "unique_openings": n,
            "unique_opening_ratio": 1.0 if n else 0.0,
            "max_pairwise_similarity": None,
            "mean_pairwise_similarity": None,
            "_note": "样本不足 2 篇,无法测多样性",
        }

    openings = [first_sentence(b) for b in bodies]
    uniq_open = len({o for o in openings if o})

    shingle_sets = [_shingles(b) for b in bodies]
    sims: list[float] = []
    worst_pair: tuple[int, int] | None = None
    worst_val = -1.0
    for i in range(n):
        for j in range(i + 1, n):
            s = _jaccard(shingle_sets[i], shingle_sets[j])
            sims.append(s)
            if s > worst_val:
                worst_val, worst_pair = s, (i, j)

    return {
        "n": n,
        "unique_openings": uniq_open,
        "unique_opening_ratio": round(uniq_open / n, 4),
        "max_pairwise_similarity": round(max(sims), 4),
        "mean_pairwise_similarity": round(sum(sims) / len(sims), 4),
        "most_similar_pair": list(worst_pair) if worst_pair else None,
        "duplicate_openings": sorted(
            {o for o in openings if o and openings.count(o) > 1}
        ),
    }


# ── 单 cell 的采样 + 分析 ────────────────────────────────────────────────

def build_sample_user_message(cell: dict, brief: dict, seed: int) -> str:
    """按 cell 的 `user_prompt_template` 填变量,生成一次采样的 user message。

    **只变 seed**,topic / persona 固定:
    - `topic` 取 brief 的 core_claim / product_name,让 N 篇同题可比
    - `persona` **留空**,交给 system_prompt 里内置的人设轮换规则自己挑 ——
      如果我们手动指定人设,就等于替 prompt 做了它本该做的事,那条规则也就
      测不出来了
    - `seed` 按 BATCH_SAMPLE_SEED_STEP 递增(默认 20,对齐 works_builder.md
      「相邻两篇 seed 间隔 ≥ 20」的承诺)

    模板缺失时退回一个最小 message,保证采样链路不因为模板格式变化而断掉。
    """
    topic = (
        brief.get("core_claim")
        or brief.get("product_name")
        or brief.get("product_category")
        or "按本 prompt 的方向自行确定主题"
    )
    if isinstance(topic, list):
        topic = "、".join(str(t) for t in topic[:3])

    tpl = (cell.get("user_prompt_template") or "").strip()
    if not tpl:
        return f"主题：{topic}\n差异化种子：{seed}\n请按 system prompt 输出一篇内容。"

    filled = tpl
    for key, val in (
        ("topic", str(topic)),
        ("seed", str(seed)),
        ("persona", ""),          # 有意留空,见上面的说明
    ):
        filled = re.sub(r"\{\{\s*" + key + r"\s*\}\}", val, filled)
    # 模板里可能还有别的变量({{brand}} / {{scene}} 等),统一清成空串,
    # 否则字面量 "{{brand}}" 会被执行模型当成要写进正文的内容。
    filled = re.sub(r"\{\{[^}]*\}\}", "", filled)
    return filled


async def sample_one_cell(
    cell: dict,
    brief: dict,
    n: int,
    semaphore: asyncio.Semaphore,
) -> dict[str, Any]:
    """对一个 cell 跑 n 次采样并分析,返回该 cell 的采样报告。

    失败的单次采样不算废掉整个 cell —— 拿到几篇算几篇,`n_ok` 如实反映。
    """
    from pipeline.agents.kimi_client import (
        KimiCallFailed,
        KimiNotConfigured,
        call_kimi_text,
    )

    cid = cell.get("cell_id", "?")
    sp = cell.get("system_prompt") or ""
    if not sp.strip():
        return {"cell_id": cid, "status": "skipped", "reason": "system_prompt 为空"}

    def _call_under_limiter(system_prompt: str, user_message: str) -> dict:
        """在**主链路的限流器**下发这次采样调用。

        v0.33.8 修正:此前采样完全绕开 `_SlidingWindowLimiter` —— 辅助层
        (kimi_client)本来就不走主限流器,那对二审/结构审这种一次一两个调用的
        岗位没问题,但采样是 60 次。

        后果是两条链共用同一个上游配额、却只有一条在自律:采样打出去的 429 会
        让主链路的自适应限流器误以为**自己**太快,于是把速率减半 —— 主链路被
        一个它管不着的旁路拖慢了。

        ⚠️ v0.34.1:限流器必须包住**每一次真实请求**,不能包住整个带重试的
        helper。`call_kimi_text` 内部默认还有 3 次重试,包在外面的话:三次请求
        只占一个滑动窗口时间戳、只占一个并发槽,而且采样侧的 429 永远不会调到
        限流器的自适应退避钩子。结果是限流被 429 触发时最需要它的那一刻恰好
        失效 —— 60 个逻辑样本可以打出多达 180 次真实请求,而限流器以为只有 60。
        所以这里把内层重试关掉(max_attempts=1),自己做退避,每次尝试单独取槽,
        并在 429 时显式通知限流器退避。

        `slot()` 是同步上下文管理器,而本函数已经在 to_thread 里跑,直接 with
        即可。限流器不可用时(Vertex 模式会返回 None)退回裸调用。
        """
        import time as _time
        from pipeline.agents import _get_active_limiter

        # ⚠️ v0.34.2:只重试**瞬时**故障,而且**直接复用 llm_retry 的分类器**,
        # 不自己手写一个。
        #
        # 上一版我把内层 call_with_retry 关掉后用宽泛 except 接管重试,丢掉了
        # 分类;修的时候又手写了一张"瞬时标记"表 —— 那张表把所有 429 都当可重试,
        # 而 `_is_transient` 里早就有一组**欠费停号**指纹(insufficient balance /
        # please recharge / account is suspended / 余额不足),它们披着 429 的皮
        # 但充值前永远不会成功。
        #
        # 这不是个新问题:v0.32.5 就是专门修这个的(原注记:"账户欠费被停不再当
        # 普通限流重试…余额烧干后终审对着 suspended 账户重试 3 轮 × 3 个阶段")。
        # 我在一条新代码路径上把它重新引入了一遍。教训很直接:**已有的分类器就用,
        # 不要手写平行实现** —— 平行实现必然缺掉原版里那些踩坑攒出来的例外。
        from pipeline.llm_retry import _is_transient

        _lim = _get_active_limiter()
        _last: Exception | None = None
        for _attempt in range(3):
            try:
                if _lim is None:
                    return call_kimi_text(
                        system_prompt, user_message,
                        model=PRIMARY_MODEL,
                        max_output_tokens=BATCH_SAMPLE_MAX_OUTPUT_TOKENS,
                        max_attempts=1,
                    )
                with _lim.slot(stage_name="batch_sampling"):
                    return call_kimi_text(
                        system_prompt, user_message,
                        model=PRIMARY_MODEL,
                        max_output_tokens=BATCH_SAMPLE_MAX_OUTPUT_TOKENS,
                        max_attempts=1,
                    )
            except Exception as _e:
                _last = _e
                _msg = str(_e).lower()
                # 采样侧撞到限流也要让限流器知道 —— 不通知的话它只按主链路的
                # 429 调速,而采样正是那个把配额吃掉的旁路。
                if _lim is not None and (
                    "429" in _msg or "rate" in _msg or "too many" in _msg
                ):
                    try:
                        _lim.note_rate_limited(stage_name="batch_sampling")
                    except Exception:
                        pass
                # 永久性错误立刻抛,不要空等 6 秒重试三次
                if not _is_transient(_e):
                    raise
                if _attempt < 2:
                    _time.sleep(2.0 * (2 ** _attempt))
        raise _last if _last else RuntimeError("采样调用失败(未知原因)")

    async def _one(idx: int) -> tuple[str | None, dict, str | None]:
        seed = 1 + idx * BATCH_SAMPLE_SEED_STEP
        msg = build_sample_user_message(cell, brief, seed)
        async with semaphore:
            try:
                # 辅助层的 client 是同步的,而这里要跑几十次 —— 直接在事件循环里
                # 阻塞会把采样串行化(60 次 × 数秒 = 好几分钟)。丢进线程池换回
                # 并发。这是本模块和其它辅助层调用点唯一的写法差异,原因就是量级。
                res = await asyncio.to_thread(
                    _call_under_limiter,
                    sp,
                    msg,
                )
                return res.get("text", ""), res, None
            except (KimiNotConfigured, KimiCallFailed) as e:
                return None, {}, f"{type(e).__name__}: {e}"
            except Exception as e:  # noqa: BLE001 — advisory,绝不外抛
                return None, {}, f"{type(e).__name__}: {e}"

    results = await asyncio.gather(*[_one(i) for i in range(n)])

    # (seed, text) 一起存 —— 只存 text 的话,某次调用失败后列表被压缩,
    # 后面 enumerate 会给成功的样本安上**错的 seed**(seed 1 失败、seed 21 成功
    # 时,报告会把那篇记成 seed 1)。而 per-seed 诊断正是用来判断 prompt 的
    # seed 轮转有没有生效的,记错了这层诊断就废了。
    seeded: list[tuple[int, str]] = []
    errors: list[str] = []
    cost = 0.0
    in_tok = out_tok = 0
    for _idx, (text, usage, err) in enumerate(results):
        if err:
            errors.append(err)
            continue
        if text:
            seeded.append((1 + _idx * BATCH_SAMPLE_SEED_STEP, text))
        cost += float(usage.get("cost_usd", 0.0) or 0.0)
        in_tok += int(usage.get("input_tokens", 0) or 0)
        out_tok += int(usage.get("output_tokens", 0) or 0)

    samples = [t for _, t in seeded]

    # ⑨ 样本太少不算"采样成功"。只回来 1 篇时 measure_diversity 会报
    # 首句去重率 100%(一篇当然不重复),矩阵层和 UI 会把它当健康数据收下 ——
    # 而这恰恰最容易发生在限流/部分故障的时候,于是 baseline 被污染成
    # "越不稳定看起来越好"。至少要 3 篇才谈得上分布。
    if len(samples) < min(3, n):
        return {
            "cell_id": cid,
            "status": "partial",
            "n_requested": n,
            "n_ok": len(samples),
            "reason": (
                f"只取回 {len(samples)}/{n} 篇,不足以测分布 —— 本 cell 不计入"
                f"聚合指标(多为限流或部分故障)"
            ),
            "errors": errors[:3],
            "_usage": {"cost_usd": cost, "input_tokens": in_tok,
                       "output_tokens": out_tok},
        }

    if not samples:
        return {
            "cell_id": cid,
            "status": "failed",
            "n_requested": n,
            "n_ok": 0,
            "errors": errors[:3],
            "_usage": {"cost_usd": cost, "input_tokens": in_tok, "output_tokens": out_tok},
        }

    # ── 逐篇红线扫描 ────────────────────────────────────────────────
    # 注意和 quality_metrics.score_matrix 的差别:那边判的是**唯一那篇 demo**,
    # 这边判的是**这个 prompt 真跑出来的一批**。同一套红线规则,不同的样本空间
    # —— 这正是本模块存在的意义。
    per_sample: list[dict] = []
    clean = 0
    tally: dict[str, int] = {}
    paradigm = cell.get("paradigm")
    craft_ok = 0
    for i, (_seed, s) in enumerate(seeded):
        v = check_redlines(s)
        for x in v:
            tally[x["rule"]] = tally.get(x["rule"], 0) + 1
        if not v:
            clean += 1
        missing = check_craft(s, paradigm)
        if not missing:
            craft_ok += 1
        per_sample.append({
            "idx": i,
            "seed": _seed,          # 真实发出去的 seed,不是列表下标推算的
            "opening": first_sentence(_strip_trailing_hashtags(s))[:60],
            "chars": len(s),
            "redline_violations": [x["rule"] for x in v],
            "craft_missing": missing,
        })

    n_ok = len(samples)
    return {
        "cell_id": cid,
        "platform": cell.get("platform", ""),
        "direction_id": cell.get("direction_id", ""),
        "paradigm": paradigm or "A_emotional_hook(默认)",
        "status": "ok",
        "n_requested": n,
        "n_ok": n_ok,
        # 红线层:追 100%
        "redline_clean_samples": clean,
        "redline_pass_rate": round(clean / n_ok, 4),
        "redline_violation_tally": tally,
        # 工艺层:按范式判的四要素,看绝对数
        "craft_complete_samples": craft_ok,
        # 多样性:这是 n=1 永远测不到的那一维
        "diversity": measure_diversity(samples),
        "per_sample": per_sample,
        "errors": errors[:3],
        "_usage": {"cost_usd": cost, "input_tokens": in_tok, "output_tokens": out_tok},
    }


# ── 全 matrix 编排 ───────────────────────────────────────────────────────

async def run_batch_sampling(
    prompt_cells: list[dict],
    brief: dict,
    *,
    n: int | None = None,
) -> dict[str, Any]:
    """对整个 prompt_matrix 跑批量采样,返回可落库的报告。

    永不抛异常 —— 任何失败都退化成 `{"status": "skipped"/"failed", ...}`。
    采样是 advisory,不能因为它把一条跑完的 run 判死。
    """
    cells = [c for c in (prompt_cells or []) if (c.get("system_prompt") or "").strip()]
    if not cells:
        return {"status": "skipped", "reason": "没有可采样的 cell"}

    n = n or BATCH_SAMPLE_N
    # 上限保护:格子数被策略升级刷到很多时,采样量是 cells × n 的乘积。
    # 截断要**显式报告**,不能让"只采了前 12 个"看起来像"全采了"。
    truncated: list[str] = []
    if BATCH_SAMPLE_MAX_CELLS and len(cells) > BATCH_SAMPLE_MAX_CELLS:
        truncated = [c.get("cell_id", "?") for c in cells[BATCH_SAMPLE_MAX_CELLS:]]
        cells = cells[:BATCH_SAMPLE_MAX_CELLS]
        logger.warning(
            "[batch_sample] 格子数 %d 超过采样上限 %d,只采前 %d 个;"
            "未采样: %s",
            len(truncated) + BATCH_SAMPLE_MAX_CELLS, BATCH_SAMPLE_MAX_CELLS,
            BATCH_SAMPLE_MAX_CELLS, truncated,
        )

    sem = asyncio.Semaphore(BATCH_SAMPLE_CONCURRENCY)
    try:
        reports = await asyncio.gather(
            *[sample_one_cell(c, brief, n, sem) for c in cells],
            return_exceptions=True,
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("[batch_sample] 编排层失败(non-fatal)")
        return {"status": "failed", "reason": f"{type(e).__name__}: {e}"}

    ok_reports = [r for r in reports if isinstance(r, dict) and r.get("status") == "ok"]
    bad = [r for r in reports if not (isinstance(r, dict) and r.get("status") == "ok")]

    if not ok_reports:
        # ⚠️ partial 的 cell 可能已经成功付费生成过一两篇 —— 全部 cell 都走
        # partial 时不能直接返回一个没有 _usage 的失败结构,否则 orchestrator
        # 上报成本为 0,而钱是真花了。本模块 docstring 承诺过"成本单独上报,
        # 悄悄花钱比花钱更糟",这里必须兑现。
        _bad_cost = sum(
            float((r.get("_usage") or {}).get("cost_usd", 0.0))
            for r in bad if isinstance(r, dict)
        )
        _bad_in = sum(
            int((r.get("_usage") or {}).get("input_tokens", 0))
            for r in bad if isinstance(r, dict)
        )
        _bad_out = sum(
            int((r.get("_usage") or {}).get("output_tokens", 0))
            for r in bad if isinstance(r, dict)
        )
        return {
            "status": "failed",
            "reason": (
                "没有任何 cell 取回足够样本(多半是辅助层未配置、限流、或"
                "部分故障)。已发生的调用成本仍如实上报。"
            ),
            "cells_attempted": len(cells),
            "cells_partial": sum(
                1 for r in bad
                if isinstance(r, dict) and r.get("status") == "partial"
            ),
            "sample_errors": [
                (r.get("errors") or [str(r)])[:1] for r in bad
                if isinstance(r, dict)
            ][:3],
            "_usage": {
                "cost_usd": round(_bad_cost, 5),
                "input_tokens": _bad_in,
                "output_tokens": _bad_out,
            },
        }

    total_samples = sum(r["n_ok"] for r in ok_reports)
    total_clean = sum(r["redline_clean_samples"] for r in ok_reports)
    total_craft = sum(r["craft_complete_samples"] for r in ok_reports)
    agg_tally: dict[str, int] = {}
    for r in ok_reports:
        for k, v in (r.get("redline_violation_tally") or {}).items():
            agg_tally[k] = agg_tally.get(k, 0) + v

    # 多样性的整体口径取**各 cell 的最差值**,不取均值:一个格子严重趋同就是
    # 一个格子的批量废掉了,被别的格子的好成绩平均掉就看不见了。
    sims = [
        r["diversity"]["max_pairwise_similarity"]
        for r in ok_reports
        if r.get("diversity", {}).get("max_pairwise_similarity") is not None
    ]
    open_ratios = [
        r["diversity"]["unique_opening_ratio"] for r in ok_reports
    ]

    cost = sum(float(r.get("_usage", {}).get("cost_usd", 0.0)) for r in reports
               if isinstance(r, dict))
    in_tok = sum(int(r.get("_usage", {}).get("input_tokens", 0)) for r in reports
                 if isinstance(r, dict))
    out_tok = sum(int(r.get("_usage", {}).get("output_tokens", 0)) for r in reports
                  if isinstance(r, dict))

    return {
        "status": "ok",
        "mode": "observe_only",   # 不参与出货判决,见模块 docstring
        "n_per_cell": n,
        "cells_sampled": len(ok_reports),
        "cells_failed": len(bad),
        "cells_not_sampled": truncated,
        "total_samples": total_samples,
        # ── 红线层:追 100% ──────────────────────────────────────────
        "redline_clean_samples": total_clean,
        "redline_pass_rate": round(total_clean / total_samples, 4) if total_samples else 0.0,
        "redline_violation_tally": agg_tally,
        # ── 工艺层:绝对数 ──────────────────────────────────────────
        "craft_complete_samples": total_craft,
        # ── 多样性:n=1 永远测不到的那一维 ──────────────────────────
        "worst_cell_max_similarity": round(max(sims), 4) if sims else None,
        "worst_cell_unique_opening_ratio": round(min(open_ratios), 4) if open_ratios else None,
        "per_cell": ok_reports,
        "_usage": {
            "cost_usd": round(cost, 5),
            "input_tokens": in_tok,
            "output_tokens": out_tok,
        },
    }
