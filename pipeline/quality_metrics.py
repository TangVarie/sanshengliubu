"""双层评分体系 — 给每条 run 的最终产出打一个可跨 run 对比的分数。

## 为什么需要这个模块

在此之前,整条流水线**没有任何输出侧的质量度量**。24 个 stage、11 道质量闸,
但没有一个地方回答得了"这一版产出比上一版好吗"。`config.py` 里几百行版本
注记全是崩溃修复和截断修复,没有一条是质量增量;R-022 飞轮 audit 追的是
"数据库样本有没有被用上"(输入侧遥测),不是"产出有没有变好"。

后果是改提示词全凭手感:改完 `works_builder.md` 跑一条 run,觉得"这版 demo
看着顺眼",就当改对了。样本量 1,还是被红蓝 + 网感重写打磨过 3 轮的那 1 篇。

这个模块补的就是这一层。它**不新增任何 LLM 调用**:
  - 红线层是纯 Python 确定性判定
  - 高分层从 `vibe_critic` 已经产出的 `cell_reviews` 里提取

## 两层为什么必须分开算

- **红线层**用 hill-climbing 思维,追 100% 通过率。这些是"命中即废稿"的
  硬伤(AI 空话、寒暄开场、列表体正文、跨 cell 撞车),零容忍尾部失败。
- **高分层**用上限思维,追**高分篇数的绝对值**(如 4/12 → 8/12),
  **不追通过率**。

第二条是这个模块最容易被改错的地方,所以写清楚:如果把高分层也当 pass rate
优化,mutation 会自然倾向"消除尾部低分篇" → 分布向均值收窄 → 把 60 分篇
拉到 75 分的同时也把 95 分篇压到 85 分 → 方差消失 → 爆款消失。而小红书
内容的价值分布是极度长尾的,100 篇里 5 篇爆款的价值远大于 100 篇都 85 分。
**保方差是有意的,不是没优化干净。**

## 红线层为什么只收"命中即死"型判定

本仓库有两次血的教训,都是"应存在"型检查(检查某个东西**在不在**)误判导致
批次重试 + 单 cell 重试的三轮空烧:

  - v0.32.3: `_validate_prompt_cell` 的结尾完整性检查在小红书上 100% 误判 ——
    小红书笔记本来就以话题标签收尾,而检查只认句末标点。
  - v0.32.4: `essential_keywords` 是全函数唯一没有别名表的硬失败检查,模型把
    禁用清单写成「不要写…」而不是「禁止…」时就误判。

共同点:**"应存在"型判定的假阴性率天然高**,因为自然语言的表达方式无穷,
词表永远不全。而"命中即死"型判定(黑名单)的假阳性率天然低,因为命中是
确凿事件。

所以红线层只放黑名单类判定。凡是"必须有 X"的检查(具体性四要素这种)一律
放高分层 —— 那里判错只影响"高分篇数"这个统计量,不会打死任何 cell,更不会
触发重试。
"""

from __future__ import annotations

import logging
import re

from pipeline.config import PLATFORM_VOICE_ALIASES, PLATFORM_VOICE_MARKERS
from pipeline.prose_gate import (
    first_sentence,
    scan_text,
    strip_trailing_hashtags as _strip_trailing_hashtags,
)

logger = logging.getLogger(__name__)

# first_sentence / strip_trailing_hashtags / scan_text 都来自 prose_gate,
# 本模块不再自己定义任何词表或文本判定。
#
# v0.33.5 起所有机械文字判定收归 prose_gate 一家:此前评分器自己带了一份
# AI 空话黑名单,机审闸再带一份,两份必然漂移 —— 而漂移的表现是最难查的那种:
# 分数还在涨,闸门已经按新规矩走了。architecture.md 第 5 节记的"词表同步义务"
# 本来就是在说这个风险,与其靠人记着同步,不如让它只有一份。
#
# 副作用是红线层跟着机审闸一起变强了(新增翻案句家族 / 商业黑话 / 伪精确
# 行为量),这是想要的 —— 评分口径和闸门口径本来就该是同一套。


