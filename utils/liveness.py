"""Startup reaper for zombie 'running' runs.

The pipeline executes in a daemon thread inside the Streamlit process. When
Streamlit Cloud recycles/sleeps the process (SIGKILL), that thread dies
mid-run and the DB row stays 'running' forever. Nothing else cleans it up,
so the UI shows a permanent spinner. Calling maybe_reap_stale_runs() on page
load collects those zombies: any 'running' run whose heartbeat_at is stale
(process is dead) gets marked failed with an explanation.

Throttled via session_state so it costs nothing on the 3-second auto-refresh
reruns. Never raises — page load must not depend on it.
"""

from __future__ import annotations

import time

import streamlit as st

from pipeline.config import RUN_STALE_SECONDS

_THROTTLE_SECONDS = 30


def maybe_reap_stale_runs() -> int:
    """Reap zombie runs at most once per _THROTTLE_SECONDS per session.
    Returns the number reaped (0 if throttled / unconfigured / error)."""
    try:
        now = time.time()
        last = float(st.session_state.get("_last_reap_ts", 0.0))
        if now - last < _THROTTLE_SECONDS:
            return 0
        st.session_state["_last_reap_ts"] = now
        from db.supabase_client import SupabaseClient

        return SupabaseClient.get_instance().reap_stale_runs(RUN_STALE_SECONDS)
    except Exception:
        # Supabase unconfigured / transient error — never break the page.
        return 0
