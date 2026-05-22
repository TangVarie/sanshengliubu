"""Secret-masking utilities for logs, stacktraces, and user-visible output.

Problem (2026-05-22 audit R-023): exception stacktraces and `logger.exception`
calls can spill API keys, Supabase bearer tokens, or JWT payloads into the
runtime log stream. Once deployed, a single log screenshot or pipeline-to-
external-aggregator leak exposes those keys.

Usage:
    from pipeline.logger_utils import mask_secrets

    # Wrap any text that may contain secrets before showing/logging:
    st.code(mask_secrets(traceback.format_exc()))

    # Or install the formatter on the root logger to mask EVERY log line:
    install_secret_masking_on_root_logger()
"""

from __future__ import annotations

import logging
import re
from typing import Any

# Patterns covering the credential shapes we use in this codebase + adjacent
# tooling. Order doesn't matter for correctness; we run all of them.
# Each pattern matches a credential-like token; the entire match becomes
# "***REDACTED***" so we don't leak even a prefix.
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),          # Anthropic
    re.compile(r"sb_secret_[A-Za-z0-9]{20,}"),          # Supabase 2024+ service_role
    re.compile(r"sbp_[A-Za-z0-9]{20,}"),                # Supabase personal access token
    re.compile(r"eyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}"),  # JWT (anon/service)
    re.compile(r"AIza[A-Za-z0-9_\-]{20,}"),             # Google / Vertex Express
    re.compile(r"sk-proj-[A-Za-z0-9_\-]{20,}"),         # OpenAI scoped
    re.compile(r"sk-[A-Za-z0-9]{40,}"),                 # Generic OpenAI/Anthropic
)


def mask_secrets(s: Any) -> str:
    """Replace credential-like tokens in `s` with ***REDACTED***.

    Accepts any value — non-strings are passed through repr() first so a
    stray dict / Exception object doesn't crash the masking pipeline.
    Returns an empty string for None/empty input.

    Pure-string transformation; no side effects.
    """
    if s is None:
        return ""
    if not isinstance(s, str):
        try:
            s = str(s)
        except Exception:
            try:
                s = repr(s)
            except Exception:
                return "<unrenderable>"
    if not s:
        return s
    for pat in _SECRET_PATTERNS:
        s = pat.sub("***REDACTED***", s)
    return s


class SecretMaskingFormatter(logging.Formatter):
    """Logging formatter that masks credential-looking tokens in EVERY
    message. Wraps an inner formatter so existing format strings still
    apply; just the final rendered message goes through mask_secrets().

    Install with `install_secret_masking_on_root_logger()` once at app
    startup. After that, anything written via `logger.info / warning /
    exception` is sanitized — including chained tracebacks via
    `exc_info`.
    """

    def format(self, record: logging.LogRecord) -> str:
        msg = super().format(record)
        return mask_secrets(msg)


def install_secret_masking_on_root_logger() -> None:
    """Idempotent: wrap each existing root-logger handler's formatter
    with SecretMaskingFormatter. Safe to call multiple times — we tag
    the formatter so a second call is a no-op.

    Why wrap instead of replace: the root logger may already have a
    streamlit-supplied formatter (with custom datefmt etc). Replacing
    would lose that config.
    """
    root = logging.getLogger()
    for handler in root.handlers:
        fmt = handler.formatter
        if isinstance(fmt, SecretMaskingFormatter):
            continue  # already wrapped on a prior call
        # Inherit the existing format string + datefmt so we don't lose
        # whatever streamlit/the operator configured.
        if fmt is not None:
            new_fmt = SecretMaskingFormatter(
                fmt._fmt if hasattr(fmt, "_fmt") else None,
                datefmt=fmt.datefmt,
            )
        else:
            new_fmt = SecretMaskingFormatter("%(asctime)s [%(levelname)s] %(name)s · %(message)s")
        handler.setFormatter(new_fmt)
