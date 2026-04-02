"""尚书省 · Dispatcher — Splits approved plan into ministry task packages."""

from pipeline.agents import BaseAgent


class Dispatcher(BaseAgent):
    stage_name = "dispatcher"
    prompt_file = "dispatcher.md"
