"""Base agent class for all pipeline agents."""

from __future__ import annotations

import asyncio
import collections
import contextlib
import json
import re
import threading
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
    CLAUDE_MAX_CONCURRENT,
    CLAUDE_RPM_LIMIT,
    COST_PER_1M_INPUT,
    COST_PER_1M_OUTPUT,
    ENABLE_PROMPT_CACHING,
    MAX_RETRIES,
    MAX_TOKENS_DEFAULT,
    MAX_TOKENS_PER_RUN,
    MODELS,
    RETRY_BASE_DELAY_SECONDS,
    STAGE_MAX_TOKENS,
    THINKING_BUDGET_TOKENS,
    THINKING_STAGES,
)

PROMPTS_DIR = Path(__file__).parent.parent / "prompts"

# ── API config cache (populated in main thread, used by background threads) ──
_api_config: dict[str, str] = {}
# Guard the narrow write-during-run path in _call_claude: when the backend
# rejects cache_control with 400, multiple concurrent agent threads can all
# hit the same fault simultaneously. Without this lock N threads each do a
# redundant fallback retry before any of them flips the "cache disabled" bit
# in _api_config. Init-time writes to _api_config are single-threaded so they
# don't need the lock.
_api_config_lock = threading.Lock()


def _list_claude_relay_presets() -> dict[str, dict]:
    """Read all [claude_relay_presets.xxx] sections from secrets.toml
    into a plain dict keyed by preset name. Empty if none defined.

    Falls back to a single synthesized preset named "default" when the
    user has only the legacy top-level ANTHROPIC_API_KEY / BASE_URL set
    — keeps backward compat with secrets.toml files written before the
    multi-preset feature.
    """
    raw = st.secrets.get("claude_relay_presets")
    presets: dict[str, dict] = {}
    if raw:
        for name, cfg in dict(raw).items():
            cfg_d = dict(cfg)
            presets[str(name)] = cfg_d

    if not presets:
        # Legacy fallback: if user hasn't migrated to multi-preset yet,
        # wrap their top-level ANTHROPIC_API_KEY into a synthetic preset
        # so the rest of the plumbing (init_api_config / settings page)
        # has a uniform structure to work with.
        legacy_key = st.secrets.get("ANTHROPIC_API_KEY", "")
        if legacy_key:
            presets["default"] = {
                "label": "默认（来自顶层 ANTHROPIC_API_KEY）",
                "api_key": legacy_key,
                "base_url": st.secrets.get("ANTHROPIC_BASE_URL", ""),
                "rpm_limit": None,  # use global defaults
                "max_concurrent": None,
                "supports_cache": None,  # use ENABLE_PROMPT_CACHING
                "supports_adaptive_thinking": None,  # infer from model
            }
    return presets


def get_available_claude_relay_presets() -> dict[str, dict]:
    """Public accessor for UI callers. Same as internal helper."""
    return _list_claude_relay_presets()


def get_active_claude_relay_name() -> str:
    """Return the preset name currently in effect. Priority:
      1. Streamlit session override (set by the Settings page's switch
         button; only persists within the current Streamlit process)
      2. secrets.toml top-level ACTIVE_CLAUDE_RELAY
      3. First preset key in secrets.toml alphabetical order
      4. "" if no presets at all (Vertex mode or totally unconfigured)
    """
    # Session override takes precedence so the UI switch button works
    # without needing a restart.
    try:
        override = st.session_state.get("_active_claude_relay_override")
        if override:
            return str(override)
    except Exception:
        # Not in a Streamlit context (e.g. called from a background
        # thread before init_api_config has cached anything). Fall
        # through to the secrets lookup.
        pass

    presets = _list_claude_relay_presets()
    if not presets:
        return ""
    default = st.secrets.get("ACTIVE_CLAUDE_RELAY", "")
    if default and default in presets:
        return str(default)
    return next(iter(presets.keys()))


