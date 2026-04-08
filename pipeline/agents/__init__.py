"""Base agent class for all pipeline agents."""

from __future__ import annotations

import asyncio
import json
import re
import time
import traceback
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
        kwargs: dict[str, Any] = {"api_key": _api_config["api_key"]}
        if _api_config.get("base_url"):
            kwargs["base_url"] = _api_config["base_url"]
        # Generous timeout — proxy may be slow, thinking calls use streaming
        kwargs["timeout"] = 900.0  # 15 min
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
        # Model name is the switch: any model with "thinking" in its ID uses
        # extended thinking. If the relay's -thinking channel fails, the
        # fallback in _call_claude strips the suffix and retries with the
        # base model name (no thinking).
        return "thinking" in self.model

    @staticmethod
    def _build_content_blocks(text: str) -> list[dict[str, Any]] | str:
        """Convert text with [BASE64_IMAGE:...] markers into multimodal content blocks."""
        if "[BASE64_IMAGE:" not in text:
            return text  # plain text, no conversion needed

        blocks: list[dict[str, Any]] = []
        parts = re.split(r"\[BASE64_IMAGE:(.*?)\]", text)
        # parts = [text_before, base64_data, text_after, base64_data, ...]
        for i, part in enumerate(parts):
            if i % 2 == 0:
                # Text segment
                stripped = part.strip()
                if stripped:
                    blocks.append({"type": "text", "text": stripped})
            else:
                # Base64 image data — detect media type from header
                b64 = part.strip()
                if b64.startswith("/9j/"):
                    media_type = "image/jpeg"
                elif b64.startswith("iVBOR"):
                    media_type = "image/png"
                elif b64.startswith("R0lGOD"):
                    media_type = "image/gif"
                elif b64.startswith("UklGR"):
                    media_type = "image/webp"
                else:
                    media_type = "image/png"  # default
                blocks.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": b64,
                    },
                })

        return blocks if blocks else text

    def _call_claude(self, system_prompt: str, user_message: str) -> tuple[str, int, int]:
        """Synchronous Claude API call. Returns (response_text, input_tokens, output_tokens).

        Opus thinking models first try with extended thinking + streaming.
        Falls back to plain call if the proxy doesn't support these features.
        """
        client = self._get_client()

        # Build content (may include image blocks)
        user_content = self._build_content_blocks(user_message)

        # Build base kwargs
        if self._use_thinking:
            # Thinking mode: system prompt goes into user message
            if isinstance(user_content, str):
                combined = system_prompt + "\n\n---\n\n" + user_content
            else:
                # Prepend system prompt as text block before image blocks
                combined = [{"type": "text", "text": system_prompt + "\n\n---\n\n"}] + user_content
            messages = [{"role": "user", "content": combined}]
        else:
            messages = [{"role": "user", "content": user_content}]

        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": messages,
        }

        if not self._use_thinking:
            kwargs["system"] = system_prompt

        # Try with thinking first, fall back to plain call if proxy doesn't support it
        response = None
        if self._use_thinking:
            budget = STAGE_THINKING_BUDGET.get(self.stage_name, THINKING_BUDGET_TOKENS)
            thinking_kwargs = {
                **kwargs,
                "thinking": {"type": "enabled", "budget_tokens": budget},
            }
            try:
                # Streaming is required by SDK for long-running thinking requests (>10min).
                # Use stream context manager and collect final message.
                with client.messages.stream(**thinking_kwargs) as stream:
                    response = stream.get_final_message()
            except Exception as e:
                err_str = str(e) or repr(e)
                # Fallback for proxy-incompatibility errors (thinking unsupported, etc.)
                err_lower = err_str.lower()
                if (
                    "构建请求" in err_str
                    or "thinking" in err_lower
                    or "model_not_found" in err_lower
                    or ("model" in err_lower and "not" in err_lower)
                    or (not err_str.strip())  # empty body from proxy → also try fallback
                ):
                    # Strip -thinking suffix from model name in case the proxy doesn't
                    # recognize it at all. Fall back to base model name + plain call.
                    base_model = self.model.replace("-thinking", "")
                    logger.warning(
                        f"[{self.stage_name}] thinking stream failed ({e!r}), "
                        f"falling back to plain call: model {self.model} → {base_model}"
                    )
                    kwargs["model"] = base_model
                    kwargs["system"] = system_prompt
                    kwargs["messages"] = [{"role": "user", "content": user_message}]
                else:
                    raise  # re-raise quota, network, etc. errors

        if response is None:
            # Non-thinking path (or thinking fallback): plain create.
            # max_tokens is small enough that the SDK won't enforce streaming.
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
    def _lenient_json_loads(text: str) -> dict[str, Any]:
        """Parse JSON tolerantly: allow control chars in strings, normalize smart quotes.

        LLMs frequently emit multi-line content inside JSON string values without
        escaping the newlines/tabs. Python's strict=False decoder accepts these.
        Also normalizes smart/chinese quotes to ASCII equivalents before parsing.
        """
        # Normalize common non-ASCII quote variants that LLMs sometimes produce
        # around JSON keys or values. We only touch quotes; Chinese punctuation
        # inside string VALUES is fine and gets preserved naturally.
        normalized = (
            text
            .replace("\u201c", '"')  # left double quote
            .replace("\u201d", '"')  # right double quote
            .replace("\u2018", "'")  # left single quote
            .replace("\u2019", "'")  # right single quote
        )
        return json.JSONDecoder(strict=False).decode(normalized)

    @staticmethod
    def _extract_json(response_text: str) -> dict[str, Any]:
        """Extract JSON from Claude response.

        Handles: markdown code blocks, outermost braces, unescaped control chars
        inside strings (common LLM mistake with multi-line system_prompt values),
        smart quotes, and truncation.
        """
        loads = BaseAgent._lenient_json_loads

        # Try direct parse
        try:
            return loads(response_text)
        except (json.JSONDecodeError, ValueError):
            pass

        # Try ```json ... ``` block
        match = re.search(r"```json\s*\n?(.*?)\n?\s*```", response_text, re.DOTALL)
        if match:
            try:
                return loads(match.group(1))
            except (json.JSONDecodeError, ValueError):
                pass

        # Try any ``` ... ``` block
        match = re.search(r"```\s*\n?(.*?)\n?\s*```", response_text, re.DOTALL)
        if match:
            try:
                return loads(match.group(1))
            except (json.JSONDecodeError, ValueError):
                pass

        # Try outermost braces
        match = re.search(r"\{.*\}", response_text, re.DOTALL)
        if match:
            try:
                return loads(match.group(0))
            except (json.JSONDecodeError, ValueError):
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
        """Repair truncated JSON by finding the last valid cut point and closing brackets.

        Handles truncation mid-string, mid-value, or between fields.
        """
        # Track JSON structure and remember valid cut points
        open_braces = 0
        open_brackets = 0
        in_string = False
        escape_next = False
        # Positions right after a complete value where we could safely cut
        cut_points: list[int] = []

        for i, ch in enumerate(fragment):
            if escape_next:
                escape_next = False
                continue
            if ch == "\\" and in_string:
                escape_next = True
                continue
            if ch == '"':
                in_string = not in_string
                if not in_string:
                    # Just closed a string — valid cut after this
                    cut_points.append(i + 1)
                continue
            if in_string:
                continue
            if ch == "{":
                open_braces += 1
            elif ch == "}":
                open_braces -= 1
                cut_points.append(i + 1)
            elif ch == "[":
                open_brackets += 1
            elif ch == "]":
                open_brackets -= 1
                cut_points.append(i + 1)
            elif ch == ",":
                cut_points.append(i)  # cut BEFORE comma is also valid

        if open_braces <= 0 and open_brackets <= 0:
            return None  # Not a truncation issue

        # Try cut points from most recent to earliest (keep as much data as possible)
        for cp in reversed(cut_points[-200:]):  # check last 200 cut points
            candidate = fragment[:cp].rstrip().rstrip(",").rstrip()
            # Strip dangling `"key":` (key with no value, left when cut mid-value)
            candidate = re.sub(r',\s*"[^"]*"\s*:\s*$', "", candidate)
            candidate = re.sub(r'\{\s*"[^"]*"\s*:\s*$', "{", candidate)
            candidate = candidate.rstrip().rstrip(",")

            # Recount open brackets for this candidate
            ob, obrk, ins, esc = 0, 0, False, False
            for ch in candidate:
                if esc:
                    esc = False
                    continue
                if ch == "\\" and ins:
                    esc = True
                    continue
                if ch == '"':
                    ins = not ins
                    continue
                if ins:
                    continue
                if ch == "{": ob += 1
                elif ch == "}": ob -= 1
                elif ch == "[": obrk += 1
                elif ch == "]": obrk -= 1

            if ob <= 0 or ins:
                continue  # over-closed or ended inside string

            suffix = "]" * obrk + "}" * ob
            try:
                result = BaseAgent._lenient_json_loads(candidate + suffix)
                logger.info(
                    f"Truncation repair: cut at pos {cp}/{len(fragment)}, "
                    f"closed {ob} braces + {obrk} brackets"
                )
                return result
            except (json.JSONDecodeError, ValueError):
                continue

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
                # Capture full traceback so we never end up with an empty error_message
                logger.exception(
                    f"[{self.stage_name}] attempt {attempt + 1}/{MAX_RETRIES + 1} failed"
                )
                if attempt < MAX_RETRIES:
                    delay = RETRY_BASE_DELAY_SECONDS * (attempt + 1)
                    await asyncio.sleep(delay)

        # All retries exhausted — build a robust, non-empty error message
        duration = time.time() - start_time
        err_str = ""
        if last_error is not None:
            try:
                err_str = str(last_error) or repr(last_error)
            except Exception:
                err_str = repr(last_error)
            tb = traceback.format_exception(type(last_error), last_error, last_error.__traceback__)
            err_str = (
                f"{type(last_error).__name__}: {err_str}\n"
                f"model={self.model} max_tokens={self.max_tokens}\n"
                f"--- traceback ---\n{''.join(tb)}"
            )
        if not err_str.strip():
            err_str = (
                f"Stage {self.stage_name} failed after {MAX_RETRIES + 1} attempts "
                f"but no error info was captured (last_error={last_error!r}, "
                f"model={self.model})"
            )
        db.update_stage_log(
            log_id,
            status="failed",
            error_message=err_str[:8000],  # cap to avoid blowing up the column
            tokens_used=total_input_tokens + total_output_tokens,
            duration_seconds=round(duration, 2),
        )
        raise last_error  # type: ignore[misc]
