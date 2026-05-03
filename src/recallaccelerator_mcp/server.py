"""RecallAccelerator MCP server.

Exposes the RecallAccelerator project-memory and task-tracker over MCP so
Claude Code, Cursor, Codex, and other AI coding agents can claim work,
read project context, write back progress, and propose brief updates.

Read tools query SQL Server directly (fast). Write tools delegate to the
.NET API (single source of truth for state mutations).
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pyodbc
from fastmcp import FastMCP

from .config import API_BASE_URL, CONFIG_PATH, DB_CONNECTION_STRING, tbl

mcp = FastMCP("RecallAccelerator")


def _get_db():
    return pyodbc.connect(DB_CONNECTION_STRING)


def _api(method: str, path: str, body: dict | None = None) -> Any:
    url = f"{API_BASE_URL}{path}"
    with httpx.Client(timeout=60.0) as client:
        if method == "GET":
            resp = client.get(url)
        elif method == "POST":
            resp = client.post(url, json=body or {})
        else:
            raise ValueError(f"Unsupported method: {method}")
        resp.raise_for_status()
        if not resp.content:
            return {}
        try:
            return resp.json()
        except json.JSONDecodeError:
            return {"raw": resp.text}


# ---- Read tools (direct DB) ------------------------------------------------


@mcp.tool()
def list_projects() -> str:
    """List all projects with id, slug, status, and current brief version."""
    conn = _get_db()
    try:
        cursor = conn.cursor()
        cursor.execute(
            f"""
            SELECT p.Id, p.Name, p.Slug, p.Status, p.CompactDescription,
                   p.CurrentBriefVersionId, p.CurrentPhaseId
            FROM {tbl('Projects')} p
            ORDER BY p.Name
            """
        )
        rows = cursor.fetchall()
        if not rows:
            return "No projects."
        lines = [f"Found {len(rows)} project(s):\n"]
        for r in rows:
            brief = f" v{r[5]}" if r[5] else ""
            lines.append(
                f"  #{r[0]:>3} {r[2]:30} [{r[3]:8}]  {r[1]}{brief}\n"
                f"        {r[4] or '(no description)'}"
            )
        return "\n".join(lines)
    finally:
        conn.close()


@mcp.tool()
def list_ready_tasks(project_id: int, limit: int = 20) -> str:
    """List ready (claimable) tasks for a project, ordered by priority desc."""
    conn = _get_db()
    try:
        cursor = conn.cursor()
        cursor.execute(
            f"""
            SELECT TOP (?) t.Id, t.Title, t.Priority, t.Description, t.AcceptanceCriteria
            FROM {tbl('Tasks')} t
            WHERE t.ProjectId = ? AND t.Status = 'ready'
              AND NOT EXISTS (
                  SELECT 1 FROM {tbl('TaskClaims')} c
                  WHERE c.TaskId = t.Id AND c.Status = 'active' AND c.LeaseExpiresAt > SYSUTCDATETIME()
              )
            ORDER BY t.Priority DESC, t.CreatedAt ASC
            """,
            (limit, project_id),
        )
        rows = cursor.fetchall()
        if not rows:
            return f"No ready tasks for project {project_id}."
        lines = [f"{len(rows)} ready task(s) for project {project_id}:\n"]
        for r in rows:
            desc = (r[3] or "").strip()
            if desc and len(desc) > 120:
                desc = desc[:117] + "..."
            lines.append(f"  #{r[0]:>4} [pri {r[2]:>3}]  {r[1]}\n        {desc}")
        return "\n".join(lines)
    finally:
        conn.close()


@mcp.tool()
def get_project_brief(project_id: int) -> str:
    """Return the current project brief markdown, or a message if none is set."""
    conn = _get_db()
    try:
        cursor = conn.cursor()
        cursor.execute(
            f"""
            SELECT b.VersionNumber, b.BriefMarkdown, b.CreatedBy, b.CreatedByType, b.CreatedAt
            FROM {tbl('Projects')} p
            JOIN {tbl('ProjectBriefVersions')} b ON b.Id = p.CurrentBriefVersionId
            WHERE p.Id = ?
            """,
            (project_id,),
        )
        row = cursor.fetchone()
        if not row:
            return f"No current brief for project {project_id}."
        return (
            f"# Brief v{row[0]} - by {row[2]} ({row[3]}) on {row[4]}\n\n{row[1]}"
        )
    finally:
        conn.close()


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
    conn = _get_db()
    try:
        cursor = conn.cursor()
        cursor.execute(
            f"""
            SELECT TOP (?) e.CreatedAt, e.EventType, e.EventSummary, e.CreatedBy, e.CreatedByType
            FROM {tbl('ProjectEvents')} e
            WHERE e.ProjectId = ?
            ORDER BY e.CreatedAt DESC
            """,
            (limit, project_id),
        )
        rows = cursor.fetchall()
        if not rows:
            return f"No activity for project {project_id}."
        lines = [f"Last {len(rows)} event(s) for project {project_id}:\n"]
        for r in rows:
            lines.append(f"  {r[0]:%Y-%m-%d %H:%M}  [{r[1]:25}]  {r[2]}  - {r[3]} ({r[4]})")
        return "\n".join(lines)
    finally:
        conn.close()


# ---- Agent flow tools (delegate to API) -----------------------------------


@mcp.tool()
def claim_next_task(
    project_id: int,
    agent_name: str = "Claude Code",
    tool_name: str = "claude-code",
    capabilities: list[str] | None = None,
) -> str:
    """Claim the highest-priority ready task on a project. Atomic.

    Returns the agent_session_id, task_id, and full agent context (project,
    brief, lineage, decisions, related notes, previous handoffs, writeback
    instructions). The same task cannot be claimed twice.
    """
    body = {
        "projectId": project_id,
        "agentName": agent_name,
        "toolName": tool_name,
        "capabilities": capabilities or [],
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
    """Diagnostic: return the MCP server's view of the API URL, config path, and schema."""
    return (
        f"RecallAccelerator MCP\n"
        f"  config:  {CONFIG_PATH}\n"
        f"  api:     {API_BASE_URL}\n"
        f"  db conn: {DB_CONNECTION_STRING.split(';PWD=')[0]};PWD=***"
    )


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
