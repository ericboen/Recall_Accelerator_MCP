"""Configuration for the RecallAccelerator MCP server.

All tools talk to the .NET API over HTTPS - no direct SQL connection. Agent
boxes only need:
  - RECALLACCELERATOR_API_URL  (e.g. https://recallaccelerator.offcamber.ai)
  - RECALLACCELERATOR_API_KEY  (when prod has Auth:RequireApiKey=true)

Optional identity defaults:
  - RECALLACCELERATOR_AGENT_NAME  (default "Agent")
  - RECALLACCELERATOR_TOOL_NAME   (default "unknown" - typically set per-tool
    in the MCP launcher's env block: "claude-code", "codex", "cursor")
  - RECALLACCELERATOR_CLAIMER_KIND (default "ai"; "human" for human-driven
    sessions through the MCP)

Optional config-file fallback for any of the above lives at
%APPDATA%\\RecallAccelerator\\config.json under top-level ConnectionStrings
or Custom.RecallAccelerator{ApiUrl,ApiKey,AgentName,ToolName,ClaimerKind}.
Override the path with RECALLACCELERATOR_CONFIG_PATH.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULT_API_URL = "http://localhost:5050"


def _resolve_config_path() -> Path:
    override = os.environ.get("RECALLACCELERATOR_CONFIG_PATH")
    if override:
        return Path(override)
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "RecallAccelerator" / "config.json"
    return Path.home() / ".config" / "RecallAccelerator" / "config.json"


def _load_user_config() -> dict:
    path = _resolve_config_path()
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


_USER_CONFIG = _load_user_config()


def _resolve_api_url() -> str:
    env = os.environ.get("RECALLACCELERATOR_API_URL")
    if env:
        return env
    custom = _USER_CONFIG.get("Custom") or {}
    if isinstance(custom, dict) and custom.get("RecallAcceleratorApiUrl"):
        return custom["RecallAcceleratorApiUrl"]
    return DEFAULT_API_URL


def _resolve_api_key() -> str | None:
    """Optional X-Api-Key header value. Required when prod has Auth:RequireApiKey=true."""
    env = os.environ.get("RECALLACCELERATOR_API_KEY")
    if env:
        return env
    custom = _USER_CONFIG.get("Custom") or {}
    if isinstance(custom, dict) and custom.get("RecallAcceleratorApiKey"):
        return custom["RecallAcceleratorApiKey"]
    return None


def _resolve_identity() -> dict:
    """Default agent identity for tool calls (claim_next_task, create_*, etc.)."""
    custom = _USER_CONFIG.get("Custom") or {}
    return {
        "agent_name": (
            os.environ.get("RECALLACCELERATOR_AGENT_NAME")
            or (custom.get("RecallAcceleratorAgentName") if isinstance(custom, dict) else None)
            or "Agent"
        ),
        "tool_name": (
            os.environ.get("RECALLACCELERATOR_TOOL_NAME")
            or (custom.get("RecallAcceleratorToolName") if isinstance(custom, dict) else None)
            or "unknown"
        ),
        "claimer_kind": (
            os.environ.get("RECALLACCELERATOR_CLAIMER_KIND")
            or (custom.get("RecallAcceleratorClaimerKind") if isinstance(custom, dict) else None)
            or "ai"
        ),
    }


API_BASE_URL = _resolve_api_url()
API_KEY = _resolve_api_key()
CONFIG_PATH = str(_resolve_config_path())
DEFAULT_IDENTITY = _resolve_identity()


def auth_headers() -> dict:
    """Build the auth header dict for HTTP calls. Empty when no API key configured."""
    return {"X-Api-Key": API_KEY} if API_KEY else {}
