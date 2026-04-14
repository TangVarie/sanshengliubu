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
from pipeline.config import (
    COST_PER_1M_INPUT,
    COST_PER_1M_OUTPUT,
    ENABLE_PROMPT_CACHING,
    MAX_RETRIES,
    MAX_TOKENS_DEFAULT,
    MAX_TOKENS_PER_RUN,
    MIN_SECONDS_BETWEEN_CALLS,
    MODELS,
    RELAY_MIN_SECONDS_BETWEEN_CALLS,
    RETRY_BASE_DELAY_SECONDS,
    STAGE_MAX_TOKENS,
    THINKING_BUDGET_TOKENS,
    THINKING_STAGES,
)

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
        # Auto-throttle on relay: most relays have tighter concurrency caps
        # than native Anthropic (often 5 req/s). Only upgrade if the user
        # hasn't already set an explicit MIN_SECONDS_BETWEEN_CALLS > 0.
        if MIN_SECONDS_BETWEEN_CALLS <= 0 and RELAY_MIN_SECONDS_BETWEEN_CALLS > 0:
            _api_config["rate_limit_seconds"] = RELAY_MIN_SECONDS_BETWEEN_CALLS
            logger.info(
                f"[init_api_config] Direct/relay mode: "
                f"base_url={'(default)' if not base_url else base_url}, "
                f"auto-throttle={RELAY_MIN_SECONDS_BETWEEN_CALLS}s between calls"
            )
        else:
            _api_config["rate_limit_seconds"] = MIN_SECONDS_BETWEEN_CALLS
            logger.info(
                f"[init_api_config] Direct/relay mode: "
                f"base_url={'(default)' if not base_url else base_url}, "
                f"explicit throttle={MIN_SECONDS_BETWEEN_CALLS}s"
            )


def _effective_rate_limit_seconds() -> float:
    """The throttle actually enforced per call. init_api_config sets this
    per backend. Defaults to the static config constant if init wasn't
    called (unit tests, tooling) or no override exists."""
    return float(_api_config.get("rate_limit_seconds", MIN_SECONDS_BETWEEN_CALLS))


class ClarificationNeeded(Exception):
    """Raised when an agent needs user input to continue."""

    def __init__(self, stage_name: str, questions: list[str], context: str, partial_output: dict, log_id: str):
        self.stage_name = stage_name
        self.questions = questions
        self.context = context
        self.partial_output = partial_output
        self.log_id = log_id
        super().__init__(f"Agent {stage_name} needs clarification: {questions}")


class RunBudgetExceededError(RuntimeError):
    """Raised when a pipeline run exceeds MAX_TOKENS_PER_RUN. Surfaces as a
    failed run with an explicit cost-safety message so the operator knows
    the failure was a guardrail trip, not a model error."""


# ── Per-run budget tracker ─────────────────────────────────────────────
# Shared across all agents in the process so every _call_claude accrues
# into the same pot. Keyed by run_id to stay isolated across concurrent
# runs (if ever). Reset at the start of orchestrator.run() via
# reset_run_budget(run_id).
_run_totals: dict[str, dict[str, int]] = {}


def reset_run_budget(run_id: str) -> None:
    """Zero out token counters for a run. Called by orchestrator.run()
    before any agent executes, so resumed runs don't re-count tokens
    that were already charged in the prior attempt."""
    _run_totals[run_id] = {
        "input": 0,
        "output": 0,
        "cache_read": 0,
        "cache_creation": 0,
        "cost_usd": 0.0,
        "calls": 0,
        "calls_with_cache_activity": 0,
    }


def _estimate_call_cost_usd(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read: int,
    cache_creation: int,
) -> float:
    """Approximate the dollar cost of one Claude call.

    Anthropic publishes:
      - cache_creation_input_tokens bill at 1.25× input rate (write premium)
      - cache_read_input_tokens bill at 0.10× input rate (read discount)
      - regular input_tokens and output_tokens at their standard rates

    `input_tokens` from response.usage is the NON-cached portion
    (Anthropic splits them out when caching is active). So we add the
    three input categories independently — no double-counting.

    Unknown model → input and output priced at 0; the call just won't
    accrue (operator will see $0.00 and know to add the model to
    COST_PER_1M_*). We deliberately don't estimate from a default so
    wrong cost data isn't silently reported.
    """
    in_rate = COST_PER_1M_INPUT.get(model, 0.0)
    out_rate = COST_PER_1M_OUTPUT.get(model, 0.0)
    if in_rate == 0.0 and out_rate == 0.0:
        return 0.0
    return (
        input_tokens * in_rate
        + output_tokens * out_rate
        + cache_read * in_rate * 0.10
        + cache_creation * in_rate * 1.25
    ) / 1_000_000