# ── 高分层 · 具体性四要素 ───────────────────────────────────────────────
#
# `works_builder.md` 范式 A (c)「具体性硬指标」要求通篇至少各有 1 处:
# 具体人物身份 / 具体时间地点 / 具体物品动作数字 / 具体情绪反应。
#
# 放在高分层而不是红线层,原因见模块 docstring:这是"应存在"型判定,词表
# 不可能穷尽中文的表达方式,假阴性率高。判错只让这篇少拿 1 分,不打死它。

_CONCRETE_PERSON = re.compile(
    r"我老公|我老婆|我妈|我爸|我婆婆|婆婆|公公|我妹|我姐|我哥|我弟"
    r"|室友|闺蜜|同事|领导|前任|男朋友|女朋友|对象|房东|我儿子|我女儿"
    r"|我家那位|亲戚|表姐|表妹|舍友|同桌|\d{1,2}\s*岁"
)
_CONCRETE_TIME_PLACE = re.compile(
    r"\d+\s*(?:天|周|个?月|年|分钟|小时|点|号)"
    r"|昨天|前天|今早|昨晚|半夜|凌晨|周末|年夜饭|过年|春节"
    r"|酒店|公司|地铁|宿舍|厨房|卫生间|浴室|车里|办公室|医院|超市|楼下|老家"
)
_CONCRETE_THING_NUM = re.compile(
    r"\d+\s*(?:块|元|毛|斤|公斤|kg|g|克|ml|毫升|cm|厘米|米|度|次|遍|瓶|支|片|盒|袋|包|条|件|%|％)"
)
_CONCRETE_EMOTION = re.compile(
    r"破防|愣住|傻眼|当场|裂开|绷不住|心态崩|哭了|笑死|无语|服了"
    r"|吓一跳|头皮发麻|鸡皮疙瘩|气笑了|尴尬|社死|懵了|慌了|后怕|上头"
)

_PARADIGM_A_CHECKS: tuple[tuple[str, re.Pattern], ...] = (
    ("具体人物身份", _CONCRETE_PERSON),
    ("具体时间地点", _CONCRETE_TIME_PLACE),
    ("具体物品数字", _CONCRETE_THING_NUM),
    ("具体情绪反应", _CONCRETE_EMOTION),
)


# ── 高分层 · 范式 B 的四要素(和范式 A 平行,但查的是完全不同的东西)──────
#
# ⚠️ 这一段是评分器最容易被写错的地方,单独说明:
#
# 「具体性四要素」在 `works_builder.md` 里是写在**「范式 A 专用」**标题下的
# (人物 / 时间地点 / 物品数字 / 情绪反应)。范式 B(元评论应答体)建立信任
# 靠的是完全不同的东西 —— 术语锚、结构、距离感,不靠具体人物和情绪爆点。
#
# 拿 A 的尺子量 B 的后果是**系统性低估所有 B 范式格子**:实测 foundation.md
# 里那条被当作范例的真实洗洁精爆款,按 A 的四要素判会缺 3 项。这会让任何
# 改善 B 范式内容的改动在分数上看起来毫无长进,把迭代引到错误的方向。
#
# 所以按 paradigm 分流,两套各 4 项、各值 1 分,保持两条路径的分数可比。
# 四项取自 `foundation.md` 范式 B 的 7 个技巧里**可高精度机械判定**的那些
# (术语锚那条靠正则判不可靠,故意不收 —— 宁可少判一项,不要制造假信号)。

