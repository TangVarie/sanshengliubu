"""AI 空话黑名单:Python 只有一份,提示词里的清单是它的子集。

方向选「提示词 ⊆ Python」而不是反过来:提示词按设计只放少量带原因的禁令
(prose_gate 模块头、architecture.md §9),机审兜全量;要求 Python 的每一条都
写进三份提示词,等于逼着改提示词、把注意力预算花在清单上。反过来这条是真正
要防的漂移 —— 提示词让模型 / critic 避开某个词,机审和评分却不认,于是
「分数还在涨,闸门已经按新规矩走了」(architecture.md §5)。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from pipeline import prose_gate
from pipeline.orchestrator import _validate_prompt_cell

PROMPTS = Path(__file__).resolve().parent.parent / "pipeline" / "prompts"

# 两份旧清单(v0.36.1)。钉住「并集」这件事:谁从单一来源里删条目,测试会红。
_OLD_ORCHESTRATOR_11 = (
    "效果显著", "性价比高", "值得推荐", "适合所有人", "温和不刺激",
    "希望对你有帮助", "综上所述", "在如今", "让我们一起", "姐妹们冲", "快快收藏",
)
_OLD_PROSE_GATE_15 = (
    "效果显著", "性价比高", "值得推荐", "适合所有人", "温和不刺激",
    "希望对你有帮助", "综上所述", "总而言之", "让我们一起", "姐妹们冲", "快快收藏",
    "分享几个小技巧", "记住这3点", "记住这三点", "以下几个要点",
)


def test_single_source_is_the_union_of_both_old_lists():
    assert set(prose_gate.AI_CLICHE_BLACKLIST) == set(_OLD_ORCHESTRATOR_11) | set(_OLD_PROSE_GATE_15)
    assert len(prose_gate.AI_CLICHE_BLACKLIST) == len(set(prose_gate.AI_CLICHE_BLACKLIST)) == 16


def test_orchestrator_has_no_private_copy():
    src = (Path(__file__).resolve().parent.parent / "pipeline" / "orchestrator.py").read_text(encoding="utf-8")
    assert "ai_cliches = [" not in src
    assert "works_builder.md:60" not in src          # 旧注释指错了行(docs/02 §4 #9)


def _cell(demo: str) -> dict:
    return {"cell_id": "D1", "direction_id": "D1", "platform": "小红书",
            "system_prompt": "", "user_prompt_template": "", "demo_output": demo}


@pytest.mark.parametrize("phrase", ["总而言之", "以下几个要点", "在如今"])
def test_builder_validation_reads_the_shared_list(phrase):
    _ok, issues = _validate_prompt_cell(_cell(f"我妈翻我包。{phrase}这事就这样。"))
    assert any("AI 空话黑名单" in i and phrase in i for i in issues)


def test_prose_gate_now_checks_zai_ruJin_anywhere():
    hits = prose_gate.scan_text("周三晚上我妈翻我包。在如今这个节奏下谁不累。")["hard"]
    assert any(h["rule"] == "ai_cliche" and h["hit"] == "在如今" for h in hits)


# ── 提示词清单 ⊆ Python ─────────────────────────────────────────────────

_QUOTED = re.compile(r'"([^"\n]{2,40})"')


def _section(path: Path, start: str, end: str) -> str:
    text = path.read_text(encoding="utf-8")
    i = text.index(start)
    j = text.index(end, i + len(start))
    return text[i:j]


def _bullets(text: str) -> str:
    """只要清单的条目行(「- 」开头),说明文字里引号括起来的判决词不算。"""
    return "\n".join(ln for ln in text.splitlines() if ln.lstrip().startswith("- "))


def _prompt_lists() -> dict[str, list[str]]:
    return {
        "vibe_critic.md 第 0.5 步": _QUOTED.findall(_bullets(_section(
            PROMPTS / "vibe_critic.md", "### 第 0.5 步", "这些词是 AI 腔的硬指纹"))),
        "vibe_rewriter.md 通篇禁止": _QUOTED.findall(_bullets(_section(
            PROMPTS / "vibe_rewriter.md", '通篇禁止以下"AI 空话"', "完美对仗"))),
        # works_builder 这份写在一句话里,没有条目行
        "works_builder.md 范式 A (c)": _QUOTED.findall(_section(
            PROMPTS / "ministries" / "works_builder.md", "禁止 AI 空话：", "\n")),
    }


def _key(item: str) -> str:
    """模板写法按字面前缀对账:「在如今 X 的时代」→「在如今」,「如果你也...那么…」→「如果你也」。"""
    return re.split(r"\s*(?:X|\.\.\.|…)", item, maxsplit=1)[0].strip()


def test_prompt_lists_are_found():
    lists = _prompt_lists()
    # 锚点挪了就会抓空 —— 抓空时测试必须红,不能静默通过
    for name, items in lists.items():
        assert len(items) >= 5, f"{name} 只抓到 {items} —— 提示词里的锚点句挪了?"


def test_every_prompt_cliche_is_known_to_the_machine_gate():
    known = (set(prose_gate.AI_CLICHE_BLACKLIST)
             | set(prose_gate.BANNED_OPENING_PREFIXES)
             | set(prose_gate.PROMPT_ONLY_AI_CLICHES))
    missing = {name: sorted({_key(i) for i in items} - known)
               for name, items in _prompt_lists().items()}
    missing = {k: v for k, v in missing.items() if v}
    assert not missing, (
        f"提示词里的 AI 空话清单有机审不认的条目:{missing}。"
        "要么加进 prose_gate.AI_CLICHE_BLACKLIST(机审按全文硬查),要么写进 "
        "PROMPT_ONLY_AI_CLICHES 并注明为什么机审不查。"
    )
