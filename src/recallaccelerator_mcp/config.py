"""Configuration for the RecallAccelerator MCP server.

Reads from the same user-level config file used by the .NET API:
  %APPDATA%\\RecallAccelerator\\config.json   (Windows)
  ~/.config/RecallAccelerator/config.json     (Linux/macOS fallback)

Override the path with RECALLACCELERATOR_CONFIG_PATH.
Override individual values with environment variables (see below).
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

DEFAULT_API_URL = "http://localhost:5050"
DEFAULT_SCHEMA = "dbo"


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


def _dotnet_to_odbc(dotnet_conn: str) -> str:
    """Translate a .NET-style SQL Server connection string into an ODBC one.

    Input:  Server=NIKXDOG\\NIKXDOG;Database=RecallAccelerator;User Id=localAdmin;Password=localAdmin;TrustServerCertificate=true;
    Output: DRIVER={ODBC Driver 17 for SQL Server};SERVER=NIKXDOG\\NIKXDOG;DATABASE=RecallAccelerator;UID=localAdmin;PWD=localAdmin;TrustServerCertificate=yes;
    """
    pairs: dict[str, str] = {}
    for chunk in dotnet_conn.split(";"):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        k, v = chunk.split("=", 1)
        pairs[k.strip().lower()] = v.strip()

    server = pairs.get("server") or pairs.get("data source") or ""
    database = pairs.get("database") or pairs.get("initial catalog") or ""
    user = pairs.get("user id") or pairs.get("uid") or ""
    password = pairs.get("password") or pairs.get("pwd") or ""
    trust = pairs.get("trustservercertificate", "false")
    trusted = pairs.get("integrated security") or pairs.get("trusted_connection")

    parts = ["DRIVER={ODBC Driver 17 for SQL Server}"]
    if server:
        parts.append(f"SERVER={server}")
    if database:
        parts.append(f"DATABASE={database}")
    if trusted and trusted.lower() in ("true", "sspi", "yes"):
        parts.append("Trusted_Connection=yes")
    else:
        if user:
            parts.append(f"UID={user}")
        if password:
            parts.append(f"PWD={password}")
    if trust.lower() in ("true", "yes"):
        parts.append("TrustServerCertificate=yes")
    return ";".join(parts) + ";"


def _resolve_db_connection() -> str:
    # 1. Explicit ODBC env var wins
    env_odbc = os.environ.get("RECALLACCELERATOR_ODBC")
    if env_odbc:
        return env_odbc

    # 2. User config: Custom.RecallAcceleratorOdbcString (already ODBC-formatted)
    custom = _USER_CONFIG.get("Custom") or {}
    if isinstance(custom, dict):
        odbc = custom.get("RecallAcceleratorOdbcString")
        if odbc:
            return odbc

    # 3. User config: ConnectionStrings.RecallAcceleratorDb (.NET style, translate)
    conn_strings = _USER_CONFIG.get("ConnectionStrings") or {}
    if isinstance(conn_strings, dict):
        dotnet = conn_strings.get("RecallAcceleratorDb")
        if dotnet:
            return _dotnet_to_odbc(dotnet)

    # 4. Fallback: localhost trusted
    return (
        "DRIVER={ODBC Driver 17 for SQL Server};"
        "SERVER=localhost;"
        "DATABASE=RecallAccelerator;"
        "Trusted_Connection=yes;"
    )


def _resolve_api_url() -> str:
    env = os.environ.get("RECALLACCELERATOR_API_URL")
    if env:
        return env
    custom = _USER_CONFIG.get("Custom") or {}
    if isinstance(custom, dict) and custom.get("RecallAcceleratorApiUrl"):
        return custom["RecallAcceleratorApiUrl"]
    return DEFAULT_API_URL


def _resolve_schema() -> str:
    return os.environ.get("RECALLACCELERATOR_SCHEMA", DEFAULT_SCHEMA)


DB_CONNECTION_STRING = _resolve_db_connection()
API_BASE_URL = _resolve_api_url()
DB_SCHEMA = _resolve_schema()
CONFIG_PATH = str(_resolve_config_path())


def tbl(name: str) -> str:
    """Return a schema-qualified table name (e.g. dbo.Projects)."""
    return f"{DB_SCHEMA}.{name}"