# ① 元话术开场:把内容伪装成"应粉丝要求的解答"。只扫第一句。
_META_OPENING = re.compile(
    r"评论|后台|私信|留言|都在问|问爆|居然火了|上一条|上次发|被骂|好多人问|这么多人"
)
# ② 专业身份延后揭示:身份词出现在正文里,但**不在第一句**(开场就报家门 = AI 腔)
_PRO_IDENTITY = re.compile(
    r"我是学[一-龥]{1,4}的|我是[一-龥]{2,6}(?:师|医生|博士|研究员)"
    r"|干这行\s*\d*\s*年|做了\s*\d+\s*年[一-龥]{0,4}|学医的|业内|同行"
)
# ③ 反广告距离感 + 平台暗号:非天猫旗舰店渠道 / 不在乎态度 / 时长背书
_ANTI_AD_DISTANCE = re.compile(
    r"不是广|没恰饭|没收钱|买不买无所谓|你们随意|不推荐也行|自费|自己买的"
    r"|1688|闲鱼|拼多多|淘宝小店|某宝小店"
    r"|用了快\s*\d|用了大半年|第[二三四]\s*[瓶支盒]"
)
# ④ 科学自我反驳:主动承认对手论点再顶回去 —— AI 写的科普永远是单向断言
_SELF_REBUTTAL = re.compile(
    r"虽然[^。！？\n]{2,40}[，,][^。！？\n]{0,10}但"
    r"|我知道有人会说|可能[有很]多人[觉认]为|有人会[说觉]"
    r"|不谈[一-龥]{1,6}谈[一-龥]{1,6}都是耍流氓"
)

_PARADIGM_B_CHECKS: tuple[tuple[str, re.Pattern], ...] = (
    ("元话术开场", _META_OPENING),
    ("身份延后揭示", _PRO_IDENTITY),
    ("反广告距离感", _ANTI_AD_DISTANCE),
    ("科学自我反驳", _SELF_REBUTTAL),
)

# 高分层判定「高分篇」的门槛:6 项里拿到几项。
HIGH_SCORE_THRESHOLD = 5
HIGH_SCORE_TOTAL = 6


# ── 红线层 ───────────────────────────────────────────────────────────────

def check_redlines(
    demo_output: str,
    *,
    duplicate_opening: bool = False,
) -> list[dict]:
    """跑红线层判定,返回**命中的违规项**列表(空 = 全过)。

    v0.33.5 起本函数是 `prose_gate.scan_text` 的薄封装,只取 hard 档。
    自己不再维护任何词表 —— 评分器和机审闸各带一份黑名单必然漂移,而漂移的
    表现是最难查的那种:分数还在涨,闸门已经按新规矩走了。

    副作用是红线层跟着机审闸一起变强了:除了原有的 AI 空话 / 寒暄开场 /
    列表体 / 首句撞车,现在还包括翻案句家族、商业黑话、伪精确行为量。
    这是想要的 —— 两边本来就该是同一套判定。

    Args:
        demo_output: 该 cell 最终交付的示例文稿。
        duplicate_opening: 该 cell 的首句是否和 matrix 里另一个 cell 相同。
            由调用方在 matrix 层算好传进来 —— 单 cell 视角看不到撞车。

    Returns:
        [{"rule": ..., "hit": ..., "detail": ...}, ...]
    """
    return scan_text(demo_output, duplicate_opening=duplicate_opening)["hard"]


# ── 高分层 ───────────────────────────────────────────────────────────────

def check_craft(demo_output: str, paradigm: str | None) -> list[str]:
    """按范式跑「工艺四要素」判定,返回**缺失项**列表(空 = 四项齐)。

    范式 A 查具体性(人物/时间地点/物品数字/情绪反应),范式 B 查结构性
    (元话术开场/身份延后/反广告距离感/科学自我反驳)。理由见 _PARADIGM_B_CHECKS
    上方的长注释。

    paradigm 缺失时默认走范式 A —— 和 `works_builder.md:75`
    「如果 paradigm 字段缺失 → 默认走范式 A」保持同一个兜底。
    """
    body = _strip_trailing_hashtags(demo_output or "")
    if not body:
        return [name for name, _ in _PARADIGM_A_CHECKS]

    is_b = "b_meta" in (paradigm or "").strip().lower()
    if not is_b:
        return [n for n, pat in _PARADIGM_A_CHECKS if not pat.search(body)]

    missing: list[str] = []
    opening = first_sentence(body)
    for name, pat in _PARADIGM_B_CHECKS:
        if name == "元话术开场":
            # 只认第一句 —— 正文中段提到"评论区"不构成应答式开场
            hit = bool(pat.search(opening))
        elif name == "身份延后揭示":
            # 身份词要在正文里出现,但**不能**在第一句 —— 开场报家门是反面
            hit = bool(pat.search(body)) and not pat.search(opening)
        else:
            hit = bool(pat.search(body))
        if not hit:
            missing.append(name)
    return missing


