# RecallAccelerator MCP

MCP (Model Context Protocol) server that lets Claude Code, Codex, Cursor, and any other MCP-aware coding tool talk to a [RecallAccelerator](https://github.com/ericboen/RecallAccelerator) instance — the project memory + task tracker that gives your AI agents shared, durable working memory across sessions and tools.

This package is the thin client layer. The server logic lives in the main RecallAccelerator repo; this MCP just speaks HTTPS to its `/api/agent/*` and `/api/projects/*` endpoints so you don't need a SQL driver, ODBC, or any DB credentials on your dev box.

## Install

You need Python 3.11+ and a RecallAccelerator instance you can reach over HTTPS (run your own — see the [main repo](https://github.com/ericboen/RecallAccelerator) — or use someone else's).

```powershell
git clone https://github.com/ericboen/Recall_Accelerator_MCP.git
cd Recall_Accelerator_MCP
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
```

That installs `recallaccelerator-mcp.exe` at `.venv\Scripts\` — the launcher each AI tool spawns.

## Configure

Three required env vars:

| Var | What | Example |
|-----|------|---------|
| `RECALLACCELERATOR_API_URL` | Your instance's HTTPS base URL | `https://recallaccelerator.example.com` |
| `RECALLACCELERATOR_API_KEY` | Generate at `/ApiKeys` on your instance | `ra_AbC1234...` |
| `RECALLACCELERATOR_AGENT_NAME` | Who's calling — usually your name | `Eric` |

Optional:

| Var | Default | Notes |
|-----|---------|-------|
| `RECALLACCELERATOR_TOOL_NAME` | the launcher infers | `claude-code` / `codex` / `cursor` — set explicitly if running multiple tools |
| `RECALLACCELERATOR_CLAIMER_KIND` | `ai` | `ai` or `human` (humans get a 7-day claim lease vs 30 min for AI) |

There's also a fallback config file at `%APPDATA%\RecallAccelerator\config.json` that the launcher reads if env vars aren't set, but for normal use the env-var-per-tool pattern (see below) is cleaner.

## Wire it into your AI tool

### Claude Code

```powershell
claude mcp add recallaccelerator --scope user `
  --env RECALLACCELERATOR_TOOL_NAME=claude-code `
  --env RECALLACCELERATOR_API_KEY=ra_paste_your_key_here `
  --env RECALLACCELERATOR_API_URL=https://your-instance.example.com `
  --env RECALLACCELERATOR_AGENT_NAME="Your Name" `
  -- C:\path\to\Recall_Accelerator_MCP\.venv\Scripts\recallaccelerator-mcp.exe
```

### Codex (TOML config)

```toml
[mcp_servers.recallaccelerator]
command = "C:\\path\\to\\Recall_Accelerator_MCP\\.venv\\Scripts\\recallaccelerator-mcp.exe"
env = {
  RECALLACCELERATOR_TOOL_NAME = "codex",
  RECALLACCELERATOR_API_KEY = "ra_paste_your_key_here",
  RECALLACCELERATOR_API_URL = "https://your-instance.example.com",
  RECALLACCELERATOR_AGENT_NAME = "Your Name"
}
```

Codex's MCP launcher uses a clean environment, so you must pass every variable explicitly.

### Cursor / other AGENTS.md-aware tools

Most read MCP config from `mcpServers` in their workspace settings. Same shape as Claude Desktop — point `command` at the launcher, set the env vars.

### Claude Desktop

```json
{
  "mcpServers": {
    "recallaccelerator": {
      "command": "C:\\path\\to\\Recall_Accelerator_MCP\\.venv\\Scripts\\recallaccelerator-mcp.exe",
      "env": {
        "RECALLACCELERATOR_TOOL_NAME": "claude-desktop",
        "RECALLACCELERATOR_API_KEY": "ra_paste_your_key_here",
        "RECALLACCELERATOR_API_URL": "https://your-instance.example.com",
        "RECALLACCELERATOR_AGENT_NAME": "Your Name"
      }
    }
  }
}
```

## Tools exposed

All operations go through HTTPS to the RA instance — no DB driver needed.

**Discovery / read:**
- `list_projects` — all projects in your workspace
- `list_ready_tasks(project_id)` — claimable tasks ordered by priority
- `get_project_brief(project_id)` — current brief markdown
- `get_project_lineage(project_id)` — parent/child relations
- `get_project_activity(project_id)` — recent project events
- `get_task_context(task_id, agent_session_id)` — full agent context for a claimed task
- `get_project_agent_files(project_id, tool=None)` — fetch ready-to-drop CLAUDE.md/AGENTS.md/.cursorrules for an existing project

**Claim lifecycle:**
- `claim_next_task(project_id)` — atomic claim of the highest-priority ready task
- `claim_specific_task(task_id)` — claim by id (skip the queue)
- `complete_task(task_id, agent_session_id, completion_summary, ...)` — finish + handoff + follow-ups
- `release_task(task_id, agent_session_id, reason)` — drop a claim
- `heartbeat_session(session_id)` — extend the lease (returns time-until-expiry)
- `end_session(session_id, summary)` — close out a session

**Writebacks during work:**
- `add_context_note(project_id, agent_session_id, content, note_type)` — record a constraint, warning, or open question
- `add_decision(project_id, agent_session_id, title, decision_text, ...)` — record an architectural choice
- `propose_task(project_id, title, context, suggested_priority)` — surface follow-up work without polluting the ready queue (humans triage at `/Triage`)
- `propose_brief_update(project_id, agent_session_id, base_brief_version_id, proposed_markdown, ...)` — propose a brief change (humans approve at `/BriefProposals`)

**Bootstrap (cold-start a new project from agent context):**
- `create_workspace(name, description)`
- `create_project(workspace_id, name, slug, ...)` — response embeds ready-to-drop per-tool agent files
- `create_brief_version(project_id, brief_markdown, change_summary)`
- `create_task(project_id, title, description, priority, kind, ...)`

**Releases:**
- `list_releases(project_id, status)`
- `get_release(release_id)`
- `get_release_changelog(release_id)`
- `create_release(project_id, version, name, release_notes, target_environment)`
- `attach_to_release(release_id, item_type, target_id, notes)`
- `start_release(release_id, git_ref)`
- `complete_release(release_id, released_by, git_ref, deployment_artifact, skip_brief_proposal)`
- `rollback_release(release_id, reason)`

**Diagnostic:**
- `server_info` — API URL, identity defaults, whether an API key is configured

## Quick start (typical day-to-day workflow)

Once installed and wired, in any repo folder ask your AI tool:

> "Use the recallaccelerator MCP and start the session ritual."

The agent will call `list_projects` and find this repo's project (by slug = lowercased folder name, by convention), `get_project_brief`, `list_ready_tasks`, `claim_next_task`, do the work, then `complete_task` with a real handoff. The next session — same agent, different agent, different tool, different machine — picks up with the full audit trail.

If you're starting a new repo without an RA project yet, ask:

> "Create a new RA project for this repo."

The MCP `create_project` response embeds three pre-filled per-tool files (CLAUDE.md / AGENTS.md / .cursorrules) — drop the one matching your tool at the repo root and the next agent in this folder will know where to find its project memory.

## Development

This is the standalone publication of the MCP code. Active development happens in the [main RecallAccelerator repo](https://github.com/ericboen/RecallAccelerator) under `mcp/recallaccelerator-mcp/`; this repo is mirrored from there.

Please file issues / PRs on the main repo unless they're specific to standalone packaging concerns.

## License

Same as the main RecallAccelerator project.
