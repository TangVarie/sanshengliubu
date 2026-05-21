"""Red-Blue adversarial content refiner.

v0.30.9 起拆分成真正的异模型对抗:
- RedBlueRed(攻方,Opus 4.6)— 找出 demo 里 0-5 个 AI 腔/空洞/暴广告的点
- RedBlueBlue(守方,Sonnet 3.7)— 接收 attacks,写具体替换 + 输出修复 demo

之前 RedBlueRefiner 一个 Sonnet 3.7 自己跟自己演两个角色,**同模型同盲区**
不是真对抗。现在两个模型各自独立 stage_log,orchestrator 串行编排:
红队先跑 → attacks 非空时蓝队接力跑(空就跳过省 token)。

旧的 RedBlueRefiner 类保留给老 stage_log 兼容,实际不再被 orchestrator 调用。
"""

from pipeline.agents import BaseAgent


class RedBlueRefiner(BaseAgent):
    """v0.30.8 之前的整合版红蓝精炼。v0.30.9 起被拆分,本类保留只为
    resume 老 run 时不报 import 错。新代码请用 RedBlueRed + RedBlueBlue。"""
    stage_name = "red_blue_refiner"
    prompt_file = "red_blue_refiner.md"


class RedBlueRed(BaseAgent):
    """红队 · 攻方(独立)。Opus 4.6,深推理找 AI 腔指纹和结构问题。
    输出: attacks 数组(0-5 条)+ _red_summary。蓝队接力。"""
    stage_name = "red_blue_red"
    prompt_file = "red_blue_red.md"


class RedBlueBlue(BaseAgent):
    """蓝队 · 守方(独立)。Sonnet 3.7,写作语感强,做最小修复。
    输入: cell + attacks 列表(从红队拿)。输出: fixes + refined_demo_output。
    attacks 为空时静默返回空 fixes,不无中生有。"""
    stage_name = "red_blue_blue"
    prompt_file = "red_blue_blue.md"