def check_high_score(
    demo_output: str,
    review: dict | None,
    paradigm: str | None = None,
) -> dict:
    """跑高分层 6 项判定。

    5 项来自 `vibe_critic` 已有的输出(零额外成本),1 项是 Python 判定的
    具体性四要素。

    Args:
        demo_output: 该 cell 的示例文稿(用于具体性判定)。
        review: 该 cell 在 `vibe_critic.cell_reviews` 里的那一条。
            None / 缺字段时对应项记 `None`(不是 False)—— 见下面
            "为什么区分 None 和 False"。

    Returns:
        {"items": {...}, "earned": int, "scored": int, "is_high_score": bool,
         "concreteness_missing": [...]}

    为什么区分 None 和 False:critic 挂掉或 resume 路径拿不到 review 时,
    该项是"没测",不是"没通过"。都算 False 会让一次 critic 故障看起来像
    质量断崖,污染跨 run 的曲线。没测的项不进分母(`scored`),
    `is_high_score` 只在测满 6 项时才可能为 True。
    """
    items: dict[str, bool | None] = {}
    review = review or {}

    gate = review.get("multiplier_gate") or {}
    for key in ("reward_signal", "interest_align", "gap_tension",
                "identity_consistency"):
        raw = (gate.get(key) or "").strip().lower()
        if raw not in ("pass", "weak", "fail"):
            items[key] = None          # 没测
        else:
            items[key] = (raw == "pass")   # weak 不算拿分 —— 高分层追的是上限

    tmpl = review.get("template_test") or {}
    tmpl_verdict = (tmpl.get("verdict") or "").strip().lower()
    if tmpl_verdict not in ("pass", "borderline", "fail"):
        items["template_test"] = None
    else:
        items["template_test"] = (tmpl_verdict == "pass")

    # 工艺四要素 —— 唯一一项 Python 判定,按范式分流,四项全中才拿分。
    # paradigm 优先取 critic 的判定(它第 -1 步专门判范式),回落到调用方传入。
    body = _strip_trailing_hashtags(demo_output or "")
    _paradigm = (review.get("paradigm") or paradigm or "")
    missing = check_craft(demo_output or "", _paradigm)
    items["craft"] = (not missing) if body else None

    scored = sum(1 for v in items.values() if v is not None)
    earned = sum(1 for v in items.values() if v is True)

    return {
        "items": items,
        "earned": earned,
        "scored": scored,
        # 只有 6 项全测到才可能算高分篇 —— 测不全就不给这个荣誉,
        # 否则"critic 挂了两项"会变成一条免费的高分捷径。
        "is_high_score": scored == HIGH_SCORE_TOTAL and earned >= HIGH_SCORE_THRESHOLD,
        "craft_missing": missing,
        "paradigm": _paradigm or "A_emotional_hook(默认)",
    }


# ── 结构审的确定性部分 ───────────────────────────────────────────────────
#
# 结构审(kimi_structure_reviewer)原本查 6 类结构件,其中 4 类
# (五池 / 人设 / 合规存在性 / 关键词存在性 / 禁用清单存在性)`_validate_prompt_cell`
# 已经用别名表确定性地查过了 —— 花一次 LLM 调用重查一遍是纯重复。
#
# 剩下真正需要模型判断的只有「具体 vs 空话」("注意合规即可" vs "不得声称根治"),
# 以及本函数处理的平台调性张冠李戴 —— 后者其实也是确定性的,所以下沉到这里。

