"""机审闸 —— 所有「机械可判」的文字指纹检测,单篇 + 跨篇。

## 为什么单独立一层

去 AI 味的规则此前以**禁令清单**形态散在礼部产出和工部装配的 prompt 里。
这个形态有三个代价:

1. **信号稀释** —— 生成模型的注意力从"写"转到"查"。单 prompt 规则超过 30 条
   后尤其明显(和 works_builder 字符预算是同一个问题的两面)
2. **防御性写作** —— 模型一边写一边躲清单,稿子会紧、会平、不敢下判断
3. **让 LLM 查禁词是用判断力干正则的活** —— 既烧 token,判断还带方差
   ("critic 这次没看出破折号")

所以本模块的定位是:**机械可判的下沉到这里,需要判断的留给 vibe_critic,
生成 prompt 里只放正向目标 + 少量带原因的禁令。**

一个可操作的判断标准:想往任何生成 prompt 里加一条新禁令时,先问这条能不能
被正则抓 —— 能,就放这里,prompt 省下这一条的注意力预算。

## 三档分级,以及为什么必须分三档

| 档 | 判定 | 后果 |
|----|------|------|
| HARD | 命中即 fail | 走定点改写 → 复扫(免费) |
| SOFT | 命中记标 | 不 fail,喂给 critic 当参考 |
| BATCH | 跨 cell 分布 | 单篇不判,只在 matrix 层看占比 |

**第三档是本模块最要紧的设计,不是可选项。**

小红书有大量自己的方言 —— 「家人们」「谁懂」「闭眼入」「救了我的」。
`foundation.md` 明确把这些列为**平台暗号词**,是真实感信号:

> 平台暗号词:「绝绝子」「真的会谢」「无语子」「家人们」「谢邀」「评评理」

这些词**单篇出现是人味,十篇都有才是流水线**。把它们放进单篇硬禁,等于让
机审闸和 foundation.md 打架 —— 而 foundation.md 是对的,那是平台原生语言。
所以它们只能进 BATCH 档,按**跨 cell 占比**判。

同一个词在不同层里是不同性质的东西,这一点在移植任何外部规则表时最容易搞错。

## 为什么自建规则表而不是移植现成脚本

外部的中文 AI 腔检测脚本(如 human-writing 的 check_prose.py)校准场景是
知乎长文 / 公众号,和小红书种草差异很大。直接移植至少有三处会误伤:

- **冒号全禁** —— 小红书正文「成分:」「价格:」「适合:」是常规写法,
  标题「XX:一定要看」更是平台原生句式。这条需要重写规则而不是加豁免开关
- **家人们 / 姐妹们** —— 见上,和 foundation.md 直接冲突
- **谁懂 / 闭眼入 / 救了我的** —— 小红书自己的方言,单篇禁掉等于禁掉平台语言

所以这里只借**规则家族的思路**(翻案句 / 商业黑话 / 伪精确 / 跨篇指纹),
具体词表按小红书重新校准。

## 和 quality_metrics 的关系

`quality_metrics.check_redlines` 的模式检测**委托给本模块**,保证一套词表
一个来源。此前评分器自己带了一份 AI 空话黑名单,再加一份就会出现
"分数还在涨、闸门已经按新规矩走"的漂移 —— 这正是 architecture.md 第 5 节
记的同步义务想防的事。
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════
# HARD —— 命中即 fail。收录标准:在小红书语境下**假阳性率极低**
# ══════════════════════════════════════════════════════════════════════

# ── AI 空话黑名单 ────────────────────────────────────────────────────
# 同源出处(改一边要同步):
#   - vibe_critic.md 第 0.5 步「AI 空话硬否决」
#   - works_builder.md 范式 A (c) + 范式 B「反 AI 腔禁用清单」
#   - foundation.md「反面教材——这些都是伪网感」
AI_CLICHE_BLACKLIST: tuple[str, ...] = (
    "效果显著", "性价比高", "值得推荐", "适合所有人", "温和不刺激",
    "希望对你有帮助", "综上所述", "总而言之",
    "让我们一起", "姐妹们冲", "快快收藏",
    "分享几个小技巧", "记住这3点", "记住这三点", "以下几个要点",
)

# ── 翻案句家族(新增)──────────────────────────────────────────────────
# 中文 AI 腔最稳定的句法指纹。真人说话极少用这种对仗式转折 —— 它是模型为了
# 显得"有洞察"而生成的句式模板,一批内容里反复出现比任何单个词都刺眼。
#
# ⚠️ 有意**不收**「不只是X还Y」:那是完全自然的中文("不只是便宜还好用"),
# 精度不够。宁可漏一条也不误伤 —— 本仓库 v0.32.3/v0.32.4 两次三轮空烧都是
# 误判造成的。
PIVOT_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    ("不是X而是Y", re.compile(r"(?:并)?不是[^。！？\n]{1,60}而是")),
    ("并非X而是Y", re.compile(r"并非[^。！？\n]{1,60}而是")),
    ("不在于X而在于Y", re.compile(r"不在于[^。！？\n]{1,60}而在于")),
    ("与其说X不如说Y", re.compile(r"与其说[^。！？\n]{1,60}不如说")),
    ("看似X其实Y", re.compile(r"看似[^。！？\n]{1,60}(?:其实|实际上?|实则)")),
    ("表面上X其实Y", re.compile(r"表面(?:上)?[^。！？\n]{1,60}(?:其实|实际上?|实则)")),
    ("X的本质是Y", re.compile(r"的本质(?:并)?不是[^。！？\n]{1,40}而是")),
)

# ── 商业黑话 / 汇报腔 ────────────────────────────────────────────────
# 这些词出现在素人种草笔记里,身份立刻穿帮 —— 没有哪个真实用户会说
# "这款产品赋能了我的护肤闭环"。在小红书语境下精度接近 100%。
HARD_JARGON: tuple[str, ...] = (
    "赋能", "抓手", "闭环", "拉通", "对齐颗粒度", "底层逻辑",
    "打法", "心智", "势能", "组合拳", "护城河",
    "值得注意的是", "综上", "由此可见", "不难看出", "众所周知",
)

# ── 伪精确行为量 ──────────────────────────────────────────────────────
# 「停留了 1.7 秒」「用了 3.5 次」—— 真人不会这样记自己的行为。
#
# ⚠️ 单位表**只收时间和次数**,有意不收 克/毫升/斤/% —— 那些是产品规格
# (0.5 克烟酰胺 / 2.5% 水杨酸),属于事实层,照常精确。文档原始口径里
# 「产品规格数豁免」如果靠上下文判断很难做准,按单位切分是确定性的做法。
PSEUDO_PRECISE_PATTERN = re.compile(
    r"\d+\.\d+\s*(?:秒|分钟|小时|天|周|个月|年|次|遍|顿|口)"
)

# ── 禁止开场(只扫第一句)───────────────────────────────────────────────
BANNED_OPENING_PREFIXES: tuple[str, ...] = (
    "今天给大家分享", "今天就给大家", "今天来聊聊", "今天想跟大家",
    "作为一个", "作为一名", "身为一个",
    "大家好", "Hi 姐妹们", "hi姐妹们", "嗨姐妹们", "姐妹们好",
    "在如今", "在当今", "在这个", "随着",
    "首先", "第一点",
)

# ── 列表体正文 ────────────────────────────────────────────────────────
_LIST_BODY_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"(?:^|\n)\s*[1-9１-９][.、．)）]\s*\S"),
    re.compile(r"(?:^|\n)\s*第[一二三四五六七八九]\s*[点条]"),
)
_SEQUENCE_MARKERS: tuple[str, ...] = ("首先", "其次", "再次", "最后", "综上")


# ══════════════════════════════════════════════════════════════════════
# SOFT —— 记标不 fail,喂给 critic 当参考
# ══════════════════════════════════════════════════════════════════════

# 破折号。放 SOFT 不放 HARD:小红书笔记确实会用「——」做强调,不是纯 AI 指纹,
# 精度不够上硬禁。但一批里频繁出现仍然是信号。
_EM_DASH = re.compile(r"[—–]{1,2}")

# 洞察路标词。这类词本身没错,密集出现说明模型在用信号词推进段落而不是靠内容。
ROAD_SIGNS: tuple[str, ...] = (
    "其实", "说白了", "换句话说", "也就是说", "更重要的是",
    "关键在于", "问题在于", "真正的", "本质上",
)

# 第二人称密度。种草内容用「你」是正常的,但每百字 >2 且总量 ≥6 通常意味着
# 稿子在说教而不是分享。
_YOU_CHAR = re.compile(r"你")


# ══════════════════════════════════════════════════════════════════════
# BATCH —— 跨 cell 分布。单篇不判,只看占比
#
# ⚠️ 本档收的全是**小红书原生语言**。它们单篇出现是人味(foundation.md 明确
# 把「家人们」列为平台暗号词、是真实感信号),十篇都有才是流水线指纹。
# 放错档会让机审闸和 foundation.md 打架。
# ══════════════════════════════════════════════════════════════════════

# 共用插入词 —— 一批里 ≥60% 的 cell 都插同一句,就是同批指纹
BATCH_INSERT_PHRASES: tuple[str, ...] = (
    "说真的", "btw", "对了", "忘了说", "你看", "讲真",
    "家人们", "姐妹们", "不夸张", "真的",
)

# 共用修辞模板 —— 一批里 ≥40% 的 cell 用同一个模板,就是流水线
BATCH_TEMPLATE_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    ("跟……没关系", re.compile(r"跟[^，。！？\n]{1,12}没(?:什么)?关系")),
    ("直到……才", re.compile(r"直到[^，。！？\n]{1,16}才")),
    ("后来才发现", re.compile(r"后来(?:我)?才(?:发现|知道|明白)")),
    ("谁懂", re.compile(r"谁懂")),
    ("一整个……住", re.compile(r"一整个[^，。！？\n]{0,8}住")),
    ("被……狠狠", re.compile(r"被[^，。！？\n]{0,10}狠狠")),
    ("救了我的", re.compile(r"救了我的")),
    ("闭眼入", re.compile(r"闭眼入")),
    ("不允许还有人不知道", re.compile(r"不允许(?:还)?有人不知道")),
    ("……的尽头是", re.compile(r"[^，。！？\n]{1,10}的尽头是")),
    ("有人跟我说", re.compile(r"有人(?:跟|对)我说")),
)

# 占比阈值
BATCH_INSERT_THRESHOLD = 0.60
BATCH_TEMPLATE_THRESHOLD = 0.40
BATCH_GRAM_MIN_CELLS = 3       # 同一个四字串至少出现在这么多 cell 才算热

# 计算四字串时跳过的高频字 —— 全是虚词/常用字,不带内容信息
_COMMON_GRAM_CHARS = set(
    "的了是在我你他她它这那有和就都也很不一个上要会说没到还只被把来去给让最多可以"
)

# 开头/结尾指纹取多少个汉字
_FINGERPRINT_LEN = 10


# ══════════════════════════════════════════════════════════════════════
# 工具
# ══════════════════════════════════════════════════════════════════════

def strip_trailing_hashtags(text: str) -> str:
    """剥掉尾部话题标签。

    v0.32.3 的教训:小红书笔记本来就以「#按摩椅 #中秋送礼」收尾,不剥标签
    就按正文规则判会 100% 误判。
    """
    if not text:
        return ""
    return re.sub(r"(?:[\s\n]*[#＃][^\s#＃]+)+\s*$", "", text.strip()).strip()


def first_sentence(text: str) -> str:
    """取第一句(到第一个 。！？或换行为止)。"""
    if not text:
        return ""
    return re.split(r"[。！？!?\n]", text.strip(), maxsplit=1)[0].strip()


def _han_only(text: str) -> str:
    return "".join(ch for ch in text if "一" <= ch <= "鿿")


def _opening_fingerprint(text: str) -> str:
    return _han_only(strip_trailing_hashtags(text))[:_FINGERPRINT_LEN]


def _ending_fingerprint(text: str) -> str:
    h = _han_only(strip_trailing_hashtags(text))
    return h[-_FINGERPRINT_LEN:] if h else ""


def _four_grams(text: str, allow: frozenset[str]) -> set[str]:
    """抽四字串,跳过全是常用字的和白名单里的(品牌词/蓝词)。"""
    h = _han_only(text)
    out: set[str] = set()
    for i in range(len(h) - 3):
        g = h[i : i + 4]
        if all(c in _COMMON_GRAM_CHARS for c in g):
            continue
        if any(a and a in g for a in allow):
            continue
        out.add(g)
    return out


# ══════════════════════════════════════════════════════════════════════
# 单篇扫描
# ══════════════════════════════════════════════════════════════════════

def scan_text(text: str, *, duplicate_opening: bool = False) -> dict[str, Any]:
    """扫一篇文本,返回 {"hard": [...], "soft": [...]}。

    `duplicate_opening` 由调用方在 matrix 层算好传进来 —— 单篇视角看不到撞车。
    """
    if not text or not text.strip():
        return {
            "hard": [{"rule": "demo_missing", "hit": "", "detail": "内容为空"}],
            "soft": [],
        }

    body = strip_trailing_hashtags(text)
    hard: list[dict] = []
    soft: list[dict] = []

    # ── HARD ──────────────────────────────────────────────────────
    for phrase in AI_CLICHE_BLACKLIST:
        if phrase in body:
            hard.append({
                "rule": "ai_cliche", "hit": phrase,
                "detail": f"命中 AI 空话黑名单:{phrase!r}",
            })

    for name, pat in PIVOT_PATTERNS:
        m = pat.search(body)
        if m:
            hard.append({
                "rule": "pivot", "hit": name,
                "detail": f"翻案句「{name}」:{m.group(0)[:40]!r}",
            })

    for word in HARD_JARGON:
        if word in body:
            hard.append({
                "rule": "jargon", "hit": word,
                "detail": f"商业黑话/汇报腔「{word}」—— 素人笔记里出现身份立刻穿帮",
            })

    m = PSEUDO_PRECISE_PATTERN.search(body)
    if m:
        hard.append({
            "rule": "pseudo_precise", "hit": m.group(0),
            "detail": f"伪精确行为量 {m.group(0)!r} —— 真人不会这样记自己的行为"
                      "(产品规格数不在此列)",
        })

    opening = first_sentence(body)
    for prefix in BANNED_OPENING_PREFIXES:
        if opening.startswith(prefix):
            hard.append({
                "rule": "banned_opening", "hit": prefix,
                "detail": f"第一句以禁用开场起手:{opening[:30]!r}",
            })
            break

    numbered = sum(len(p.findall(body)) for p in _LIST_BODY_PATTERNS)
    seq_hits = sum(1 for m2 in _SEQUENCE_MARKERS if m2 in body)
    if numbered >= 2 or seq_hits >= 2:
        hard.append({
            "rule": "list_body", "hit": f"编号 {numbered} / 顺序词 {seq_hits}",
            "detail": "正文写成了列表体",
        })

    if duplicate_opening:
        hard.append({
            "rule": "duplicate_opening", "hit": opening[:40],
            "detail": "第一句与 matrix 内另一个 cell 完全相同",
        })

    # ── SOFT ──────────────────────────────────────────────────────
    dashes = len(_EM_DASH.findall(body))
    if dashes >= 2:
        soft.append({"rule": "em_dash", "value": dashes,
                     "detail": f"破折号 {dashes} 处 —— 小红书里偏书面"})

    han_len = max(len(_han_only(body)), 1)
    you_n = len(_YOU_CHAR.findall(body))
    you_density = you_n / han_len * 100
    if you_n >= 6 and you_density > 2:
        soft.append({"rule": "you_density", "value": round(you_density, 2),
                     "detail": f"「你」{you_n} 次(每百字 {you_density:.1f})"
                               " —— 偏说教而非分享"})

    road_n = sum(body.count(r) for r in ROAD_SIGNS)
    if road_n >= 4:
        soft.append({"rule": "road_signs", "value": road_n,
                     "detail": f"洞察路标词 {road_n} 处 —— 段落靠信号词推进"
                               "而不是靠内容"})

    return {"hard": hard, "soft": soft}


# ══════════════════════════════════════════════════════════════════════
# 跨 cell 指纹 —— 本模块真正的新能力
# ══════════════════════════════════════════════════════════════════════

def scan_batch_fingerprints(
    cells: list[dict],
    *,
    allow: frozenset[str] | None = None,
) -> dict[str, Any]:
    """扫整批 cell 的**结构性**重合。

    和已有的跨 cell 查重是两回事,两层并行各管一半:

    - `_find_cross_cell_duplicates` / trigram Jaccard 抓的是**词面重合**
      —— 换汤不换药
    - 本函数抓的是**结构重合** —— 每篇开头都时空锚定、每篇都插一句"说真的"、
      每篇结尾都落在同一个句式上。词面重合度可以很低,读者一眼流水线。
      这是**换药不换汤**

    `allow`: 品牌词 / 蓝词白名单。蓝词必须原样重复(轮换同义词直接损伤搜索
    权重),所以它们出现在每一篇里是**正确行为**,不能被算成批量指纹。
    """
    allow = allow or frozenset()
    bodies = [
        (c.get("cell_id", "?"), strip_trailing_hashtags(c.get("demo_output") or ""))
        for c in cells or []
    ]
    bodies = [(cid, b) for cid, b in bodies if b]
    n = len(bodies)
    if n < 2:
        return {"n_cells": n, "_note": "cell 不足 2 个,不做跨篇指纹"}

    # 开头 / 结尾指纹
    open_map: dict[str, list[str]] = {}
    end_map: dict[str, list[str]] = {}
    for cid, b in bodies:
        of, ef = _opening_fingerprint(b), _ending_fingerprint(b)
        if len(of) >= 6:
            open_map.setdefault(of, []).append(cid)
        if len(ef) >= 6:
            end_map.setdefault(ef, []).append(cid)

    # 共用插入词
    shared_inserts: dict[str, dict] = {}
    for phrase in BATCH_INSERT_PHRASES:
        hits = [cid for cid, b in bodies if phrase in b]
        ratio = len(hits) / n
        if ratio >= BATCH_INSERT_THRESHOLD:
            shared_inserts[phrase] = {
                "cells": hits, "ratio": round(ratio, 3),
            }

    # 共用修辞模板
    shared_templates: dict[str, dict] = {}
    for name, pat in BATCH_TEMPLATE_PATTERNS:
        hits = [cid for cid, b in bodies if pat.search(b)]
        ratio = len(hits) / n
        if ratio >= BATCH_TEMPLATE_THRESHOLD:
            shared_templates[name] = {
                "cells": hits, "ratio": round(ratio, 3),
            }

    # 跨篇高频四字串
    gram_cells: dict[str, list[str]] = {}
    for cid, b in bodies:
        for g in _four_grams(b, allow):
            gram_cells.setdefault(g, []).append(cid)
    hot_grams = {
        g: cids for g, cids in gram_cells.items()
        if len(cids) >= max(BATCH_GRAM_MIN_CELLS, int(n * 0.5))
    }
    # 只留最热的十条,别把 stage_log 撑爆
    hot_grams = dict(
        sorted(hot_grams.items(), key=lambda kv: -len(kv[1]))[:10]
    )

    findings = {
        "n_cells": n,
        "opening_dup": {k: v for k, v in open_map.items() if len(v) > 1},
        "ending_dup": {k: v for k, v in end_map.items() if len(v) > 1},
        "shared_inserts": shared_inserts,
        "shared_templates": shared_templates,
        "hot_4grams": {g: len(c) for g, c in hot_grams.items()},
    }
    findings["fingerprint_hits"] = (
        len(findings["opening_dup"]) + len(findings["ending_dup"])
        + len(shared_inserts) + len(shared_templates) + len(hot_grams)
    )
    return findings


# ══════════════════════════════════════════════════════════════════════
# Matrix 级入口
# ══════════════════════════════════════════════════════════════════════

def run_prose_gate(
    cells: list[dict],
    *,
    allow: frozenset[str] | None = None,
) -> dict[str, Any]:
    """对整个 prompt_matrix 跑机审,返回可落库、可喂给 rewriter 的结构。

    纯 Python,零 LLM 成本,永不抛异常。
    """
    prompt_cells = cells or []
    if not prompt_cells:
        return {"status": "skipped", "reason": "prompt_matrix 为空"}

    # 先在 matrix 层算首句撞车
    opening_counts: dict[str, int] = {}
    for c in prompt_cells:
        op = first_sentence(strip_trailing_hashtags(c.get("demo_output") or ""))
        if len(op) >= 8:
            opening_counts[op] = opening_counts.get(op, 0) + 1

    per_cell: list[dict] = []
    failed_ids: list[str] = []
    hard_tally: dict[str, int] = {}
    for c in prompt_cells:
        cid = c.get("cell_id", "?")
        demo = c.get("demo_output") or ""
        op = first_sentence(strip_trailing_hashtags(demo))
        dup = len(op) >= 8 and opening_counts.get(op, 0) > 1

        res = scan_text(demo, duplicate_opening=dup)
        for h in res["hard"]:
            hard_tally[h["rule"]] = hard_tally.get(h["rule"], 0) + 1
        if res["hard"]:
            failed_ids.append(cid)
        per_cell.append({
            "cell_id": cid,
            "platform": c.get("platform", ""),
            "gate_verdict": "fail" if res["hard"] else "pass",
            "hard_hits": res["hard"],
            "soft_flags": res["soft"],
        })

    batch = scan_batch_fingerprints(prompt_cells, allow=allow)

    return {
        "status": "ok",
        "total_cells": len(prompt_cells),
        "failed_cells": failed_ids,
        "pass_rate": round(
            (len(prompt_cells) - len(failed_ids)) / len(prompt_cells), 4
        ),
        "hard_tally": hard_tally,
        "per_cell": per_cell,
        "batch_fingerprints": batch,
    }


def format_hard_hits_for_rewriter(per_cell_entry: dict) -> str:
    """把一个 cell 的硬命中拍成给 rewriter 的定点改写指令。

    刻意只给**命中什么 + 在哪**,不给"应该改成什么" —— 修法交给 rewriter 的
    修复哲学段(见 vibe_rewriter.md)。给出建议改法会诱导它去找另一种漂亮句式
    替换,那正是 A 指纹换成 B 指纹的来源。
    """
    hits = per_cell_entry.get("hard_hits") or []
    if not hits:
        return ""
    lines = ["【机审硬命中 · 必须逐条改掉】"]
    for h in hits:
        lines.append(f"- [{h.get('rule')}] {h.get('detail', '')}")
    lines.append(
        "改的时候先找这句话**原本想说的那件事**,用普通句子把事说出来。"
        "不要找另一种漂亮句式替换。"
    )
    return "\n".join(lines)