def init_api_config():
    """Call from Streamlit main thread to cache API secrets for background use.

    Two backends (auto-detected):
      - Vertex AI: GCP_PROJECT_ID + GCP_REGION (+ gcp_service_account)
      - Relay/Direct: one of the [claude_relay_presets.*] in secrets,
        picked by ACTIVE_CLAUDE_RELAY or session override. Falls back
        to legacy top-level ANTHROPIC_API_KEY when no presets defined.
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
        return

    # ── Relay / direct mode ──────────────────────────────────────────
    presets = _list_claude_relay_presets()
    active_name = get_active_claude_relay_name()

    # v0.30.0: DeepSeek 走官方 anthropic-compatible 端点(api.deepseek.com/
    # anthropic),独立 secret + 独立 base_url。stage 级用 deepseek-* 模型时,
    # _get_client_for_model 会路由到这里。和 tdyun/Claude/GPT 共存。
    _ds_key = (st.secrets.get("DEEPSEEK_API_KEY") or "").strip()
    if _ds_key:
        _api_config["deepseek"] = {
            "api_key": _ds_key,
            "base_url": (
                st.secrets.get("DEEPSEEK_BASE_URL")
                or "https://api.deepseek.com/anthropic"
            ).rstrip("/"),
        }
        logger.info(
            "[init_api_config] DeepSeek anthropic-compat 端点已配置 "
            "(base=%s)",
            _api_config["deepseek"]["base_url"],
        )
    else:
        _api_config.pop("deepseek", None)

    if not presets or not active_name:
        raise RuntimeError(
            "找不到可用的 Claude 接入配置：既没有 Vertex（GCP_PROJECT_ID），"
            "也没有 [claude_relay_presets.*] 或 ANTHROPIC_API_KEY。"
            "请补齐 .streamlit/secrets.toml。"
        )

    cfg = presets[active_name]
    _api_config["mode"] = "direct"
    _api_config["active_preset"] = active_name
    _api_config["active_label"] = str(cfg.get("label", active_name))
    _api_config["api_key"] = str(cfg.get("api_key", "") or "")

    base_url = str(cfg.get("base_url", "") or "").rstrip("/")
    if base_url.endswith("/v1"):
        base_url = base_url[:-3]
    _api_config["base_url"] = base_url

    # Per-preset behavioral flags. None means "use the global default".
    _api_config["rpm_limit"] = (
        int(cfg["rpm_limit"])
        if cfg.get("rpm_limit") not in (None, "")
        else int(CLAUDE_RPM_LIMIT)
    )
    _api_config["max_concurrent"] = (
        int(cfg["max_concurrent"])
        if cfg.get("max_concurrent") not in (None, "")
        else int(CLAUDE_MAX_CONCURRENT)
    )
    # supports_cache defaults to the global ENABLE_PROMPT_CACHING flag
    # so the existing config surface still works when a preset omits it.
    # Check if this specific preset had caching auto-disabled by a prior
    # 400 error (per-preset key) — if so, keep it disabled.
    _cache_disabled_key = f"_cache_disabled_{active_name}"
    if _api_config.get(_cache_disabled_key):
        _api_config["supports_cache"] = False
    elif cfg.get("supports_cache") is None:
        _api_config["supports_cache"] = bool(ENABLE_PROMPT_CACHING)
    else:
        _api_config["supports_cache"] = bool(cfg["supports_cache"])
    if cfg.get("supports_adaptive_thinking") is None:
        # Previously-assumed default: adaptive for Opus 4.6+, which is
        # what most modern relays support. Legacy relays that want the
        # old budget_tokens form set this to false explicitly.
        _api_config["supports_adaptive_thinking"] = True
    else:
        _api_config["supports_adaptive_thinking"] = bool(
            cfg["supports_adaptive_thinking"]
        )

    # Some relays (e.g. tdyun.ai) don't understand the `thinking` JSON
    # parameter at all and use a model-name suffix convention instead
    # (claude-opus-4-6-thinking / -high / -medium / -low / -max). When
    # this flag is true, _call_claude:
    #   1. Does NOT send the `thinking` JSON parameter
    #   2. Relies on the model name (resolved via model_overrides below)
    #      to carry thinking-tier information
    _api_config["thinking_via_model_suffix"] = bool(
        cfg.get("thinking_via_model_suffix", False)
    )

    # Per-stage model override. Takes precedence over config.py MODELS
    # when the active preset has an entry for that stage. Useful for
    # suffix-convention relays where different stages need different
    # thinking tiers: e.g. tdyun
    #   secretariat = "claude-opus-4-6-high"     (deep reasoning)
    #   crown_prince = "claude-opus-4-6-thinking" (medium)
    #   dispatcher = "claude-opus-4-6"            (no thinking)
    # Stages not listed fall back to the global MODELS default.
    raw_overrides = cfg.get("model_overrides") or {}
    try:
        _api_config["model_overrides"] = {
            str(k): str(v) for k, v in dict(raw_overrides).items()
        }
    except Exception:
        _api_config["model_overrides"] = {}

    # Refresh the shared rate limiter to match this preset. Any in-flight
    # calls continue under the old limits until they finish; new calls
    # made after this point respect the new values.
    _relay_limiter.reconfigure(
        _api_config["rpm_limit"], _api_config["max_concurrent"]
    )

    logger.info(
        f"[init_api_config] Direct/relay mode preset=%r (%s): "
        f"base_url=%s, RPM=%d, concurrent=%d, cache=%s, adaptive_thinking=%s, "
        f"thinking_via_suffix=%s, model_overrides=%d stages",
        active_name,
        _api_config["active_label"],
        "(default)" if not base_url else base_url,
        _api_config["rpm_limit"],
        _api_config["max_concurrent"],
        _api_config["supports_cache"],
        _api_config["supports_adaptive_thinking"],
        _api_config["thinking_via_model_suffix"],
        len(_api_config["model_overrides"]),
    )


# ── Sliding-window rate limiter ───────────────────────────────────────────
#
# Lives in this module because _call_claude is the chokepoint and runs in
# threads (via asyncio.to_thread). All primitives are threading-based, not
# asyncio-based, so they work correctly inside the thread-pool executor.
# A single shared limiter instance per process is correct: even with
# multiple parallel pipeline runs they all share the same backend quota.
class _SlidingWindowLimiter:
    """Caps API call STARTS at `max_per_minute` over a rolling 60-second
    window AND in-flight count at `max_concurrent`. Both constraints are
    enforced together — acquire blocks on whichever is binding.

    Usage:
        limiter = _SlidingWindowLimiter(15, 16)
        with limiter.slot(stage_name="ministry_works"):
            # ... actual API call ...

    Concurrency slot is held for the full call duration (until the with
    block exits). RPM slot is consumed at acquire time only — once the
    timestamp is appended, the call counts toward the window even if it
    later fails.
    """

    def __init__(self, max_per_minute: int, max_concurrent: int):
        self.max_per_minute = int(max_per_minute)
        self.max_concurrent = int(max_concurrent)
        self._lock = threading.Lock()
        self._history: collections.deque[float] = collections.deque()
        # threading.Semaphore is process-wide and works across asyncio
        # to_thread-spawned threads.
        self._concurrency_sem = threading.Semaphore(self.max_concurrent)

    def reconfigure(self, max_per_minute: int, max_concurrent: int) -> None:
        """Update the rate caps live — called by init_api_config when
        the active relay preset changes (each preset carries its own
        RPM / concurrency).

        RPM limit is updated under the lock so it lands atomically.

        Concurrency semaphore: rather than swapping the semaphore object
        (which leaves in-flight holders releasing into a dead reference),
        we keep the same object and only update max_concurrent. The slot()
        context manager checks the current max_concurrent value each time
        it acquires, so the effective cap adjusts naturally as in-flight
        calls complete.
        """
        with self._lock:
            self.max_per_minute = int(max_per_minute)
            self.max_concurrent = int(max_concurrent)

    def _wait_for_window(self, stage_name: str) -> None:
        """Block until our timestamp can be appended to the rolling
        window without exceeding max_per_minute."""
        if self.max_per_minute <= 0:
            return  # disabled
        while True:
            with self._lock:
                now = time.time()
                # Drop entries older than 60s
                while self._history and now - self._history[0] >= 60.0:
                    self._history.popleft()
                if len(self._history) < self.max_per_minute:
                    self._history.append(now)
                    return
                # +0.1s slack so we don't wake up exactly at the boundary
                # and find the entry hasn't expired yet
                wait_for = 60.0 - (now - self._history[0]) + 0.1
            logger.info(
                f"[{stage_name}] rate limiter: window full "
                f"({self.max_per_minute}/min), sleeping {wait_for:.1f}s"
            )
            time.sleep(wait_for)

    @contextlib.contextmanager
    def slot(self, stage_name: str = ""):
        """Acquire concurrency + RPM, hold concurrency until exit.

        Order matters: concurrency first (cheap), then RPM (may sleep
        for tens of seconds). With concurrency-first, the RPM wait
        happens with the concurrency slot held — preserving the
        max_concurrent invariant of "no more than N calls in flight".
        """
        # Read max_concurrent once per call; reconfigure() may update
        # it between calls but the semaphore object stays the same.
        cap = self.max_concurrent
        if cap > 0:
            self._concurrency_sem.acquire()
        try:
            self._wait_for_window(stage_name)
            yield
        finally:
            if cap > 0:
                self._concurrency_sem.release()


# Global shared limiter, one per process. Reconfigurable in init_api_config.
_relay_limiter = _SlidingWindowLimiter(CLAUDE_RPM_LIMIT, CLAUDE_MAX_CONCURRENT)


def _get_active_limiter() -> _SlidingWindowLimiter | None:
    """Return the rate limiter to use for this call, or None to skip it.

    Vertex backend has its own server-side quota and returns 429 we'd
    just retry into; app-level limiting there is wasted overhead. Relay
    + direct Anthropic both go through the shared sliding-window limiter.
    """
    if _api_config.get("mode") == "vertex":
        return None
    return _relay_limiter


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
#
# Thread-safety: multiple agents call _accumulate_run_tokens concurrently
# via asyncio.to_thread. Python's += is NOT atomic (read-modify-write),
# so a lock is required to prevent lost updates.
_run_totals: dict[str, dict[str, int]] = {}
_run_totals_lock = threading.Lock()

# Track completed run_ids for periodic cleanup to prevent memory leak.
_COMPLETED_RUN_IDS: set[str] = set()
_MAX_COMPLETED_RUNS_KEPT = 20


def reset_run_budget(run_id: str) -> None:
    """Zero out token counters for a run. Called by orchestrator.run()
    before any agent executes, so resumed runs don't re-count tokens
    that were already charged in the prior attempt."""
    with _run_totals_lock:
        _run_totals[run_id] = {
            "input": 0,
            "output": 0,
            "cache_read": 0,
            "cache_creation": 0,
            "cost_usd": 0.0,
            "calls": 0,
            "calls_with_cache_activity": 0,
        }
        # Remove this run from the completed set if it's being re-run
        _COMPLETED_RUN_IDS.discard(run_id)


def release_run_budget(run_id: str) -> None:
    """Mark a run as completed and garbage-collect old entries.
    Called by the orchestrator when a run finishes (success or failure)."""
    with _run_totals_lock:
        _COMPLETED_RUN_IDS.add(run_id)
        # Keep at most _MAX_COMPLETED_RUNS_KEPT finished entries
        if len(_COMPLETED_RUN_IDS) > _MAX_COMPLETED_RUNS_KEPT:
            oldest = list(_COMPLETED_RUN_IDS)[
                : len(_COMPLETED_RUN_IDS) - _MAX_COMPLETED_RUNS_KEPT
            ]
            for old_id in oldest:
                _run_totals.pop(old_id, None)
                _COMPLETED_RUN_IDS.discard(old_id)


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
    with _run_totals_lock:
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
    with _run_totals_lock:
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
    (run_totals_snapshot, this_call_cost_usd) so the caller can write the
    per-stage cost to stage_logs without recomputing.

    Thread-safe: acquires _run_totals_lock to prevent lost-update races
    when multiple agents call this concurrently via asyncio.to_thread.
    """
    this_call_cost = _estimate_call_cost_usd(
        model, input_tokens, output_tokens, cache_read, cache_creation
    )
    with _run_totals_lock:
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
        totals["cost_usd"] = float(totals.get("cost_usd", 0.0)) + this_call_cost
        snapshot = dict(totals)
    return snapshot, this_call_cost