def check_platform_voice(system_prompt: str, platform: str) -> dict | None:
    """检测 system_prompt 的平台调性段是不是写成了别的平台的。

    返回 None = 没问题;返回 dict = 疑似写串了。

    判定规则(**有意压低召回换精度**):只有「自己平台的调性词一个都没命中 +
    某个别家平台的词命中 ≥2 个」才报。

    为什么这么保守:单个外来词太容易是反例引用 —— 小红书的 prompt 里写
    「不要写成抖音那种前3秒钩子」是完全正确的用法,按"命中即报"会误杀。
    而"自己家的一个没有、别人家的好几个"这种组合,基本只可能是整段写错了平台。

    本仓库有两次因为误判导致三轮空烧的事故(v0.32.3 / v0.32.4),所以这里
    宁可漏报也不误报;而且结果只进 _structure_hint(交给重写器顺手补),
    不触发 builder 重试。
    """
    if not system_prompt or not platform:
        return None

    plat_lower = platform.strip().lower()
    own_key = None
    for alias, canonical in PLATFORM_VOICE_ALIASES.items():
        if alias in plat_lower:
            own_key = canonical
            break
    if own_key is None:
        return None      # 不认识的平台不判,别瞎报

    own_hits = [
        m for m in PLATFORM_VOICE_MARKERS.get(own_key, ())
        if m in system_prompt
    ]
    if own_hits:
        return None      # 自己家的调性词在,就算同时提到别家也大概率是反例引用

    foreign: dict[str, list[str]] = {}
    for key, markers in PLATFORM_VOICE_MARKERS.items():
        if key == own_key:
            continue
        hits = [m for m in markers if m in system_prompt]
        if len(hits) >= 2:
            foreign[key] = hits
    if not foreign:
        return None

    worst = max(foreign.items(), key=lambda kv: len(kv[1]))
    return {
        "platform": own_key,
        "written_as": worst[0],
        "foreign_markers": worst[1],
        "detail": (
            f"平台调性段疑似写成了「{worst[0]}」的:命中 "
            f"{'/'.join(worst[1][:4])} 等 {len(worst[1])} 个{worst[0]}专属调性词,"
            f"而本 cell 是「{own_key}」、一个{own_key}专属调性词都没有"
        ),
        "revision_hint": (
            f"把 system_prompt 里的平台调性段改成「{own_key}」的口吻"
            f"(参考 works_builder.md 规则 6),删掉{worst[0]}专属的表述"
        ),
    }


def deterministic_structure_audit(prompt_cells: list[dict]) -> dict[str, dict]:
    """对整批 cell 跑确定性结构审,返回 {cell_id: hint}。

    hint 的形状和 kimi_structure_reviewer 写进 `_structure_hint` 的一致
    (`missing_items` + `revision_hint`),所以两边的结果可以直接合并,
    下游 vibe_loop 的强制补漏逻辑不需要改。
    """
    out: dict[str, dict] = {}
    for cell in prompt_cells or []:
        cid = cell.get("cell_id")
        if not cid:
            continue
        pv = check_platform_voice(
            cell.get("system_prompt") or "", cell.get("platform") or ""
        )
        if pv:
            out[cid] = {
                "missing_items": [f"平台调性写成了{pv['written_as']}的"],
                "revision_hint": pv["revision_hint"],
                "_detail": pv["detail"],
                "_source": "deterministic",
            }
    return out


# ── Matrix 级汇总 ────────────────────────────────────────────────────────

