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
    """Send a heartbeat to extend the lease on the active claim."""
    body = {"leaseExtensionMinutes": lease_extension_minutes}
    result = _api("POST", f"/api/agent/sessions/{session_id}/heartbeat", body)
    return f"Lease extended to {result.get('leaseExpiresAt', 'unknown')}."


@mcp.tool()
def end_session(session_id: int, summary: str | None = None) -> str:
    """End an agent session. Use when wrapping up without completing the active task."""
    body = {"summary": summary}
    _api("POST", f"/api/agent/sessions/{session_id}/end", body)
    return f"Session {session_id} ended."


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
def create_project(
    workspace_id: int,
    name: str,
    slug: str,
    compact_description: str | None = None,
    goal: str | None = None,
    current_scope: str | None = None,
    repo_url: str | None = None,
) -> str:
    """Create a new project under a workspace. Slug must be unique across all projects."""
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
    return f"Project #{result.get('id')} '{result.get('name')}' (slug: {result.get('slug')}) created."


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
