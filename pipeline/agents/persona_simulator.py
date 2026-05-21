"""User persona simulator — simulated reader reactions.

Creates 3 target-audience personas and has each react to every cell's
demo_output with a 0.5-second gut reaction (click/skip/save). Runs
AFTER red-blue refinement, results injected as advisory input to
vibe_critic so the critic sees "real user reactions" alongside its
own expert judgment.
"""

from pipeline.agents import BaseAgent


class PersonaSimulator(BaseAgent):
    stage_name = "persona_simulator"
    prompt_file = "persona_simulator.md"


class PersonaSimulatorAlt(BaseAgent):
    """v0.30.8: 画像模拟的异厂家副本(DeepSeek 系)。

    复用 persona_simulator.md 的 prompt(同样的输入输出契约),但在 MODELS 里
    映射到 deepseek-v4-pro。orchestrator 同时启动 PersonaSimulator(Claude
    Sonnet 3.7)和 PersonaSimulatorAlt(DeepSeek)并行跑,合并 personas[]
    数组——Claude 扮演 P_core/P_edge 偏目标用户细腻反应,DeepSeek 扮演
    P_anti/破圈视角偏草根用户混杂分布。两个 distribution 互补。
    """
    stage_name = "persona_simulator_alt"
    prompt_file = "persona_simulator.md"  # 共用同一份 prompt
