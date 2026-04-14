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
from pipeline.config import MODELS, MAX_RETRIES, RETRY_BASE_DELAY_SECONDS, STAGE_MAX_TOKENS, MAX_TOKENS_DEFAULT, THINKING_STAGES, THINKING_BUDGET_TOKENS, MIN_SECONDS_BETWEEN_CALLS

PROMPTS_DIR = Path(__file__).parent.parent / "prompts"

# ── API config cache (populated in main thread, used by background threads) ──
_api_config: dict[str, str] = {}


def init_api_config():
    """Call from Streamlit main thread to cache API secrets for background use.

    Supports two modes (auto-detected from secrets.toml):
    - Vertex AI:  GCP_PROJECT_ID + GCP_REGION (+ optional gcp_service_account section)
    - Relay/Direct: ANTHROPIC_API_KEY (+ optional ANTHROPIC_BASE_URL)
    """
    # Vertex AI mode
    if st.secrets.get("GCP_PROJECT_ID"):
        _api_config["mode"] = "vertex"
        _api_config["project_id"] = st.secrets["GCP_PROJECT_ID"]
        _api_config["region"] = st.secrets.get("GCP_REGION", "us-east5")

        # Build credentials from service account JSON in secrets (for Streamlit Cloud)
        if "gcp_service_account" in st.secrets:
            from google.oauth2 import service_account
            sa_info = dict(st.secrets["gcp_service_account"])
            credentials = service_account.Credentials.from_service_account_info(
                sa_info,
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )
            _api_config["credentials"] = credentials
            logger.info(
                f"[init_api_config] Vertex AI mode (Service Account): "
                f"project={_api_config['project_id']}, region={_api_config['region']}, "
                f"sa={sa_info.get('client_email', '?')}"
            )
        else:
            # No service account in secrets → rely on ADC (gcloud auth / env var)
            logger.info(
                f"[init_api_config] Vertex AI mode (ADC): "
                f"project={_api_config['project_id']}, region={_api_config['region']}"
            )
    else:
        # Legacy relay / direct Anthropic mode
        _api_config["mode"] = "direct"
        _api_config["api_key"] = st.secrets["ANTHROPIC_API_KEY"]
        base_url = st.secrets.get("ANTHROPIC_BASE_URL", "").rstrip("/")
        if base_url.endswith("/v1"):
            base_url = base_url[:-3]
        _api_config["base_url"] = base_url
        logger.info(
            f"[init_api_config] Direct/relay mode: "
            f"base_url={'(default)' if not base_url else base_url}"
        )


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

        if _api_config.get("mode") == "vertex":
            from anthropic import AnthropicVertex
            vertex_kwargs: dict[str, Any] = {
                "project_id": _api_config["project_id"],
                "region": _api_config["region"],
                "timeout": 900.0,
            }
            # Pass explicit credentials if service account JSON was in secrets
            if "credentials" in _api_config:
                vertex_kwargs["credentials"] = _api_config["credentials"]
            return AnthropicVertex(**vertex_kwargs)
        else:
            # Direct Anthropic / relay proxy
            kwargs: dict[str, Any] = {"api_key": _api_config["api_key"]}
            if _api_config.get("base_url"):
                kwargs["base_url"] = _api_config["base_url"]
            kwargs["timeout"] = 900.0  # 15 min
            return anthropic.Anthropic(**kwargs)

    # Agents that need strategy-level methodology (差异化/真实感/平台感知)
    _STRATEGY_STAGES = frozenset({
        "secretariat", "chancellery", "chancellery_final",
        "ministry_works", "ministry_works_cell_planner",
    })
    # Agents that need execution-level methodology (网感/钩子/范式) —
    # NOTE: works_builder and vibe agents already have full execution
    # knowledge hardcoded in their own prompt files. Appending foundation's
    # §2 网感 chapter would duplicate ~9.6KB of content they already have.
    # So execution agents get NO extra foundation beyond common.
    # Strategy agents get the lighter strategy foundation.
    # All agents get the common foundation (请旨协议).

    def load_system_prompt(self) -> str:
        """Load agent-specific prompt + append relevant foundation knowledge.

        Before v0.7.5, the FULL foundation.md (16KB, including 9.6KB of 网感
        rules) was appended to every single agent — 14 agents × 16KB = 224KB
        of redundant input tokens per pipeline run. Most agents don't need
        execution-level content rules (hooks, paradigms, real-person samples).

        Now segmented:
        - foundation_common.md (请旨协议): ALL agents
        - foundation_strategy.md (差异化/真实感/平台感知): strategy agents only
        - §2 网感 (9.6KB): NOT appended — already hardcoded in works_builder.md
          and vibe_critic.md
        """
        path = PROMPTS_DIR / self.prompt_file
        prompt = path.read_text(encoding="utf-8")

        # Always append common foundation (请旨协议)
        common_path = PROMPTS_DIR / "foundation_common.md"
        if common_path.exists():
            prompt += "\n\n---\n\n" + common_path.read_text(encoding="utf-8")

        # Strategy agents also get strategy methodology
        if self.stage_name in self._STRATEGY_STAGES:
            strategy_path = PROMPTS_DIR / "foundation_strategy.md"
            if strategy_path.exists():
                prompt += "\n\n---\n\n" + strategy_path.read_text(encoding="utf-8")

        return prompt

    @property
    def _use_thinking(self) -> bool:
        # Thinking is enabled for specific strategic stages, not tied to model name.
        # On Vertex: uses adaptive thinking (model decides depth).
        # On relay: uses budget_tokens=10000 as fallback.
        return self.stage_name in THINKING_STAGES

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

    # Class-level timestamp for rate limiting across all agents
    _last_call_time: float = 0.0

    def _call_claude(self, system_prompt: str, user_message: str) -> tuple[str, int, int]:
        """Synchronous Claude API call. Returns (response_text, input_tokens, output_tokens).

        Two modes:
        - Vertex AI: uses adaptive/budget thinking, system= works normally.
        - Relay/direct: legacy path with budget_tokens thinking + proxy fallbacks.
        """
        # Rate limiting: ensure minimum gap between API calls (Vertex 15K TPM)
        if MIN_SECONDS_BETWEEN_CALLS > 0:
            elapsed = time.time() - BaseAgent._last_call_time
            if elapsed < MIN_SECONDS_BETWEEN_CALLS:
                wait = MIN_SECONDS_BETWEEN_CALLS - elapsed
                logger.info(f"[{self.stage_name}] rate limit: waiting {wait:.1f}s")
                time.sleep(wait)
            BaseAgent._last_call_time = time.time()

        client = self._get_client()
        is_vertex = _api_config.get("mode") == "vertex"

        # Build content (may include image blocks)
        user_content = self._build_content_blocks(user_message)
        messages = [{"role": "user", "content": user_content}]

        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system_prompt,
            "messages": messages,
        }

        # ── Vertex AI path: clean, no workarounds ────────────────────────
        if is_vertex:
            if self._use_thinking:
                # Opus 4.6+: adaptive thinking. Older (4.1, 4.5): budget_tokens.
                if "4-6" in self.model:
                    kwargs["thinking"] = {"type": "adaptive"}
                else:
                    kwargs["thinking"] = {
                        "type": "enabled",
                        "budget_tokens": THINKING_BUDGET_TOKENS,
                    }
            # Always stream on Vertex (avoids timeout for large outputs)
            with client.messages.stream(**kwargs) as stream:
                response = stream.get_final_message()

        # ── Relay / direct path ────────────────────────────────────────────
        # IMPORTANT: relay does NOT support the `thinking` API parameter.
        # Thinking is controlled by model NAME: relay routes "-thinking"
        # suffix to a thinking-enabled backend. DO NOT pass `thinking={...}`
        # — the relay returns garbage (a canned help message) when it sees it.
        # Just use system= and messages= normally. Stream for timeout safety.
        else:
            with client.messages.stream(**kwargs) as stream:
                response = stream.get_final_message()

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
                f"[{self.stage_name}] thinking={has_thinking}, "
                f"blocks={[b.type for b in response.content]}, "
                f"mode={'vertex' if is_vertex else 'relay'}"
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
        """Repair truncated JSON by finding the last valid cut point and closing
        open containers in proper nesting order.

        Handles truncation mid-string, mid-value, or between fields. Uses a
        real stack (not counts) so nested structures like {[{[...]}]} close
        correctly as ]}]} instead of the buggy ]]}}.
        """
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
                    cut_points.append(i + 1)
                continue
            if in_string:
                continue
            if ch in "{[":
                pass  # container open — don't add cut here
            elif ch in "}]":
                cut_points.append(i + 1)
            elif ch == ",":
                cut_points.append(i)  # cut BEFORE comma is also valid

        # Try cut points from most recent to earliest (keep as much data as possible)
        for cp in reversed(cut_points[-300:]):
            candidate = fragment[:cp].rstrip().rstrip(",").rstrip()
            # Strip dangling `"key":` (key with no value, left when cut mid-value)
            candidate = re.sub(r',\s*"[^"]*"\s*:\s*$', "", candidate)
            candidate = re.sub(r'\{\s*"[^"]*"\s*:\s*$', "{", candidate)
            candidate = candidate.rstrip().rstrip(",")

            # Re-scan candidate with a real stack
            stack: list[str] = []
            ins = False
            esc = False
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
                if ch == "{":
                    stack.append("{")
                elif ch == "}":
                    if stack and stack[-1] == "{":
                        stack.pop()
                    else:
                        # Unmatched close — this candidate is malformed
                        stack = ["__BAD__"]
                        break
                elif ch == "[":
                    stack.append("[")
                elif ch == "]":
                    if stack and stack[-1] == "[":
                        stack.pop()
                    else:
                        stack = ["__BAD__"]
                        break

            if stack == ["__BAD__"] or ins:
                continue  # malformed or ended inside string
            if not stack:
                # Already balanced — try parsing as-is
                try:
                    result = BaseAgent._lenient_json_loads(candidate)
                    logger.info(
                        f"Truncation repair: cut at pos {cp}/{len(fragment)}, "
                        f"already balanced"
                    )
                    return result
                except (json.JSONDecodeError, ValueError):
                    continue

            # Build proper close suffix: reverse stack, flip { → } and [ → ]
            suffix = "".join("}" if c == "{" else "]" for c in reversed(stack))
            try:
                result = BaseAgent._lenient_json_loads(candidate + suffix)
                logger.info(
                    f"Truncation repair: cut at pos {cp}/{len(fragment)}, "
                    f"closed stack {stack} → suffix {suffix!r}"
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
