# RecallAccelerator MCP

MCP server that exposes RecallAccelerator's project memory and task tracker
to Claude Code, Cursor, Codex, and any other MCP-aware coding tool.

## Tools

**Read (direct SQL Server):**

- `list_projects` - all projects, id, slug, status, current brief version
- `list_ready_tasks(project_id)` - claimable tasks ordered by priority
- `get_project_brief(project_id)` - current brief markdown
- `get_project_lineage(project_id)` - lineage edges (child_of, branched_from, supports, etc.)
- `get_project_activity(project_id)` - recent project events

**Write (delegates to the .NET API at `/api/agent/*`):**

- `claim_next_task(project_id, agent_name, tool_name)` - atomic claim
- `get_task_context(task_id, agent_session_id)` - full agent context
- `complete_task(task_id, agent_session_id, completion_summary, ...)` - finish + handoff + follow-ups
- `release_task(task_id, agent_session_id, reason)` - drop a claim
- `heartbeat_session(session_id)` - extend the lease
- `end_session(session_id, summary)` - close out a session
- `add_context_note(project_id, agent_session_id, content, note_type)` - record a note
- `add_decision(project_id, agent_session_id, title, decision_text, ...)` - record a decision
- `propose_brief_update(project_id, agent_session_id, base_brief_version_id, proposed_markdown, ...)` - propose a brief change

**Diagnostic:**

- `server_info` - returns the API URL, config path, and DB host the MCP is using

## Installing

```powershell
cd mcp\recallaccelerator-mcp
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
```

Make sure the ODBC Driver 17 (or 18) for SQL Server is installed on your machine — `pyodbc` needs it.

## Configuration

The MCP reads from the same user config file as the .NET API:

`%APPDATA%\RecallAccelerator\config.json`

Override the path with the `RECALLACCELERATOR_CONFIG_PATH` env var.

It pulls:

- **DB connection** from `ConnectionStrings.RecallAcceleratorDb` (translates the .NET-style string to ODBC). You can also set `Custom.RecallAcceleratorOdbcString` to provide an ODBC string directly, or the `RECALLACCELERATOR_ODBC` env var.
- **API base URL** from `Custom.RecallAcceleratorApiUrl` (defaults to `http://localhost:5050`). Override with `RECALLACCELERATOR_API_URL`.
- **DB schema** defaults to `dbo`. Override with `RECALLACCELERATOR_SCHEMA` (e.g. `cxctestc_recallaccelerator` for ASPNIX).

## Wiring into Claude Desktop

Add to `%APPDATA%\Claude\claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "recallaccelerator": {
      "command": "C:\\Users\\ericb\\source\\repos\\recallaccelerator\\mcp\\recallaccelerator-mcp\\.venv\\Scripts\\recallaccelerator-mcp.exe"
    }
  }
}
```

(Replace the path with wherever your venv lives.)

## Wiring into Claude Code

```bash
claude mcp add recallaccelerator -- C:\Users\ericb\source\repos\recallaccelerator\mcp\recallaccelerator-mcp\.venv\Scripts\recallaccelerator-mcp.exe
```

## Architecture note

Reads go straight to SQL Server because the queries are simple and we don't
want a network hop for every `list_projects` call. Writes always go through
the .NET API so business logic (atomic claim, event logging, brief
versioning) stays in one place. The MCP is a thin layer.