def score_matrix(
    prompt_matrix: list[dict],
    cell_reviews_by_id: dict[str, dict] | None = None,
) -> dict:
    """给整个 prompt_matrix 打分,产出可落库、可跨 run 对比的 scorecard。

    Args:
        prompt_matrix: `final_system["prompt_matrix"]`,已经过全部精炼阶段。
        cell_reviews_by_id: `{cell_id: cell_review}`,由 vibe_loop 跨轮次
            累积(每个 cell 保留最后一次评审)。缺失时高分层大面积记 None,
            scorecard 的 `coverage` 字段会如实反映。

    Returns:
        scorecard dict — 落 stage_logs 的 output_data。
    """
    cell_reviews_by_id = cell_reviews_by_id or {}
    cells = prompt_matrix or []

    # 先在 matrix 层算首句撞车,再逐 cell 判红线。
    opening_counts: dict[str, int] = {}
    for c in cells:
        op = first_sentence(_strip_trailing_hashtags(c.get("demo_output") or ""))
        if len(op) >= 8:      # 太短的开头不算有意义的撞车,和 _find_cross_cell_duplicates 对齐
            opening_counts[op] = opening_counts.get(op, 0) + 1

    per_cell: list[dict] = []
    redline_clean = 0
    high_score_cells = 0
    scored_cells = 0
    violation_tally: dict[str, int] = {}

    for c in cells:
        cid = c.get("cell_id", "?")
        demo = c.get("demo_output") or ""
        op = first_sentence(_strip_trailing_hashtags(demo))
        dup = len(op) >= 8 and opening_counts.get(op, 0) > 1

        violations = check_redlines(demo, duplicate_opening=dup)
        for v in violations:
            violation_tally[v["rule"]] = violation_tally.get(v["rule"], 0) + 1
        if not violations:
            redline_clean += 1

        hs = check_high_score(
            demo,
            cell_reviews_by_id.get(cid),
            # cell 自己带的 paradigm 作为兜底 —— critic 没跑到 / resume 路径
            # 拿不到 review 时,至少还能按格子规划标注的范式判。
            paradigm=c.get("paradigm"),
        )
        if hs["scored"] == HIGH_SCORE_TOTAL:
            scored_cells += 1

        # 红线是**准入**,高分是**排名** —— 违规的 cell 一律不进高分篇计数,
        # 哪怕它高分层拿满 6 项。
        #
        # 两层"独立评分"指的是**分开算、分开定目标**(红线追 100%、高分追
        # 绝对数),不是"红线废了也能当爆款候选"。命中 AI 空话黑名单或写成
        # 列表体的稿子是废稿,把它算进高分篇会让那个驱动后续全部迭代决策的
        # 数字注水 —— 而这个数字一旦不可信,整个量化迭代就退回凭手感。
        is_high = hs["is_high_score"] and not violations
        if is_high:
            high_score_cells += 1

        per_cell.append({
            "cell_id": cid,
            "platform": c.get("platform", ""),
            "direction_id": c.get("direction_id", ""),
            "redline_violations": violations,
            "redline_pass": not violations,
            "high_score_items": hs["items"],
            "high_score_earned": hs["earned"],
            "high_score_scored": hs["scored"],
            "is_high_score": is_high,
            # 拿满高分层但被红线打掉的 cell 单独标出来 —— 这类"味道对但踩了
            # 硬伤"的格子是修复性价比最高的,一条红线改掉就能进高分篇。
            "high_score_blocked_by_redline": hs["is_high_score"] and bool(violations),
            "craft_missing": hs["craft_missing"],
            "paradigm": hs["paradigm"],
        })

    total = len(cells)
    return {
        "total_cells": total,
        # ── 红线层:hill-climbing,追 100% ──────────────────────────
        "redline_pass_cells": redline_clean,
        "redline_pass_rate": round(redline_clean / total, 4) if total else 0.0,
        "redline_violation_tally": violation_tally,
        # ── 高分层:上限思维,看**绝对数**不看比率 ───────────────────
        # 有意不提供 high_score_rate 字段。见模块 docstring:把高分层当
        # pass rate 优化会压掉方差,而方差正是爆款的来源。要对比就对比
        # "8/12 vs 4/12" 这个绝对数的翻倍。
        "high_score_cells": high_score_cells,
        "high_score_threshold": f"{HIGH_SCORE_THRESHOLD}/{HIGH_SCORE_TOTAL}",
        # ── 覆盖度:高分层测全了几个 cell ────────────────────────────
        # critic 故障 / resume 路径会让这个数掉下来,提醒读数的人
        # "这一条 run 的高分层数字不可比",而不是误读成质量下降。
        "high_score_coverage_cells": scored_cells,
        "per_cell": per_cell,
    }


# ── 回归哨兵 ─────────────────────────────────────────────────────────────
#
# 单条 run 的分数没有意义。「红线 10/12、高分篇 5/12」是好是坏,只有跟这个项目
# 自己的历史比才知道。
#
# 这一层要解决的不是"记得打开某个开关" —— 开关本来就是默认开的,不需要人记。
# 它解决的是**靠记性做不到的那件事**:某次改动之后质量悄悄掉了。人不会盯着
# 每条 run 的两个数字去跟上周对比,但代码会。

