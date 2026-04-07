"""Shared version badge helper. Call show_version_badge() at the top of each page."""

import streamlit as st

from pipeline.config import VERSION, VERSION_DATE, VERSION_NOTES


def show_version_badge() -> None:
    """Render the build version in the sidebar so a restart can be visually verified."""
    st.sidebar.caption(f"🏷️ **{VERSION}** · {VERSION_DATE}")
    st.sidebar.caption(VERSION_NOTES)