def get_run_totals(run_id: str) -> dict[str, int]:
    """Expose the live counters for UI / observability."""
    return dict(_run_totals.get(run_id, {}))


def accumulate_auxiliary_cost(
    run_id: str,
    cost_usd: float,
    input_tokens: int = 0,
    output_tokens: int = 0,
    source: str = "auxiliary",
) -> None:
    """Add cost from an out-of-band (non-Claude) backend to the run's
    running totals. Used by Gemini assist so its spend shows up in
    pipeline_run.total_cost_usd alongside Claude's.

    Does NOT count toward MAX_TOKENS_PER_RUN — that ceiling is there
    to bound runaway Claude retry loops, and Gemini (priced 10-100×
    cheaper for Flash/Pro tiers) would underflow the guard into
    irrelevance. Tokens are still tracked per-source for UI breakdown.
    """
    totals = _run_totals.setdefault(
        run_id,
        {
            "input": 0,
            "output": 0,
            "cache_read": 0,
            "cache_creation": 0,
            "cost_usd": 0.0,
            "calls": 0,
            "calls_with_cache_activity": 0,
        },
    )
    totals["cost_usd"] = float(totals.get("cost_usd", 0.0)) + float(cost_usd)
    # Stash per-source tokens under namespaced keys so the breakdown
    # stays visible in the UI without polluting the budget-check path.
    aux_tokens_key = f"aux_{source}_tokens"
    totals[aux_tokens_key] = (
        int(totals.get(aux_tokens_key, 0)) + int(input_tokens) + int(output_tokens)
    )


def _accumulate_run_tokens(
    run_id: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read: int = 0,
    cache_creation: int = 0,
) -> tuple[dict, float]:
    """Accumulate one call's usage into the run-level pot. Returns
    (run_totals_dict, this_call_cost_usd) so the caller can write the
    per-stage cost to stage_logs without recomputing."""
    totals = _run_totals.setdefault(
        run_id,
        {
            "input": 0,
            "output": 0,
            "cache_read": 0,
            "cache_creation": 0,
            "cost_usd": 0.0,
            "calls": 0,
            "calls_with_cache_activity": 0,
        },
    )
    totals["input"] += input_tokens
    totals["output"] += output_tokens
    totals["cache_read"] += cache_read
    totals["cache_creation"] += cache_creation
    totals["calls"] += 1
    if cache_read > 0 or cache_creation > 0:
        totals["calls_with_cache_activity"] += 1
    this_call_cost = _estimate_call_cost_usd(
        model, input_tokens, output_tokens, cache_read, cache_creation
    )
    totals["cost_usd"] = float(totals.get("cost_usd", 0.0)) + this_call_cost
    return totals, this_call_cost


def _check_run_budget(run_id: str) -> None:
    """Raise RunBudgetExceededError if the run has passed MAX_TOKENS_PER_RUN.
    Called right after every API response is accounted for."""
    totals = _run_totals.get(run_id)
    if not totals:
        return
    combined = totals["input"] + totals["output"]
    if combined > MAX_TOKENS_PER_RUN:
        raise RunBudgetExceededError(
            f"流水线 {run_id[:8]} 已累计 {combined:,} tokens "
            f"(input={totals['input']:,}, output={totals['output']:,})，"
            f"超过 MAX_TOKENS_PER_RUN={MAX_TOKENS_PER_RUN:,}。"
            f"为防止重试失控，已强制终止。如需继续请在 pipeline/config.py 调高上限，"
            f"或人工排查为什么某个阶段反复重试。"
        )


