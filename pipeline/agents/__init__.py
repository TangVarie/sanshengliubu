"""Base agent class for all pipeline agents."""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any

import logging

import anthropic
import streamlit as st

logger = logging.getLogger(__name__)

from db.supabase_client import SupabaseClient
from pipeline.config import MODELS, MAX_RETRIES, RETRY_BASE_DELAY_SECONDS, STAGE_MAX_TOKENS, MAX_TOKENS_DEFAULT, THINKING_BUDGET_TOKENS, STAGE_THINKING_BUDGET

PROMPTS_DIR = Path(__file__).parent.parent / "prompts"

# ── API config cache (populated in main thread, used by background threads) ──
_api_config: dict[str, str] = {}


def init_api_config():
    """Call from Streamlit main thread to cache API secrets for background use."""
    _api_config["api_key"] = st.secrets["ANTHROPIC_API_KEY"]
    base_url = st.secrets.get("ANTHROPIC_BASE_URL", "").rstrip("/")
    # SDK auto-appends /v1, strip if user already included it
    if base_url.endswith("/v1"):
        base_url = base_url[:-3]
    _api_config["base_url"] = base_url


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
            budget = STAGE_THINKING_BUDGET.get(self.stage_name, THINKING_BUDGET_TOKENS)
            kwargs["thinking"] = {
                "type": "enabled",
                "budget_tokens": budget,
            }
        else:
            kwargs["system"] = system_prompt

        if self._use_thinking:
            # Streaming required for long-running thinking requests (>10 min)
            with client.messages.stream(**kwargs) as stream:
                response = stream.get_final_message()
        else:
            response = client.messages.create(**kwargs)

        # Extract text from response — thinking responses have multiple content blocks
        text = ""
        has_thinking = False
        for block in response.content:
            if block.type == "thinking":
                has_thinking = True
            if block.type == "text":
                text = block.text
                break

        if self._use_thinking:
            logger.info(
                f"[{self.stage_name}] thinking enabled={self._use_thinking}, "
                f"response has thinking block={has_thinking}, "
                f"content block types={[b.type for b in response.content]}"
            )

        input_tokens = response.usage.input_tokens
        output_tokens = response.usage.output_tokens
        return text, input_tokens, output_tokens

    @staticmethod
    def _extract_json(response_text: str) -> dict[str, Any]:
        """Extract JSON from Claude response, handling markdown code blocks and truncation."""
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

        # Try to repair truncated JSON (response cut off by max_tokens)
        json_start = response_text.find("{")
        if json_start >= 0:
            fragment = response_text[json_start:]
            repaired = BaseAgent._try_repair_truncated_json(fragment)
            if repaired is not None:
                logger.warning(
                    f"JSON was truncated (len={len(response_text)}), repaired by closing brackets"
                )
                return repaired

        raise ValueError(
            f"Could not extract JSON from response (len={len(response_text)}, "
            f"last 200 chars: ...{response_text[-200:]!r})"
        )

    @staticmethod
    def _try_repair_truncated_json(fragment: str) -> dict[str, Any] | None:
        """Attempt to repair truncated JSON by closing unclosed brackets/braces."""
        # Track open brackets
        open_braces = 0
        open_brackets = 0
        in_string = False
        escape_next = False

        for ch in fragment:
            if escape_next:
                escape_next = False
                continue
            if ch == "\\":
                if in_string:
                    escape_next = True
                continue
            if ch == '"' and not escape_next:
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == "{":
                open_braces += 1
            elif ch == "}":
                open_braces -= 1
            elif ch == "[":
                open_brackets += 1
            elif ch == "]":
                open_brackets -= 1

        if open_braces <= 0 and open_brackets <= 0:
            return None  # Not a truncation issue

        # Truncate at last complete value (last comma or colon+value)
        # Then close all open brackets/braces
        # Strip trailing incomplete value after last comma
        trimmed = fragment.rstrip()
        # Remove trailing incomplete key-value or array element
        for end_char in (",", "[", "{", ":"):
            idx = trimmed.rfind(end_char)
            if idx > 0:
                candidate = trimmed[: idx].rstrip().rstrip(",")
                suffix = "]" * open_brackets + "}" * open_braces
                try:
                    return json.loads(candidate + suffix)
                except json.JSONDecodeError:
                    continue

        # Last resort: just close brackets
        suffix = "]" * open_brackets + "}" * open_braces
        try:
            return json.loads(fragment + suffix)
        except json.JSONDecodeError:
            return None

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
