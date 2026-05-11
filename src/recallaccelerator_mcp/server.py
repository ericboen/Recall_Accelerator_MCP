"""RecallAccelerator MCP server.

Exposes the RecallAccelerator project-memory and task-tracker over MCP so
Claude Code, Cursor, Codex, and other AI coding agents can claim work,
read project context, write back progress, and propose brief updates.

All tools talk to the .NET API over HTTPS (single source of truth, single
auth path). Agent boxes need only an API key + URL - no SQL credentials,
no ODBC driver install. Set RECALLACCELERATOR_API_URL +
RECALLACCELERATOR_API_KEY in the per-tool MCP config block.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from fastmcp import FastMCP

from .config import (
    API_BASE_URL,
    API_KEY,
    CONFIG_PATH,
    DEFAULT_IDENTITY,
    auth_headers,
)

mcp = FastMCP("RecallAccelerator")


def _api(method: str, path: str, body: dict | None = None) -> Any:
    url = f"{API_BASE_URL}{path}"
    headers = auth_headers()
    with httpx.Client(timeout=60.0, headers=headers) as client:
        if method == "GET":
            resp = client.get(url)
        elif method == "POST":
            resp = client.post(url, json=body or {})
        elif method == "PUT":
            resp = client.put(url, json=body or {})
        elif method == "PATCH":
            resp = client.patch(url, json=body or {})
        elif method == "DELETE":
            resp = client.delete(url)
        else:
            raise ValueError(f"Unsupported method: {method}")
        resp.raise_for_status()
        if not resp.content:
            return {}
        try:
            return resp.json()
        except json.JSONDecodeError:
            return {"raw": resp.text}


# ---- Read tools (delegate to API) -----------------------------------------


@mcp.tool()
def list_projects() -> str:
    """List all projects with id, slug, status, and current brief version."""
    rows = _api("GET", "/api/projects")
    if not rows:
        return "No projects."
    lines = [f"Found {len(rows)} project(s):\n"]
    for r in rows:
        brief = f" v{r['currentBriefVersionId']}" if r.get("currentBriefVersionId") else ""
        desc = r.get("compactDescription") or "(no description)"
        lines.append(
            f"  #{r['id']:>3} {r['slug']:30} [{r['status']:8}]  {r['name']}{brief}\n"
            f"        {desc}"
        )
    return "\n".join(lines)


@mcp.tool()
def list_ready_tasks(project_id: int, limit: int = 20) -> str:
    """List ready (claimable) tasks for a project, ordered by priority desc."""
    rows = _api("GET", f"/api/projects/{project_id}/tasks?status=ready")
    if not rows:
        return f"No ready tasks for project {project_id}."
    rows = rows[:limit]
    lines = [f"{len(rows)} ready task(s) for project {project_id}:\n"]
    for r in rows:
        # The list endpoint returns TaskSummaryDto which doesn't include Description.
        # That's OK for a quick "what's available" view; full detail comes via get_task_context after claim.
        lines.append(f"  #{r['id']:>4} [pri {r['priority']:>3}]  {r['title']}")
    return "\n".join(lines)


@mcp.tool()
def get_project_brief(project_id: int) -> str:
    """Return the current project brief markdown, or a message if none is set."""
    try:
        brief = _api("GET", f"/api/projects/{project_id}/brief")
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 204:
            return f"No current brief for project {project_id}."
        if exc.response.status_code == 404:
            return f"Project {project_id} not found."
        raise
    if not brief or not brief.get("briefMarkdown"):
        return f"No current brief for project {project_id}."
    return (
        f"# Brief v{brief.get('versionNumber')} - by {brief.get('createdBy')} "
        f"({brief.get('createdByType')}) on {brief.get('createdAt')}\n\n"
        f"{brief['briefMarkdown']}"
    )


@mcp.tool()
def get_project_lineage(project_id: int) -> str:
    """Return the lineage edges (child_of / branched_from / supports / etc.) involving a project."""
    result = _api("GET", f"/api/projects/{project_id}/lineage")
    nodes = result.get("nodes", [])
    edges = result.get("edges", [])
    if not nodes:
        return f"No lineage data for project {project_id}."
    by_id = {n["id"]: n for n in nodes}
    lines = [f"Lineage for project {project_id}:"]
    for n in nodes:
        marker = "*" if n["depth"] == 0 else " "
        lines.append(f"  {marker} #{n['id']} {n['name']} ({n['slug']}) depth={n['depth']}")
    if edges:
        lines.append("\nEdges:")
        for e in edges:
            s = by_id.get(e["sourceProjectId"], {}).get("name", f"#{e['sourceProjectId']}")
            t = by_id.get(e["targetProjectId"], {}).get("name", f"#{e['targetProjectId']}")
            lines.append(f"  {s} --{e['relationType']}--> {t}")
    return "\n".join(lines)


@mcp.tool()
def get_project_activity(project_id: int, limit: int = 20) -> str:
    """Return recent activity events for a project."""
    rows = _api("GET", f"/api/projects/{project_id}/activity?limit={limit}")
    if not rows:
        return f"No activity for project {project_id}."
    lines = [f"Last {len(rows)} event(s) for project {project_id}:\n"]
    for r in rows:
        # createdAt comes back as an ISO string; trim to minute precision for display.
        created = (r.get("createdAt") or "")[:16].replace("T", " ")
        lines.append(
            f"  {created}  [{r['eventType']:25}]  {r['eventSummary']}  "
            f"- {r.get('createdBy', '?')} ({r.get('createdByType', '?')})"
        )
    return "\n".join(lines)


# ---- Agent flow tools (delegate to API) -----------------------------------


@mcp.tool()
def claim_next_task(
    project_id: int,
    agent_name: str | None = None,
    tool_name: str | None = None,
    capabilities: list[str] | None = None,
    claimer_kind: str | None = None,
) -> str:
    """Claim the highest-priority ready task on a project. Atomic.

    Identity defaults come from env vars (RECALLACCELERATOR_AGENT_NAME,
    RECALLACCELERATOR_TOOL_NAME, RECALLACCELERATOR_CLAIMER_KIND) so callers
    don't have to repeat them. Pass them explicitly to override.

    Returns the agent_session_id, task_id, and full agent context (project,
    brief, lineage, decisions, related notes, previous handoffs, writeback
    instructions). The same task cannot be claimed twice.
    """
    body = {
        "projectId": project_id,
        "agentName": agent_name or DEFAULT_IDENTITY["agent_name"],
        "toolName": tool_name or DEFAULT_IDENTITY["tool_name"],
        "capabilities": capabilities or [],
        "claimerKind": claimer_kind or DEFAULT_IDENTITY["claimer_kind"],
    }
    result = _api("POST", "/api/agent/tasks/claim-next", body)
    if "agentSessionId" not in result:
        return f"No tasks available. ({result.get('message', '')})"
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
def claim_specific_task(
    task_id: int,
    agent_name: str | None = None,
    tool_name: str | None = None,
    capabilities: list[str] | None = None,
    claimer_kind: str | None = None,
) -> str:
    """Claim a SPECIFIC task by id (vs claim_next_task which always picks the highest-priority).

    Useful for backfill bookkeeping when you want to close out a specific task
    that's blocked behind a higher-priority one in the queue, or when an agent
    knows exactly which task they want to work on next. Atomic with the same
    guarantees as claim_next_task. Returns null if the task isn't ready,
    already claimed, or its kind doesn't match the claimer.

    Identity defaults come from env vars (RECALLACCELERATOR_AGENT_NAME,
    RECALLACCELERATOR_TOOL_NAME, RECALLACCELERATOR_CLAIMER_KIND).
    """
    body = {
        "projectId": 0,  # the API endpoint resolves the project from the task automatically
        "agentName": agent_name or DEFAULT_IDENTITY["agent_name"],
        "toolName": tool_name or DEFAULT_IDENTITY["tool_name"],
        "capabilities": capabilities or [],
        "claimerKind": claimer_kind or DEFAULT_IDENTITY["claimer_kind"],
    }
    result = _api("POST", f"/api/agent/tasks/{task_id}/claim", body)
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
def get_task_context(task_id: int, agent_session_id: int) -> str:
    """Return the full agent context for a task (project, brief, lineage, decisions, notes, handoffs)."""
    result = _api("GET", f"/api/agent/tasks/{task_id}/context?agentSessionId={agent_session_id}")
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
def complete_task(
    task_id: int,
    agent_session_id: int,
    completion_summary: str,
    files_changed: list[str] | None = None,
    incomplete_work: str | None = None,
    recommended_next_steps: str | None = None,
    warnings: str | None = None,
    follow_up_tasks: list[dict] | None = None,
    release_id: int | None = None,
) -> str:
    """Mark a task done. Creates a handoff, ends the session, optionally adds follow-up tasks.

    Args:
        follow_up_tasks: list of dicts like {"title": "...", "description": "...", "priority": 70}
        release_id: optional - if provided, the just-completed task is auto-attached to the named release.
    """
    body = {
        "agentSessionId": agent_session_id,
        "completionSummary": completion_summary,
        "filesChanged": files_changed,
        "incompleteWork": incomplete_work,
        "recommendedNextSteps": recommended_next_steps,
        "warnings": warnings,
        "followUpTasks": follow_up_tasks,
    }
    result = _api("POST", f"/api/agent/tasks/{task_id}/complete", body)

    if release_id is not None:
        try:
            _api("POST", f"/api/releases/{release_id}/items",
                 {"itemType": "task", "targetId": task_id, "addedBy": "agent (auto-tag)"})
            result["releaseTagged"] = release_id
        except httpx.HTTPStatusError as exc:
            result["releaseTagError"] = f"{exc.response.status_code}: {exc.response.text[:200]}"

    return json.dumps(result, indent=2, default=str)


@mcp.tool()
def release_task(task_id: int, agent_session_id: int, reason: str | None = None) -> str:
    """Release a task claim without completing the work. Task returns to ready."""
    body = {"agentSessionId": agent_session_id, "reason": reason}
    _api("POST", f"/api/agent/tasks/{task_id}/release", body)
    return f"Task {task_id} released."


@mcp.tool()
def heartbeat_session(session_id: int, lease_extension_minutes: int | None = None) -> str:
    """Send a heartbeat to extend the lease on the active claim.

    Returns the new expiry time and seconds remaining so callers (and humans
    reading the trace) can decide whether to schedule the next heartbeat sooner.
    Default AI lease is 30 minutes; default human lease is 7 days.
    """
    body = {"leaseExtensionMinutes": lease_extension_minutes}
    result = _api("POST", f"/api/agent/sessions/{session_id}/heartbeat", body)
    expiry = result.get("leaseExpiresAt", "unknown")
    secs = result.get("timeUntilExpirySeconds")
    if secs is None:
        return f"Lease extended to {expiry}."
    minutes = secs // 60
    return f"Lease extended to {expiry} ({minutes} min / {secs}s remaining)."


@mcp.tool()
def end_session(session_id: int, summary: str | None = None) -> str:
    """End an agent session. Use when wrapping up without completing the active task.

    Note: as of v0.10.0 this auto-fires a SessionReconciliation if the session produced
    any side-output (proposals, notes, decisions). For an explicit summary the human
    sees on /Triage, prefer `end_session_with_reconciliation` and pass an
    `agent_closing_note`.
    """
    body = {"summary": summary}
    _api("POST", f"/api/agent/sessions/{session_id}/end", body)
    return f"Session {session_id} ended."


@mcp.tool()
def end_session_with_reconciliation(
    session_id: int,
    agent_closing_note: str | None = None,
) -> str:
    """End an agent session AND return a reconciliation envelope summarizing the
    session's side-output (task #40).

    Prefer this over plain `end_session` when wrapping up a session that produced
    proposals, ideas, decisions, or context notes — the reconciliation lands on
    /Triage as a "what the agent saw but you might miss" record the human reviews.

    Pass `agent_closing_note` with a short prose summary the human reads on top of
    the auto-built footprint (e.g. "I left two TODOs in src/foo.cs — they need
    domain decisions before I can wire them up").

    Idempotent: a second call on an already-ended session returns the existing
    reconciliation row.

    Note: `complete_task` already auto-fires a reconciliation. Use this tool when
    the session is ending WITHOUT completing the active claim (release path,
    or an exploratory session that won't finish a task), or when you want to
    attach an explicit closing note that complete_task didn't carry.
    """
    body = {"agentClosingNote": agent_closing_note}
    result = _api("POST", f"/api/agent/sessions/{session_id}/end-with-reconciliation", body)

    status = result.get("status")
    if status == "skipped_empty":
        return (
            f"Session {session_id} ended. No reconciliation written — the session produced "
            f"no tracked side-output (no proposals, notes, decisions, or ideas) and no closing "
            f"note was provided. Nothing for the human to triage."
        )

    counts = (
        f"{result.get('proposalsFiled', 0)} proposal(s), "
        f"{result.get('ideasCaptured', 0)} idea(s), "
        f"{result.get('decisionsRecorded', 0)} decision(s), "
        f"{result.get('contextNotesAdded', 0)} note(s), "
        f"{result.get('tasksCompleted', 0)} task(s) completed"
    )
    return (
        f"Session {session_id} ended. Reconciliation #{result.get('id')} written "
        f"(status: {status}). Footprint: {counts}. Triage at /Triage."
    )


@mcp.tool()
def add_context_note(
    project_id: int,
    agent_session_id: int,
    content: str,
    note_type: str = "general",
    task_id: int | None = None,
    feature_id: int | None = None,
    source: str | None = None,
) -> str:
    """Record a context note (warning, open_question, technical_context, etc.) on a project."""
    body = {
        "projectId": project_id,
        "featureId": feature_id,
        "taskId": task_id,
        "noteType": note_type,
        "content": content,
        "source": source,
        "agentSessionId": agent_session_id,
    }
    result = _api("POST", "/api/agent/context-notes", body)
    return f"Context note #{result.get('id')} created."


@mcp.tool()
def add_decision(
    project_id: int,
    agent_session_id: int,
    title: str,
    decision_text: str,
    reason: str | None = None,
    impact: str | None = None,
    feature_id: int | None = None,
    supersedes_decision_id: int | None = None,
) -> str:
    """Record a decision on a project."""
    body = {
        "projectId": project_id,
        "featureId": feature_id,
        "title": title,
        "decisionText": decision_text,
        "reason": reason,
        "impact": impact,
        "supersedesDecisionId": supersedes_decision_id,
        "agentSessionId": agent_session_id,
    }
    result = _api("POST", "/api/agent/decisions", body)
    return f"Decision #{result.get('id')} created: {title}"


@mcp.tool()
def quick_capture_idea(
    project_id: int,
    content: str,
    source: str | None = None,
) -> str:
    """Lightning-fast idea capture for a project. No session required — drop and run.

    Use this for the "wait, what was that thing?" moments mid-conversation. The capture
    creates a ContextNote with note_type='idea' and waits in the project until someone
    calls `synthesize_ideas_to_tasks` (or clicks the button on /Projects/Detail), at
    which point the LLM batches the unsynthesized ideas into structured TaskProposals
    that land at /Triage.

    Prefer this over `add_context_note(..., note_type='idea')` when you don't have an
    active agent session — capture should not require a claim.
    """
    body = {
        "content": content,
        "source": source or DEFAULT_IDENTITY["tool_name"],
        "createdBy": DEFAULT_IDENTITY["agent_name"],
    }
    result = _api("POST", f"/api/projects/{project_id}/ideas", body)
    return f"Idea note #{result.get('id')} captured for project {project_id}."


@mcp.tool()
def synthesize_ideas_to_tasks(project_id: int) -> str:
    """Ask the LLM to turn this project's unsynthesized idea-notes into structured task proposals.

    The resulting proposals land at /Triage as 'pending' — the human triages them into real
    ready tasks (or rejects them). Notes consumed during synthesis are marked so they don't
    get re-processed on the next call.

    If the RA instance has no Anthropic API key configured, returns guidance for adding the
    `anthropic_api_key` credential (env var override `Anthropic__ApiKey`) at /Credentials.
    """
    result = _api("POST", f"/api/projects/{project_id}/ideas/synthesize", {})
    if result.get("llmDisabled"):
        msg = result.get("message") or "LLM not configured."
        return f"Synthesis skipped — {msg}"
    n = result.get("ideasProcessed", 0)
    m = result.get("proposalsCreated", 0)
    if n == 0:
        return f"No unsynthesized ideas to process on project {project_id}."
    return (
        f"Synthesized {n} idea(s) into {m} task proposal(s). "
        f"Triage at /Triage. Proposal IDs: {result.get('proposalIds', [])}."
    )


@mcp.tool()
def propose_task(
    project_id: int,
    title: str,
    context: str | None = None,
    suggested_priority: int | None = None,
    source: str | None = None,
    agent_session_id: int | None = None,
) -> str:
    """Throw a half-formed task suggestion into the project's Pending Proposals bucket.

    Prefer this over create_task when you're surfacing follow-up work the human hasn't
    asked for yet — proposals don't pollute the ready queue and the human triages from
    /Proposals (or /Triage). create_task is for work that's already approved.

    title is required; everything else is optional. agent_session_id is automatically
    associated with whatever session you're in if you pass it.
    """
    body = {
        "projectId": project_id,
        "title": title,
        "context": context,
        "suggestedPriority": suggested_priority,
        "source": source or DEFAULT_IDENTITY["tool_name"],
        "agentSessionId": agent_session_id,
    }
    result = _api("POST", "/api/agent/proposals", body)
    return (
        f"Proposal #{result.get('id')} pending: {result.get('title')}"
        + (f" (suggested priority: {result.get('suggestedPriority')})" if result.get('suggestedPriority') is not None else "")
    )


@mcp.tool()
def propose_brief_update(
    project_id: int,
    agent_session_id: int,
    base_brief_version_id: int,
    proposed_brief_markdown: str,
    proposed_patch_summary: str | None = None,
    reason: str | None = None,
) -> str:
    """Propose a project brief update. Does NOT overwrite the current brief; a human approves."""
    body = {
        "projectId": project_id,
        "baseBriefVersionId": base_brief_version_id,
        "proposedBriefMarkdown": proposed_brief_markdown,
        "proposedPatchSummary": proposed_patch_summary,
        "reason": reason,
        "agentSessionId": agent_session_id,
    }
    result = _api("POST", "/api/agent/brief-update-proposals", body)
    return f"Brief update proposal #{result.get('id')} created (status={result.get('status')})."


# ---- Bootstrap tools (cold-start a new project from agent context) ---------


@mcp.tool()
def create_workspace(name: str, description: str | None = None) -> str:
    """Create a new workspace (top-level container for projects). Use sparingly - usually you want one workspace per user."""
    result = _api("POST", "/api/workspaces", {"name": name, "description": description})
    return f"Workspace #{result.get('id')} '{result.get('name')}' created."


@mcp.tool()
def list_future_projects(workspace_id: int) -> str:
    """List 'future-idea' projects in a workspace — the parking lot for ideas
    that haven't earned a real project yet (#42).

    Future-ideas don't show up on the dashboard or in default /Projects lists.
    They live at /Ideas and get promoted to active projects via the UI when
    they mature into something worth tracking. Use this MCP tool to discover
    what ideas are sitting parked.
    """
    # The list_projects API endpoint returns ALL statuses; filter client-side.
    result = _api("GET", f"/api/projects?workspaceId={workspace_id}")
    if not isinstance(result, list):
        return "Unexpected response shape from /api/projects."
    ideas = [p for p in result if p.get("status") == "future_idea"]
    if not ideas:
        return f"No future-idea projects in workspace {workspace_id}."
    lines = [f"{len(ideas)} future-idea project(s) in workspace {workspace_id}:\n"]
    for p in ideas:
        desc = p.get("compactDescription") or "(no description)"
        lines.append(f"  #{p.get('id'):>3} [{p.get('slug')}]  {p.get('name')} — {desc}")
    return "\n".join(lines)


@mcp.tool()
def create_idea(
    workspace_id: int,
    name: str,
    slug: str,
    compact_description: str | None = None,
    goal: str | None = None,
    repo_url: str | None = None,
) -> str:
    """Park a future-project idea (#42). Same data shape as create_project but
    the result lands in the /Ideas parking lot with status='future_idea',
    hidden from the dashboard and default /Projects view until the human
    promotes it.

    Use this when you (the agent) discover a half-formed idea worth capturing
    but the user hasn't committed to making it a real project. The human
    promotes via /Ideas → "Promote to active project" when it matures.

    Slug must still be unique across all projects (active + idea + archived).
    """
    body = {
        "workspaceId": workspace_id,
        "name": name,
        "slug": slug,
        "compactDescription": compact_description,
        "goal": goal,
        "currentScope": None,
        "repoUrl": repo_url,
        "status": "future_idea",
    }
    result = _api("POST", "/api/projects", body)
    return (
        f"Future-idea #{result.get('id')} '{result.get('name')}' (slug: {result.get('slug')}) parked. "
        f"Visible at /Ideas. Human can promote when ready."
    )


@mcp.tool()
def create_project(
    workspace_id: int,
    name: str,
    slug: str,
    compact_description: str | None = None,
    goal: str | None = None,
    current_scope: str | None = None,
    repo_url: str | None = None,
) -> str:
    """Create a new project under a workspace. Slug must be unique across all projects.

    Phase 2 onboarding (#49): the response now includes ready-to-drop per-repo agent
    files (CLAUDE.md, AGENTS.md, .cursorrules) with the new project's slug, project_id,
    instance URL and repo URL pre-substituted. Surface them to the user so they can
    paste at the repo root — no manual editing needed.
    """
    body = {
        "workspaceId": workspace_id,
        "name": name,
        "slug": slug,
        "compactDescription": compact_description,
        "goal": goal,
        "currentScope": current_scope,
        "repoUrl": repo_url,
    }
    result = _api("POST", "/api/projects", body)
    project_id = result.get("id")
    project_name = result.get("name")
    project_slug = result.get("slug")

    summary = (
        f"Project #{project_id} '{project_name}' (slug: {project_slug}) created.\n\n"
        "Per-repo agent files have been generated and are ready to drop at the repo root:\n"
        "  - CLAUDE.md      (Claude Code)\n"
        "  - AGENTS.md      (Codex / cross-tool)\n"
        "  - .cursorrules   (Cursor)\n\n"
        f"Fetch the contents with get_project_agent_files({project_id}), then show them to "
        "the user with the instructions where to drop each file. Pick whichever matches "
        "the user's tool — multiple is fine."
    )

    # If the API actually included the files inline (it does), include the keys for the
    # agent's awareness without dumping the whole payload into the chat.
    files = (result.get("agentFiles") or {}).get("files") or {}
    if files:
        summary += f"\n\nThe response payload also embedded the file contents directly under .agentFiles.files: {sorted(files.keys())}."
    return summary


@mcp.tool()
def get_project_agent_files(project_id: int, tool: str | None = None) -> str:
    """Fetch the per-repo agent instruction file(s) for an existing project.

    Phase 2 onboarding (#49): drop these at the repo root so a fresh agent in a new
    clone can find its way back to the right RA project (slug, project_id, instance
    URL all pre-filled).

    Without `tool`, returns all three files (CLAUDE.md / AGENTS.md / .cursorrules).
    With `tool` set to one of {claude-code, codex, cursor}, returns just that file.

    Use this for retrofit on projects that pre-date the Phase 2 work, or when the
    user has lost their original copy.
    """
    if tool:
        result = _api("GET", f"/api/projects/{project_id}/agent-files?tool={tool}")
        file_name = result.get("file")
        content = result.get("content") or ""
        return f"--- {file_name} ---\n{content}"

    result = _api("GET", f"/api/projects/{project_id}/agent-files")
    files = result.get("files") or {}
    instructions = result.get("instructions") or ""
    parts = [f"Instructions: {instructions}"]
    for name in ("CLAUDE.md", "AGENTS.md", ".cursorrules"):
        if name in files:
            parts.append(f"\n--- {name} ---\n{files[name]}")
    return "\n".join(parts)


@mcp.tool()
def update_task(
    task_id: int,
    title: str | None = None,
    description: str | None = None,
    agent_instructions: str | None = None,
    acceptance_criteria: str | None = None,
    priority: int | None = None,
    kind: str | None = None,
    status: str | None = None,
    phase_id: int | None = None,
    feature_id: int | None = None,
) -> str:
    """Update fields on an existing task. Sparse: any unspecified field is left alone.

    Use this to reprioritize, fix typos, evolve descriptions as understanding grows,
    or change the kind (agent_only ↔ either) when scope shifts. Each changed field
    writes one row to TaskAuditLog so the change history is queryable.

    Constraints:
    - Tasks with an active claim are rejected (409). Release first if you need to mutate.
    - status here is limited to backlog | ready | blocked. Use complete_task for done;
      release_task to drop a claim.
    - priority is clamped 0-100.
    - kind must be one of agent_only | human_only | either.
    """
    body: dict[str, object | None] = {}
    if title is not None: body["title"] = title
    if description is not None: body["description"] = description
    if agent_instructions is not None: body["agentInstructions"] = agent_instructions
    if acceptance_criteria is not None: body["acceptanceCriteria"] = acceptance_criteria
    if priority is not None: body["priority"] = priority
    if kind is not None: body["kind"] = kind
    if status is not None: body["status"] = status
    if phase_id is not None: body["phaseId"] = phase_id
    if feature_id is not None: body["featureId"] = feature_id

    if not body:
        return "No fields supplied — nothing to update."

    result = _api("PATCH", f"/api/tasks/{task_id}", body)
    changed = ", ".join(body.keys())
    return f"Task #{result.get('id')} '{result.get('title')}' updated. Fields changed: {changed}."


@mcp.tool()
def create_brief_version(
    project_id: int,
    brief_markdown: str,
    change_summary: str | None = None,
    created_by: str | None = None,
) -> str:
    """Create the next brief version for a project (becomes the current brief).

    Use at project bootstrap to seed the initial brief. For subsequent updates,
    prefer propose_brief_update so a human can curate.
    """
    body = {
        "briefMarkdown": brief_markdown,
        "createdBy": created_by or DEFAULT_IDENTITY["agent_name"],
        "createdByType": "agent",
        "changeSummary": change_summary,
    }
    result = _api("POST", f"/api/projects/{project_id}/brief/versions", body)
    return f"Brief version #{result.get('id')} (v{result.get('versionNumber')}) created and set as current."


@mcp.tool()
def create_task(
    project_id: int,
    title: str,
    description: str | None = None,
    agent_instructions: str | None = None,
    acceptance_criteria: str | None = None,
    kind: str = "either",
    priority: int = 50,
    status: str = "ready",
    feature_id: int | None = None,
    phase_id: int | None = None,
) -> str:
    """Create a task on a project. kind: 'agent_only' | 'human_only' | 'either'.

    Status defaults to 'ready' so the task is immediately claimable. Use 'backlog'
    if you're capturing work that's not yet refined enough to assign.
    """
    body = {
        "title": title,
        "description": description,
        "agentInstructions": agent_instructions,
        "acceptanceCriteria": acceptance_criteria,
        "kind": kind,
        "priority": priority,
        "status": status,
        "featureId": feature_id,
        "phaseId": phase_id,
    }
    result = _api("POST", f"/api/projects/{project_id}/tasks", body)
    return f"Task #{result.get('id')} '{result.get('title')}' created (status: {result.get('status')}, kind: {result.get('kind')})."


# ---- Release tools (delegate to API) ---------------------------------------


@mcp.tool()
def list_releases(project_id: int, status: str | None = None) -> str:
    """List releases for a project. Optional status filter: planned, in_progress, released, rolled_back."""
    qs = f"?status={status}" if status else ""
    rows = _api("GET", f"/api/projects/{project_id}/releases{qs}")
    if not rows:
        return f"No releases for project {project_id}."
    lines = [f"{len(rows)} release(s) for project {project_id}:\n"]
    for r in rows:
        released = r.get("releasedAt") or "-"
        lines.append(
            f"  #{r['id']:>3} {r['version']:>10}  [{r['status']:>11}]  "
            f"items={r['itemCount']}  target={r['targetEnvironment']}  released={released}"
        )
    return "\n".join(lines)


@mcp.tool()
def get_release(release_id: int) -> str:
    """Return full release detail including all attached items."""
    result = _api("GET", f"/api/releases/{release_id}")
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
def get_release_changelog(release_id: int) -> str:
    """Return the markdown changelog for a release."""
    url = f"{API_BASE_URL}/api/releases/{release_id}/changelog"
    with httpx.Client(timeout=30.0) as client:
        resp = client.get(url)
        resp.raise_for_status()
        return resp.text


@mcp.tool()
def create_release(
    project_id: int,
    version: str,
    name: str | None = None,
    release_notes: str | None = None,
    target_environment: str | None = None,
) -> str:
    """Create a planned release. Version must be unique within the project."""
    body = {
        "version": version,
        "name": name,
        "releaseNotes": release_notes,
        "targetEnvironment": target_environment,
    }
    result = _api("POST", f"/api/projects/{project_id}/releases", body)
    return f"Release #{result.get('id')} {result.get('version')} created (status={result.get('status')})."


@mcp.tool()
def attach_to_release(
    release_id: int,
    item_type: str,
    target_id: int,
    notes: str | None = None,
    added_by: str | None = None,
) -> str:
    """Attach a task, feature, or decision to a release.

    Args:
        release_id: target release.
        item_type: 'task' | 'feature' | 'decision'.
        target_id: id of the task/feature/decision.
        notes: optional note to surface in the changelog.
        added_by: who attached it (defaults to 'system' on the API side).
    """
    body = {"itemType": item_type, "targetId": target_id, "notes": notes, "addedBy": added_by}
    result = _api("POST", f"/api/releases/{release_id}/items", body)
    return f"Attached {item_type} #{target_id} to release {release_id} as item #{result.get('id')}."


@mcp.tool()
def start_release(release_id: int, git_ref: str | None = None) -> str:
    """Transition a release planned -> in_progress."""
    result = _api("POST", f"/api/releases/{release_id}/start", {"gitRef": git_ref})
    return f"Release {release_id} now {result.get('status')}."


@mcp.tool()
def complete_release(
    release_id: int,
    released_by: str,
    git_ref: str | None = None,
    deployment_artifact: str | None = None,
    skip_brief_proposal: bool = False,
) -> str:
    """Mark a release as shipped. Auto-creates a brief-update proposal unless skip_brief_proposal=True."""
    body = {
        "releasedBy": released_by,
        "gitRef": git_ref,
        "deploymentArtifact": deployment_artifact,
        "skipBriefProposal": skip_brief_proposal,
    }
    result = _api("POST", f"/api/releases/{release_id}/complete", body)
    return (
        f"Release {release_id} ({result.get('version')}) marked released by {released_by}."
        + ("" if skip_brief_proposal else " A pending brief-update proposal was generated.")
    )


@mcp.tool()
def rollback_release(release_id: int, reason: str | None = None) -> str:
    """Mark a released release as rolled_back. Audit-only; does NOT undo the deploy."""
    result = _api("POST", f"/api/releases/{release_id}/rollback", {"reason": reason})
    return f"Release {release_id} now {result.get('status')}.{(' Reason: ' + reason) if reason else ''}"


# ---- Credentials (METADATA ONLY — values flow through the racred CLI) ----------
#
# Decision #27 invariant: the LLM chat session never sees a credential value.
# The MCP intentionally has NO tool that returns cleartext. If you find yourself
# wanting one, that's a sign to use racred from the agent's bash tool instead
# (e.g. `racred run -- gh repo list` injects the value into the child process,
# never into your context). Adding a value-returning MCP tool would silently
# bypass the entire security boundary this server was designed around.


@mcp.tool()
def list_credentials(project_id: int, category: str | None = None) -> str:
    """List credentials visible to a project — its own + every global. METADATA ONLY.

    Returns names, categories, env-var mappings, descriptions, and disabled status.
    To actually read a value, run `racred get <name>` from a shell inside the
    project repo, or `racred run -- <command>` to inject creds into a child
    process's environment.

    Args:
        project_id: The project to scope the listing to. Globals (ProjectId IS NULL)
            are always included; project-scoped creds appear only for THIS project.
        category: Optional filter (e.g. "database", "api", "ftp"). Free-form string —
            matches exactly, case-sensitive.
    """
    qs = f"?category={category}" if category else ""
    rows = _api("GET", f"/api/projects/{project_id}/credentials{qs}")
    if not rows:
        return f"No credentials visible from project {project_id}."

    # Group by (scope, category) so the agent gets a clean overview rather than a flat list.
    globals_, scoped = [], []
    for r in rows:
        (globals_ if r.get("projectId") is None else scoped).append(r)

    def _fmt(rows_: list) -> list[str]:
        out = []
        by_cat: dict[str, list] = {}
        for r in rows_:
            by_cat.setdefault(r.get("category") or "(uncategorized)", []).append(r)
        for cat in sorted(by_cat):
            out.append(f"  [{cat}]")
            for r in by_cat[cat]:
                disabled = " [DISABLED]" if r.get("isDisabled") else ""
                env = r.get("envVar") or r["name"].upper()
                desc = f" — {r['description']}" if r.get("description") else ""
                out.append(f"    {r['name']:30} → ${env}{disabled}{desc}")
        return out

    lines = [f"Credentials visible to project {project_id} ({len(rows)} total):\n"]
    if globals_:
        lines.append(f"Global ({len(globals_)}):")
        lines.extend(_fmt(globals_))
        lines.append("")
    if scoped:
        lines.append(f"Project-scoped ({len(scoped)}):")
        lines.extend(_fmt(scoped))
    lines.append("")
    lines.append("To fetch a value: `racred get <name>` (from a shell inside the project repo).")
    lines.append("To inject into a command: `racred run -- <command>` (e.g. `racred run -- gh repo list`).")
    return "\n".join(lines)


@mcp.tool()
def describe_credential(project_id: int, name: str) -> str:
    """Resolve a credential by name within a project's scope and return its metadata.

    Project-scoped wins over global with the same name. Useful when you need to
    confirm a cred exists, see its env-var mapping, or check whether it's been
    disabled — BEFORE telling the user to run `racred get` and finding out it
    doesn't exist.

    Returns metadata only. Never the value.
    """
    rows = _api("GET", f"/api/projects/{project_id}/credentials")
    if not isinstance(rows, list):
        return "Unexpected response from /api/projects/{id}/credentials."

    matches = [r for r in rows if r.get("name") == name]
    if not matches:
        return (
            f"No credential named '{name}' visible from project {project_id}. "
            f"Use list_credentials to see what's available, or ask the user to add one via /Credentials."
        )

    # Precedence: project-scoped > global. The list returns globals first, but to be
    # explicit, prefer the row whose projectId matches.
    project_scoped = [r for r in matches if r.get("projectId") == project_id]
    chosen = project_scoped[0] if project_scoped else matches[0]

    scope = "project-scoped" if chosen.get("projectId") is not None else "global"
    env = chosen.get("envVar") or chosen["name"].upper()
    parts = [
        f"Credential '{chosen['name']}':",
        f"  scope:        {scope}" + (f" (project {chosen.get('projectId')})" if chosen.get("projectId") else ""),
        f"  category:     {chosen.get('category') or '(uncategorized)'}",
        f"  env var:      ${env}",
        f"  description:  {chosen.get('description') or '(none)'}",
        f"  status:       {'DISABLED — ' + (chosen.get('disabledReason') or 'no reason given') if chosen.get('isDisabled') else 'active'}",
        f"  updated:      {chosen.get('updatedAt')}",
        "",
        f"To fetch the value: `racred get {chosen['name']}` (from project repo)",
    ]
    return "\n".join(parts)


@mcp.tool()
def server_info() -> str:
    """Diagnostic: API URL, config path, identity defaults, and whether an API key is configured.

    All tools route through the API now - no direct SQL. Agent boxes only need
    the API key + URL.
    """
    return (
        f"RecallAccelerator MCP\n"
        f"  config:        {CONFIG_PATH}\n"
        f"  api:           {API_BASE_URL}\n"
        f"  api_key:       {'(set)' if API_KEY else '(not set - required when prod has Auth:RequireApiKey=true)'}\n"
        f"  agent_name:    {DEFAULT_IDENTITY['agent_name']}\n"
        f"  tool_name:     {DEFAULT_IDENTITY['tool_name']}\n"
        f"  claimer_kind:  {DEFAULT_IDENTITY['claimer_kind']}"
    )


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