def _maybe_warn_no_cache_hits(run_id: str) -> None:
    """After the first few calls, if ENABLE_PROMPT_CACHING is on but the
    backend reports zero cache activity, log once so the operator knows
    the cache_control field is being dropped by their relay. Silent cache
    misses = paying full price for nothing."""
    if not ENABLE_PROMPT_CACHING:
        return
    totals = _run_totals.get(run_id)
    if not totals or totals["calls"] < 4:
        return
    # Already warned for this run?
    if totals.get("_cache_warned"):
        return
    if totals["calls_with_cache_activity"] == 0:
        logger.warning(
            "[cache-probe] run=%s: made %d calls with cache_control but the "
            "backend reports 0 cache activity (no cache_read or "
            "cache_creation tokens). Your relay likely drops the "
            "cache_control field silently. Consider setting "
            "ENABLE_PROMPT_CACHING=False in pipeline/config.py to avoid "
            "sending dead weight.",
            run_id[:8],
            totals["calls"],
        )
        totals["_cache_warned"] = True


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

    def _call_claude(
        self, system_prompt: str, user_message: str
    ) -> tuple[str, int, int, int, int]:
        """Synchronous Claude API call.

        Returns (response_text, input_tokens, output_tokens,
        cache_read_input_tokens, cache_creation_input_tokens). The last two
        are 0 when caching isn't active or the backend doesn't report them
        (common on relays that silently drop cache_control). They feed the
        per-run cache-effectiveness probe.

        Two modes:
        - Vertex AI: uses adaptive/budget thinking, system= works normally.
        - Relay/direct: legacy path with budget_tokens thinking + proxy fallbacks.
        """
        # Rate limiting. The effective value is picked by init_api_config
        # (relay mode auto-throttles even if the static config constant is 0).
        rate_limit = _effective_rate_limit_seconds()
        if rate_limit > 0:
            elapsed = time.time() - BaseAgent._last_call_time
            if elapsed < rate_limit:
                wait = rate_limit - elapsed
                logger.info(f"[{self.stage_name}] rate limit: waiting {wait:.2f}s")
                time.sleep(wait)
            BaseAgent._last_call_time = time.time()

        client = self._get_client()
        is_vertex = _api_config.get("mode") == "vertex"

        # Build content (may include image blocks)
        user_content = self._build_content_blocks(user_message)
        messages = [{"role": "user", "content": user_content}]

        # Structured system prompt with ephemeral cache control. Anthropic
        # caches the system portion for ~5 minutes — subsequent calls within
        # the window pay ~10% input rate for it. Foundation + agent prompt
        # together are ~5-15KB per agent, and we call each agent multiple
        # times per run (strategy loop, retry rounds, batch retries), so the
        # cache hit rate is high. Falls back to plain string when caching is
        # disabled (e.g. for relays that 400 on cache_control).
        if ENABLE_PROMPT_CACHING:
            system_block: Any = [
                {
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
        else:
            system_block = system_prompt

        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system_block,
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
        # Cache usage fields are present when the backend supports prompt
        # caching. Missing / None = silently unsupported (typical on many
        # relay proxies). getattr falls back to 0 without crashing on older
        # SDK versions that lack these attributes.
        cache_read = int(getattr(response.usage, "cache_read_input_tokens", 0) or 0)
        cache_creation = int(
            getattr(response.usage, "cache_creation_input_tokens", 0) or 0
        )
        return text, input_tokens, output_tokens, cache_read, cache_creation

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
            # Strip a dangling partial field. Order matters — more specific
            # patterns first. All regexes assume the candidate has no unclosed
            # strings (cut_points are only recorded outside strings). After
            # these substitutions the candidate should end at a clean structure
            # boundary (`}`, `]`, a closed string value, a number, or
            # `true`/`false`/`null`).
            candidate = re.sub(r',\s*"[^"]*"\s*:\s*$', "", candidate)  # , "k":
            candidate = re.sub(r'\{\s*"[^"]*"\s*:\s*$', "{", candidate)  # { "k":
            # NEW: lone trailing keys with no colon yet (truncation hit between
            # the key and its colon). Matches `, "k"` and `{ "k"` respectively.
            candidate = re.sub(r',\s*"[^"]*"\s*$', "", candidate)  # , "k"
            candidate = re.sub(r'\{\s*"[^"]*"\s*$', "{", candidate)  # { "k"
            # NEW: trailing array item that is just a lone string with no
            # preceding comma — e.g. `[ "a", "b"` is fine (commas delimit)
            # but `["a"` after a cut is fine too (stack handles it). We only
            # need to handle `[ "a", ` which rstrip already covers.
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

                (
                    text,
                    input_tokens,
                    output_tokens,
                    cache_read,
                    cache_creation,
                ) = await asyncio.to_thread(
                    self._call_claude, system_prompt, user_message
                )
                total_input_tokens += input_tokens
                total_output_tokens += output_tokens

                # Feed the per-run budget tracker. Raises RunBudgetExceededError
                # if we've blown through MAX_TOKENS_PER_RUN — caught at the
                # orchestrator level which marks the run as failed. Runs
                # BEFORE JSON extraction so a catastrophic reply doesn't also
                # get billed into a loop.
                _, _call_cost = _accumulate_run_tokens(
                    run_id,
                    self.model,
                    input_tokens,
                    output_tokens,
                    cache_read,
                    cache_creation,
                )
                _check_run_budget(run_id)
                _maybe_warn_no_cache_hits(run_id)

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
                # Push the run-level cost + token totals to pipeline_runs so
                # the UI can display current spend without needing access to
                # the in-process _run_totals dict. Swallow DB errors — cost
                # tracking is observability, not a hard invariant. One small
                # write per agent completion, at most ~14/run.
                try:
                    _run_total = _run_totals.get(run_id) or {}
                    _run_tokens = (
                        _run_total.get("input", 0) + _run_total.get("output", 0)
                    )
                    _run_cost = round(float(_run_total.get("cost_usd", 0.0)), 4)
                    db.update_pipeline_run(
                        run_id,
                        total_tokens=_run_tokens,
                        total_cost_usd=_run_cost,
                    )
                except Exception:
                    logger.warning(
                        "[%s] failed to push run totals to pipeline_runs",
                        self.stage_name,
                        exc_info=True,
                    )
                return output

            except ClarificationNeeded:
                raise  # Don't retry clarification requests

            except RunBudgetExceededError:
                # Don't retry budget exhaustion — that would just double-down
                # on the failure. Let it bubble up to the orchestrator, which
                # will mark the run as failed. Still log the stage as failed
                # so the UI shows which stage tripped the guard.
                duration = time.time() - start_time
                db.update_stage_log(
                    log_id,
                    status="failed",
                    error_message=(
                        "RunBudgetExceededError: 流水线累计 token 超过 "
                        "MAX_TOKENS_PER_RUN 上限，已强制终止以防成本失控。"
                        "请检查是否有阶段在反复重试，或在 pipeline/config.py "
                        "调高 MAX_TOKENS_PER_RUN。"
                    ),
                    tokens_used=total_input_tokens + total_output_tokens,
                    duration_seconds=round(duration, 2),
                )
                raise

            except Exception as e:
                last_error = e
                # Capture full traceback so we never end up with an empty error_message
                logger.exception(
                    f"[{self.stage_name}] attempt {attempt + 1}/{MAX_RETRIES + 1} failed"
                )
                if attempt < MAX_RETRIES:
                    # True exponential backoff (see config.py). For MAX_RETRIES=2
                    # the sequence is 3s, 6s — unchanged from the old linear
                    # path; the formula is kept exponential so MAX_RETRIES can
                    # grow without changing the curve's shape.
                    delay = RETRY_BASE_DELAY_SECONDS * (2 ** attempt)
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
        # Cap to avoid blowing up the DB column; but leave an explicit marker
        # so the UI/operator knows the tail was cut instead of silently
        # getting 8000 chars that look complete.
        if len(err_str) > 8000:
            truncated_msg = (
                err_str[:7900]
                + "\n\n[... 错误信息被截断于 8000 字符上限，完整 traceback 请看 "
                + "Streamlit 运行控制台日志 ...]"
            )
        else:
            truncated_msg = err_str
        db.update_stage_log(
            log_id,
            status="failed",
            error_message=truncated_msg,
            tokens_used=total_input_tokens + total_output_tokens,
            duration_seconds=round(duration, 2),
        )
        raise last_error  # type: ignore[misc]
