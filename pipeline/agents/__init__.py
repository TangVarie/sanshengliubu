"""Base agent class for all pipeline agents."""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any

import anthropic
import streamlit as st

from db.supabase_client import SupabaseClient
from pipeline.config import MODELS, MAX_RETRIES, RETRY_BASE_DELAY_SECONDS, STAGE_MAX_TOKENS, MAX_TOKENS_DEFAULT, THINKING_BUDGET_TOKENS

PROMPTS_DIR = Path(__file__).parent.parent / "prompts"

# ── API config cache (populated in main thread, used by background threads) ──
_api_config: dict[str, str] = {}


def init_api_config():
    """Call from Streamlit main thread to cache API secrets for background use."""
    _api_config["api_key"] = st.secrets["ANTHROPIC_API_KEY"]
    _api_config["base_url"] = st.secrets.get("ANTHROPIC_BASE_URL", "")


class ClarificationNeeded(Exception):
    """Raised when an agent needs user input to continue."""

    def __init__(self, stage_name: str, questions: list[str], context: str, partial_output: dict, log_id: str):
        self.stage_name = stage_name
        self.questions = questions
        self.context = context
        self.partial_output = partial_output
        self.log_id = log_id
        super().__init__(f"Agent {stage_name} needs clarification: {questions}")


class BaseAgent:
    """Base class for all pipeline agents. Subclasses set stage_name and prompt_file."""

    stage_name: str = ""
    prompt_file: str = ""  # relative to pipeline/prompts/

    def __init__(self):
        self.model = MODELS.get(self.stage_name, "claude-sonnet-4-20250514")
        self.max_tokens = STAGE_MAX_TOKENS.get(self.stage_name, MAX_TOKENS_DEFAULT)

    def _get_client(self) -> anthropic.Anthropic:
        if not _api_config:
            init_api_config()
        kwargs: dict[str, str] = {"api_key": _api_config["api_key"]}
        if _api_config.get("base_url"):
            kwargs["base_url"] = _api_config["base_url"]
        return anthropic.Anthropic(**kwargs)

    def load_system_prompt(self) -> str:
        """Load agent-specific prompt + append shared foundation knowledge."""
        path = PROMPTS_DIR / self.prompt_file
        prompt = path.read_text(encoding="utf-8")
        # Inject foundation methodology knowledge
        foundation_path = PROMPTS_DIR / "foundation.md"
        if foundation_path.exists():
            prompt += "\n\n---\n\n" + foundation_path.read_text(encoding="utf-8")
        return prompt

    @property
    def _use_thinking(self) -> bool:
        return "opus" in self.model

    def _call_claude(self, system_prompt: str, user_message: str) -> tuple[str, int, int]:
        """Synchronous Claude API call. Returns (response_text, input_tokens, output_tokens).

        Opus models use extended thinking for deeper reasoning.
        """
        client = self._get_client()

        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": [{"role": "user", "content": system_prompt + "\n\n---\n\n" + user_message}]
            if self._use_thinking
            else [{"role": "user", "content": user_message}],
        }

        if self._use_thinking:
            # Extended thinking: system prompt goes into user message (thinking doesn't support system param),
            # and we enable the thinking block with a budget.
            kwargs["thinking"] = {
                "type": "enabled",
                "budget_tokens": THINKING_BUDGET_TOKENS,
            }
        else:
            kwargs["system"] = system_prompt

        response = client.messages.create(**kwargs)

        # Extract text from response — thinking responses have multiple content blocks
        text = ""
        for block in response.content:
            if block.type == "text":
                text = block.text
                break

        input_tokens = response.usage.input_tokens
        output_tokens = response.usage.output_tokens
        return text, input_tokens, output_tokens

    @staticmethod
    def _extract_json(response_text: str) -> dict[str, Any]:
        """Extract JSON from Claude response, handling markdown code blocks."""
        # Try direct parse
        try:
            return json.loads(response_text)
        except json.JSONDecodeError:
            pass

        # Try ```json ... ``` block
        match = re.search(r"```json\s*\n?(.*?)\n?\s*```", response_text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass

        # Try any ``` ... ``` block
        match = re.search(r"```\s*\n?(.*?)\n?\s*```", response_text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass

        # Try outermost braces
        match = re.search(r"\{.*\}", response_text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass

        raise ValueError(f"Could not extract JSON from response: {response_text[:500]}")

    async def run(self, input_data: dict, run_id: str, db: SupabaseClient) -> dict:
        """Execute the agent: log → call Claude → parse → check clarification → update log."""
        log = db.create_stage_log(run_id, self.stage_name, input_data)
        log_id = log["id"]
        start_time = time.time()
        total_input_tokens = 0
        total_output_tokens = 0

        last_error = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                system_prompt = self.load_system_prompt()
                user_message = json.dumps(input_data, ensure_ascii=False, indent=2)

                text, input_tokens, output_tokens = await asyncio.to_thread(
                    self._call_claude, system_prompt, user_message
                )
                total_input_tokens += input_tokens
                total_output_tokens += output_tokens

                output = self._extract_json(text)
                duration = time.time() - start_time

                # ── Check if agent is requesting clarification ─────────
                if output.get("status") == "needs_clarification":
                    db.update_stage_log(
                        log_id,
                        status="needs_input",
                        output_data=output,
                        model_used=self.model,
                        tokens_used=total_input_tokens + total_output_tokens,
                        duration_seconds=round(duration, 2),
                    )
                    raise ClarificationNeeded(
                        stage_name=self.stage_name,
                        questions=output.get("questions", []),
                        context=output.get("context", ""),
                        partial_output=output.get("partial_output", {}),
                        log_id=log_id,
                    )

                db.update_stage_log(
                    log_id,
                    status="completed",
                    output_data=output,
                    model_used=self.model,
                    tokens_used=total_input_tokens + total_output_tokens,
                    duration_seconds=round(duration, 2),
                )
                return output

            except ClarificationNeeded:
                raise  # Don't retry clarification requests

            except Exception as e:
                last_error = e
                if attempt < MAX_RETRIES:
                    delay = RETRY_BASE_DELAY_SECONDS * (attempt + 1)
                    await asyncio.sleep(delay)

        # All retries exhausted
        duration = time.time() - start_time
        db.update_stage_log(
            log_id,
            status="failed",
            error_message=str(last_error),
            tokens_used=total_input_tokens + total_output_tokens,
            duration_seconds=round(duration, 2),
        )
        raise last_error  # type: ignore[misc]