def _check_run_budget(run_id: str) -> None:
    """Raise RunBudgetExceededError if the run has passed MAX_TOKENS_PER_RUN.
    Called right after every API response is accounted for."""
    with _run_totals_lock:
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
    with _run_totals_lock:
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
        # Validate prompt file exists at construction time so we fail fast
        # instead of crashing mid-pipeline after burning tokens on prior stages.
        if self.prompt_file:
            _prompt_path = PROMPTS_DIR / self.prompt_file
            if not _prompt_path.exists():
                raise FileNotFoundError(
                    f"Agent {self.stage_name}: prompt file not found at "
                    f"{_prompt_path}. Check pipeline/prompts/ directory."
                )

    def _get_client(self) -> anthropic.Anthropic:
        """Default client for the active relay/Vertex preset.
        Backwards-compat shim — new code should call _get_client_for_model
        which routes by model-name prefix (deepseek/gpt/claude → 不同
        backend)。"""
        return self._get_client_for_model(self.model)

    def _get_client_for_model(self, model: str) -> anthropic.Anthropic:
        """Select the right Anthropic-compatible client based on model name.

        v0.30.0 引入多 vendor 路由:
        - `deepseek-*` → DeepSeek 官方 anthropic-compat 端点(独立 base_url
          和 api_key,从 _api_config["deepseek"] 读)
        - `gpt-*` 或 `claude-*` → 走当前激活 preset 的 base_url(用户的
          tdyun 中转同时支持 Claude 和 GPT,model 字符串决定 relay 内部
          路由到哪家)
        - Vertex 模式 → 仍然走 AnthropicVertex(只有 Claude 模型)

        Vertex 不支持 deepseek/gpt:如果 vertex 模式下被要求 deepseek-*,
        会 fallback 到独立的 DeepSeek 配置(如果配了);否则报错。
        """
        if not _api_config:
            init_api_config()

        _mlow = (model or "").lower()
        _is_deepseek = _mlow.startswith("deepseek-")

        if _is_deepseek:
            ds_cfg = _api_config.get("deepseek")
            if not ds_cfg or not ds_cfg.get("api_key"):
                raise RuntimeError(
                    f"模型 {model!r} 需要 DeepSeek backend,但 "
                    "secrets.toml 里没配 DEEPSEEK_API_KEY。"
                    "去 .streamlit/secrets.toml 加上 "
                    "`DEEPSEEK_API_KEY = \"sk-...\"` 后重启。"
                )
            kwargs: dict[str, Any] = {
                "api_key": ds_cfg["api_key"],
                "base_url": ds_cfg["base_url"],
                "timeout": 900.0,
            }
            return anthropic.Anthropic(**kwargs)

        # Vertex 模式只能跑 Claude(GPT 也不行,Vertex AI Anthropic 是
        # Anthropic-only endpoint)
        if _api_config.get("mode") == "vertex":
            if _mlow.startswith("gpt-"):
                raise RuntimeError(
                    f"Vertex 模式不支持 GPT 模型 {model!r}。"
                    "需要切换到 relay preset(支持多模型路由的中转)"
                    "或在 GCP_PROJECT_ID 之外另配 ANTHROPIC_API_KEY。"
                )
            from anthropic import AnthropicVertex
            vertex_kwargs: dict[str, Any] = {
                "project_id": _api_config["project_id"],
                "region": _api_config["region"],
                "timeout": 900.0,
            }
            if "credentials" in _api_config:
                vertex_kwargs["credentials"] = _api_config["credentials"]
            return AnthropicVertex(**vertex_kwargs)

        # Direct relay/Anthropic 模式 — Claude 和 GPT 都走这里
        # (用户的 tdyun 中转 model 字符串路由到 Anthropic 或 OpenAI)
        kwargs = {"api_key": _api_config["api_key"]}
        if _api_config.get("base_url"):
            kwargs["base_url"] = _api_config["base_url"]
        kwargs["timeout"] = 900.0
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

    def _call_claude(
        self, system_prompt: str, user_message: str
    ) -> tuple[str, int, int, int, int, bool]:
        """Synchronous Claude API call.

        Returns (response_text, input_tokens, output_tokens,
        cache_read_input_tokens, cache_creation_input_tokens,
        thinking_fired). thinking_fired is True iff the model's response
        actually contained a `thinking` content block. Compare against
        self._use_thinking: if we asked for thinking but didn't get it,
        the relay silently dropped the `thinking` JSON param — visible
        as "thinking ✗" in the UI's model_used field.

        Three execution paths share the SAME kwargs construction (model,
        max_tokens, system, messages, thinking) — the historical "relay
        doesn't support `thinking`" workaround is no longer needed.
        Modern relays accept the standard Anthropic JSON `thinking`
        parameter, and so do native Anthropic + Vertex.

        The only path-specific bit is the Vertex Opus-4.6 adaptive-mode
        nicety: we ask for `{"type": "adaptive"}` so Vertex picks the
        thinking budget itself; everywhere else we pass an explicit
        `{"type": "enabled", "budget_tokens": N}` which is the universal
        format.
        """
        is_vertex = _api_config.get("mode") == "vertex"

        # Build content (may include image blocks)
        user_content = self._build_content_blocks(user_message)
        messages = [{"role": "user", "content": user_content}]

        # Structured system prompt with ephemeral cache control. Cache
        # support is a per-preset flag: new_relay might support it,
        # old_relay might silently drop the cache_control field and
        # just pay full price. The active preset's supports_cache flag
        # (set in init_api_config) overrides the global
        # ENABLE_PROMPT_CACHING constant, so switching relays via the
        # settings page immediately adjusts this behavior.
        _preset_supports_cache = _api_config.get(
            "supports_cache", ENABLE_PROMPT_CACHING
        )
        if _preset_supports_cache:
            system_block: Any = [
                {
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
        else:
            system_block = system_prompt

        # Resolve effective model. Per-preset model_overrides take
        # precedence over the global config.py MODELS — required for
        # relays like tdyun.ai that signal thinking tier via model name
        # suffix (claude-opus-4-6-thinking / -high / -medium / etc).
        # Stage names not in the override map fall back to self.model
        # (which was set from MODELS at agent construction time).
        _model_overrides = _api_config.get("model_overrides") or {}
        effective_model = _model_overrides.get(self.stage_name) or self.model

        # v0.30.0: 按模型族选 backend(Claude/GPT 走 default relay,DeepSeek
        # 走独立官方 anthropic-compat 端点)。client 必须按 effective_model
        # 来挑,不能用全局 _get_client。
        client = self._get_client_for_model(effective_model)

        # GPT 走 anthropic-compat relay 时,thinking JSON 参数会被 relay
        # 转发到 OpenAI 接口,OpenAI 不认这个字段会 400。所以 GPT 系列
        # 强制不发 thinking,即使 stage 在 THINKING_STAGES 里。DeepSeek
        # 的 anthropic-compat 端点理论上支持 thinking,保持发送。
        _model_low = (effective_model or "").lower()
        _is_gpt_family = _model_low.startswith("gpt-")

        kwargs: dict[str, Any] = {
            "model": effective_model,
            "max_tokens": self.max_tokens,
            "system": system_block,
            "messages": messages,
        }

        # Thinking control via standard Anthropic JSON `thinking` field.
        # Works on Anthropic native, modern relays, and Vertex.
        #
        # Opus 4.6+ → adaptive (server picks budget). Per Anthropic's own
        # deprecation notice: `thinking.type=enabled` with a fixed
        # budget_tokens is deprecated for 4.6-class models and gives
        # measurably worse results than adaptive in their testing. See
        # https://platform.claude.com/docs/en/build-with-claude/adaptive-thinking
        #
        # Older models (4.1, 4.5) don't support adaptive yet, so keep
        # the explicit budget_tokens form as fallback.
        # Three thinking-control conventions across backends:
        #
        #   1. `thinking_via_model_suffix=True` (e.g. tdyun.ai):
        #      Don't send the JSON `thinking` parameter AT ALL. The
        #      relay infers thinking tier from the model name suffix
        #      (claude-opus-4-6-thinking / -high / -medium / etc),
        #      which is set via model_overrides per stage.
        #
        #   2. `supports_adaptive_thinking=True` (mux, native Anthropic,
        #      Vertex Opus 4.6): pass {"type":"adaptive"} so the model
        #      picks its own budget — Anthropic-recommended for 4.6+.
        #
        #   3. Fallback (older relays / older models): pass
        #      {"type":"enabled","budget_tokens":N} explicit form.
        _preset_uses_suffix = _api_config.get("thinking_via_model_suffix", False)
        _preset_supports_adaptive = _api_config.get(
            "supports_adaptive_thinking", True
        )
        # Adaptive thinking is an Opus-4.6+ API feature(自 Opus 4.6 起支持,
        # Opus 4.7 继承)。Match canonical model family prefixes exactly, so
        # future names like "claude-opus-5-0" don't accidentally match and
        # we don't cross-match unrelated model names containing those digits.
        # Sonnet 3.7 不支持 adaptive thinking,会走到 else 分支发 budget_tokens。
        _is_adaptive_thinking_family = (
            "claude-opus-4-6" in effective_model
            or "claude-opus-4-7" in effective_model
            or "claude-sonnet-4-6" in effective_model
        )
        # v0.30.0: GPT 模型即使 stage 在 THINKING_STAGES 里也强制不发
        # thinking 参数(relay 转给 OpenAI 接口会 400)。代码上层把
        # _use_thinking 留着用于日志标签的"我们打算 think 但 GPT 不接",
        # 但 kwargs 里啥都不加。
        if self._use_thinking and _is_gpt_family:
            pass  # 跳过 thinking 注入,后面 thinking_fired 会自然记录 ✗
        elif self._use_thinking and not _preset_uses_suffix:
            if _is_adaptive_thinking_family and _preset_supports_adaptive:
                kwargs["thinking"] = {"type": "adaptive"}
            else:
                kwargs["thinking"] = {
                    "type": "enabled",
                    "budget_tokens": THINKING_BUDGET_TOKENS,
                }
        elif self._use_thinking and _preset_uses_suffix:
            # Suffix-mode sanity check: the relay is supposed to infer
            # thinking tier from the model NAME, so the effective model for
            # a thinking stage must carry one of the known suffixes. If the
            # operator forgot to set a model_override for this stage, we'd
            # silently run it WITHOUT thinking on a non-thinking base model.
            # Log once per stage so the mis-config surfaces instead of
            # mysteriously-worse outputs.
            if not any(
                s in effective_model
                for s in ("-thinking", "-high", "-medium", "-low")
            ):
                logger.warning(
                    "[%s] thinking_via_model_suffix=True but effective model "
                    "%r has no known thinking suffix — this stage will likely "
                    "run without thinking. Check model_overrides in secrets.",
                    self.stage_name,
                    effective_model,
                )

        # Apply rate limiting. Vertex returns None (server-side quota);
        # relay/direct gets the sliding-window limiter so we respect both
        # CLAUDE_RPM_LIMIT (sustained) and CLAUDE_MAX_CONCURRENT (peak).
        # The limiter is held for the full call duration to enforce the
        # concurrency cap correctly.
        #
        # Cache-control safety net: some relays silently ignore the
        # cache_control field, others 400 on it. We default to sending
        # it ("万一命中了呢"), but if the backend rejects it specifically
        # with a 400 containing "cache_control", auto-disable caching
        # for this process and retry ONCE with plain-string system.
        # This means a new relay-switch doesn't require a config commit
        # just to find out whether caching works.
        limiter = _get_active_limiter()

        def _do_stream(call_kwargs: dict[str, Any]):
            if limiter is not None:
                with limiter.slot(self.stage_name):
                    with client.messages.stream(**call_kwargs) as stream:
                        return stream.get_final_message()
            with client.messages.stream(**call_kwargs) as stream:
                return stream.get_final_message()

        try:
            response = _do_stream(kwargs)
        except anthropic.BadRequestError as e:
            err_text = str(e)
            # Only treat as cache fault if we actually sent cache_control
            # AND the error message specifically mentions it. Avoid false
            # positives from unrelated 400s that happen to contain the word.
            is_cache_fault = (
                _preset_supports_cache
                and "cache_control" in err_text.lower()
            )
            if is_cache_fault:
                _active_preset = get_active_claude_relay_name() or "_global"
                _cache_key = f"_cache_disabled_{_active_preset}"
                # Check-and-set under a lock so concurrent agents all see the
                # same decision. First thread that gets the lock flips the
                # bit; later threads see it set and skip the log spam but
                # still fall through to the cache-less retry with this
                # thread's own kwargs.
                with _api_config_lock:
                    _first_to_disable = not _api_config.get(_cache_key)
                    _api_config[_cache_key] = True
                    _api_config["supports_cache"] = False
                if _first_to_disable:
                    logger.warning(
                        "[cache-fallback] backend rejected cache_control with 400, "
                        "auto-disabling caching for preset '%s' and retrying. "
                        "Error: %s",
                        _active_preset,
                        err_text[:300],
                    )
                kwargs["system"] = system_prompt  # plain string, no cache
                response = _do_stream(kwargs)
            else:
                raise

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
        return (
            text,
            input_tokens,
            output_tokens,
            cache_read,
            cache_creation,
            has_thinking,
        )

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

        # v0.29.12: 老错误只报 last 200 chars,debug 时还得翻 stage_log
        # 才能看全貌。把首 200 + 末 200 都塞进去,并报告中间遗漏了多少,
        # 让运行时日志就能判断是"整段不是 JSON"还是"尾部截断"。
        _preview_front = response_text[:200]
        _preview_tail = response_text[-200:] if len(response_text) > 200 else ""
        _gap = max(0, len(response_text) - 400)
        raise ValueError(
            f"Could not extract JSON from response (len={len(response_text)}); "
            f"first 200 chars: {_preview_front!r}; "
            f"...[{_gap} chars omitted]... "
            f"last 200 chars: {_preview_tail!r}"
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

        # Try cut points from most recent to earliest (keep as much data as possible).
        # v0.29.12: 老版本 `cut_points[-300:]` 硬限在 13K+ 响应里常常不够
        # ——最后 300 个 cut point 可能都深埋在一个没闭合的嵌套结构里,
        # 每个 candidate 都不合法。放开限制,扫所有 cut point。即使 5000
        # 个 candidate 每个 re-scan 也就 ~50ms,总共 <1s,比整条流水线
        # 报错重跑强得多。
        for cp in reversed(cut_points):
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
                    thinking_fired,
                ) = await asyncio.to_thread(
                    self._call_claude, system_prompt, user_message
                )
                total_input_tokens += input_tokens
                total_output_tokens += output_tokens

                # Build a human-readable model label that reveals thinking
                # actually fired (or didn't) so the UI doesn't need to dig
                # into server logs to answer "did this stage reason deeply?".
                #  - `[thinking ✓]` — we asked for thinking AND the model
                #    returned a thinking block
                #  - `[thinking ✗]` — we asked for thinking but got none;
                #    relay likely dropped the `thinking` JSON param
                #  - no tag          — this stage doesn't use thinking
                if self._use_thinking:
                    _thinking_tag = " [thinking ✓]" if thinking_fired else " [thinking ✗]"
                else:
                    _thinking_tag = ""
                # Use the EFFECTIVE model name (which may be a per-preset
                # override like "claude-opus-4-6-high") so the UI shows
                # what the relay actually saw, not the global default.
                _model_overrides_for_label = _api_config.get("model_overrides") or {}
                _effective_model_for_label = (
                    _model_overrides_for_label.get(self.stage_name) or self.model
                )
                model_label = f"{_effective_model_for_label}{_thinking_tag}"

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
                        model_used=model_label,
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
                    model_used=model_label,
                    tokens_used=total_input_tokens + total_output_tokens,
                    duration_seconds=round(duration, 2),
                )
                # Push the run-level cost + token totals to pipeline_runs so
                # the UI can display current spend without needing access to
                # the in-process _run_totals dict. Swallow DB errors — cost
                # tracking is observability, not a hard invariant. One small
                # write per agent completion, at most ~14/run.
                try:
                    _run_total = get_run_totals(run_id)
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
                logger.exception(
                    f"[{self.stage_name}] attempt {attempt + 1}/{MAX_RETRIES + 1} failed"
                )
                # Classify error: only retry on transient failures (rate
                # limits, server errors, timeouts). Non-retryable errors
                # (auth failures, malformed requests) waste tokens and time.
                _retryable = True
                if isinstance(e, anthropic.AuthenticationError):
                    _retryable = False
                elif isinstance(e, anthropic.PermissionDeniedError):
                    _retryable = False
                elif isinstance(e, anthropic.BadRequestError) and "cache_control" not in str(e).lower():
                    # Bad request that isn't a cache fault is not retryable
                    _retryable = False
                elif isinstance(e, (ValueError, json.JSONDecodeError)):
                    # JSON parse failures from _extract_json — retrying with
                    # the same input rarely helps, but one retry can recover
                    # from transient model weirdness.
                    _retryable = attempt < 1  # allow at most 1 retry for parse errors

                # v0.28.2: 对上游 5xx(relay 或 Anthropic internal)用更长的
                # 退避窗口 —— 抖动通常需要 10-30s 恢复,3s*2^attempt 太激进,
                # 会把重试烧在还没恢复的上游。5xx 用 base=10s,其他错误仍用
                # RETRY_BASE_DELAY_SECONDS(3s)。
                # 注意:v0.28.1 的模型自动降级已移除——用户明确选择"全部用
                # Opus 4.7,失败就重跑",避免降级在 multi-batch 场景下悄悄
                # 污染后续 batch。
                _is_5xx = isinstance(
                    e,
                    (anthropic.InternalServerError, anthropic.APIConnectionError),
                )

                if _retryable and attempt < MAX_RETRIES:
                    _base = 10 if _is_5xx else RETRY_BASE_DELAY_SECONDS
                    delay = _base * (2 ** attempt)
                    await asyncio.sleep(delay)
                elif not _retryable:
                    logger.warning(
                        f"[{self.stage_name}] non-retryable error ({type(e).__name__}), "
                        f"skipping remaining retries"
                    )
                    break

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