# 拿最近几条 run 做基线。太少(<3)噪声压不住,太多则对真实的阶段性改进反应迟钝。
BASELINE_WINDOW = 8
# 至少要有这么多条历史才开始判 —— 前几条 run 是在建立基线,不该报警。
BASELINE_MIN_RUNS = 3
# 高分篇率的噪声带。LLM 生成本身带 temperature 抖动,同一版 prompt 连跑两批
# 高分篇数就会差一两篇。低于这个幅度的变化一律读作"持平",不报警。
# 0.15 ≈ 12 格子里差 1.8 篇,和小样本下的实测抖动量级一致。
HIGH_SCORE_NOISE_BAND = 0.15
# 红线是确定性判定(正则命中),不受 temperature 影响 —— 掉了就是真掉了,
# 所以用一个很小的容差,基本等于"只要跌就报"。
REDLINE_NOISE_BAND = 0.02


def _median(xs: list[float]) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    m = len(s) // 2
    return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2


def detect_regression(
    scorecard: dict, history: list[dict]
) -> dict | None:
    """拿本轮分数跟历史中位数比,判断是否回归。

    Args:
        scorecard: 本轮的 `score_matrix` 输出。
        history: 同项目此前若干条 run 的 scorecard(时间倒序),由
            `db.get_quality_score_history` 提供。

    Returns:
        None = 没有回归(或历史不足以判断);dict = 回归详情。

    两条指标分开判,口径不同:
      - **红线通过率**是确定性判定,不受 temperature 影响 —— 跌了就是真跌
      - **高分篇率**受生成抖动影响 —— 要跌出噪声带才算

    高分篇这里用**率**不用绝对数,因为跨 run 的格子数会变(方向数 × 平台数
    不固定,策略升级还会加格子)。绝对数在同一批实验内部可比,跨 run 比必须归一。
    这和 UI 上"看绝对数翻倍"不矛盾:那是同口径的纵向对比,这里是跨口径的。
    """
    # ⚠️ 两条指标要**各自建基线**,不能共用一份。
    #
    # 高分层必须只跟"critic 覆盖完整"的历史比 —— 挂过 critic 的 run 高分篇数
    # 是被低估的,混进基线会把标准拉低、真正的退步反而报不出来。
    #
    # 但红线通过率是**确定性判定**(正则命中),critic 挂没挂跟它毫无关系。
    # 拿高分层的覆盖度去过滤红线基线,后果是:一个项目只要连着几条 run 的 critic
    # 不完整,就连红线回归也一起测不了了 —— 而那恰恰是最该报警的时候。
    redline_hist = [h for h in history if h.get("total_cells")][:BASELINE_WINDOW]
    high_hist = [
        h for h in history
        if h.get("total_cells")
        and h.get("high_score_coverage_cells") == h.get("total_cells")
    ][:BASELINE_WINDOW]

    cur_total = scorecard.get("total_cells") or 0
    if not cur_total:
        return None
    if len(redline_hist) < BASELINE_MIN_RUNS and len(high_hist) < BASELINE_MIN_RUNS:
        return None
    # 本轮自己覆盖不全时不判高分层 —— 那个数字本来就不可比。
    cur_full_coverage = (
        scorecard.get("high_score_coverage_cells") == cur_total
    )

    base_redline = _median([
        float(h.get("redline_pass_rate") or 0.0) for h in redline_hist
    ])
    base_high = _median([
        (h.get("high_score_cells") or 0) / (h.get("total_cells") or 1)
        for h in high_hist
    ])
    cur_redline = float(scorecard.get("redline_pass_rate") or 0.0)
    cur_high = (scorecard.get("high_score_cells") or 0) / cur_total

    issues: list[str] = []
    if (len(redline_hist) >= BASELINE_MIN_RUNS
            and cur_redline < base_redline - REDLINE_NOISE_BAND):
        issues.append(
            f"红线通过率 {cur_redline:.0%} < 近 {len(redline_hist)} 条 run 的中位数 "
            f"{base_redline:.0%}。红线是确定性判定、不受生成抖动影响，"
            f"跌了就是真跌 —— 多半是某条规则被改动引入，或提示词里对应的"
            f"约束被削弱了"
        )
    if (cur_full_coverage
            and len(high_hist) >= BASELINE_MIN_RUNS
            and cur_high < base_high - HIGH_SCORE_NOISE_BAND):
        issues.append(
            f"高分篇率 {cur_high:.0%} < 中位数 {base_high:.0%}，"
            f"跌幅超出噪声带 {HIGH_SCORE_NOISE_BAND:.0%}。这一项受生成抖动影响，"
            f"单次可能是偶然 —— 但连续两条 run 都报就值得回去看最近改了什么"
        )

    if not issues:
        return None

    return {
        "regressed": True,
        # ⚠️ 不要再合并出一个"总基线条数"。两条指标的基线是**不同的两批 run**
        # (红线用全部有格子的 run,高分层只用评分覆盖完整的),取 max 出来的
        # 那个数字哪条指标都不对应 —— UI 拿它写"N 条覆盖完整的 run"就是在
        # 撒谎:红线中位数根本不是从那批算的。要展示就分开展示。
        "baseline_runs_redline": len(redline_hist),
        "baseline_runs_high_score": len(high_hist),
        "redline": {"current": round(cur_redline, 4),
                    "baseline_median": round(base_redline, 4)},
        "high_score_rate": {"current": round(cur_high, 4),
                            "baseline_median": round(base_high, 4),
                            "_scored": cur_full_coverage},
        "issues": issues,
    }


