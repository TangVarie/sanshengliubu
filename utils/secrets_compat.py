"""st.secrets → 环境变量兼容层。

背景(Railway 迁移):执行层从 Streamlit 进程剥离成独立 worker 之后,
worker 进程里没有 .streamlit/secrets.toml —— Railway 的配置全部走环境
变量。而 db/supabase_client 与 pipeline/agents 此前直接读 st.secrets,
在无 secrets.toml 的进程里一律抛 FileNotFoundError。

本模块提供统一的读取口径:
  1. 先读 st.secrets(本地开发 / Streamlit Cloud 的既有路径,行为不变);
  2. 读不到(无 streamlit / 无 secrets.toml / 键不存在)则回退到环境变量;
  3. 环境变量值以 '{' 或 '[' 开头时按 JSON 解析(承接 secrets.toml 里的
     table 型配置,如 gcp_service_account / claude_relay_presets)。

UI 专属代码(pages/)可以继续直接用 st.secrets;凡是 worker 也要跑到的
共享模块(db/、pipeline/),一律经由这里。
"""

from __future__ import annotations

import json
import os
from typing import Any

_MISSING = object()


def _from_streamlit(key: str) -> Any:
    """从 st.secrets 读一个键;任何一层不可用都返回 _MISSING。"""
    try:
        import streamlit as st  # noqa: PLC0415 — 故意延迟导入
    except Exception:
        return _MISSING
    try:
        # 无 secrets.toml 时,访问 st.secrets 的任何操作都会抛
        # (StreamlitSecretNotFoundError / FileNotFoundError,随版本变),
        # 这里统一按"读不到"处理,落到环境变量分支。
        if key in st.secrets:
            return st.secrets[key]
    except Exception:
        return _MISSING
    return _MISSING


def get_secret(key: str, default: Any = None) -> Any:
    """读配置:st.secrets 优先,环境变量兜底,都没有返回 default。"""
    val = _from_streamlit(key)
    if val is not _MISSING:
        return val
    env = os.environ.get(key)
    if env is None:
        return default
    stripped = env.strip()
    if stripped[:1] in ("{", "["):
        try:
            return json.loads(stripped)
        except Exception:
            return env
    return env


def has_secret(key: str) -> bool:
    """键是否存在于 st.secrets 或环境变量(值为空串也算存在)。"""
    if _from_streamlit(key) is not _MISSING:
        return True
    return key in os.environ