def check_and_flag_regression(
    db, project_id: str, run_id: str, scorecard: dict, final_system: dict
) -> dict | None:
    """跑回归哨兵,有回归就写进 strategic_warnings 让 UI 红字显示。

    尽力而为:查询失败 / 历史不足 / 判定异常一律静默跳过,绝不影响出货。
    """
    try:
        history = db.get_quality_score_history(
            project_id, limit=BASELINE_WINDOW, exclude_run_id=run_id
        )
        result = detect_regression(scorecard, history)
        if not result:
            logger.info(
                "[regression] 无回归(可比历史 %d 条)", len(history)
            )
            return None

        final_system.setdefault("strategic_warnings", []).append({
            "type": "quality_regression",
            "stage": "quality_score",
            "message": (
                "⚠️ **本轮质量分低于该项目的历史水平** —— "
                + "；".join(result["issues"])
                + "。建议对照 architecture.md 第 5 节的 SQL 看一下分数曲线，"
                  "并回顾最近改了哪些提示词或 flag。"
            ),
            "detail": result,
            "source": "regression_sentinel",
        })
        logger.warning(
            "[regression] 检出回归:%s", " | ".join(result["issues"])
        )
        return result
    except Exception:
        logger.exception("[regression] 哨兵执行失败(non-fatal)")
        return None


def persist_quality_score(db, run_id: str, scorecard: dict) -> None:
    """把 scorecard 写成一行 stage_log(stage_name='quality_score')。

    落库策略照抄 `_persist_audit_findings`(R-022):**尽力而为,写失败绝不
    影响流水线**。评分是可观测性,不是硬不变量 —— 为了记一个分数把一条
    已经跑完的 run 判死是明确的错误行为。

    status 语义:
      - 'completed'      — 红线层 100% 通过
      - 'completed_warn' — 有红线违规(交付前应人工复核)
    """
    has_redline_fail = scorecard.get("redline_pass_rate", 0.0) < 1.0
    try:
        log = db.create_stage_log(
            run_id,
            "quality_score",
            input_data={"total_cells": scorecard.get("total_cells", 0)},
        )
        db.update_stage_log(
            log["id"],
            status="completed_warn" if has_redline_fail else "completed",
            output_data=scorecard,
        )
        logger.info(
            "[quality_score] 红线 %d/%d(%.0f%%) · 高分篇 %d/%d(门槛 %s,覆盖 %d)",
            scorecard.get("redline_pass_cells", 0),
            scorecard.get("total_cells", 0),
            scorecard.get("redline_pass_rate", 0.0) * 100,
            scorecard.get("high_score_cells", 0),
            scorecard.get("total_cells", 0),
            scorecard.get("high_score_threshold", "?"),
            scorecard.get("high_score_coverage_cells", 0),
        )
    except Exception:
        logger.exception(
            "[quality_score] 落库失败(run_id=%s);分数仍在 stderr,不阻塞流水线",
            run_id,
        )
