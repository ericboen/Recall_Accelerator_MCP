"""racred — credential broker CLI for RecallAccelerator.

This is the ONLY supported path that returns credential cleartext. The MCP
exposes names; the racred CLI fetches values. The split is deliberate: an LLM
chat session running the MCP cannot retrieve secrets, so a compromised or
prompt-injected session can't exfiltrate them. Values reach a child process'
environment and never enter your conversation.

Usage:
    racred list                 # show creds visible to the project at CWD
    racred get NAME             # print the value (newline-terminated) to stdout
    racred run -- CMD [args]    # exec CMD with all visible creds injected as env
    racred whoami               # diagnostic: which project, which API, which key

Project resolution (matches the MCP):
    1. --project SLUG flag overrides everything.
    2. AGENTS.md / .agents.md / agents.md slug override at the repo root.
    3. Lowercased current folder name → /api/projects lookup.
    4. If none resolves: fail loud (Decision #27 #10) — never silently fall
       back to "globals only" because someone forgot to cd into a repo.

Auth: the same RECALLACCELERATOR_API_URL + RECALLACCELERATOR_API_KEY env vars
the MCP already uses. Set them once at the user level.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Any

import httpx

from .config import API_BASE_URL, API_KEY, DEFAULT_IDENTITY, auth_headers

# Cache of (slug -> project_id) results from /api/projects so a `racred run` that
# needs to look up the project once doesn't bombard the API. Process-lifetime only;
# the CLI is short-lived so a stale cache doesn't matter in practice.
_PROJECT_CACHE: dict[str, int] = {}


# ---------------------------------------------------------------------------
# Project resolution
# ---------------------------------------------------------------------------

_AGENTS_FILES = ("AGENTS.md", ".agents.md", "agents.md", "AGENTS.MD")
_SLUG_LINE_RE = re.compile(r"^\s*[-*]?\s*slug\s*:\s*([\w.-]+)", re.IGNORECASE | re.MULTILINE)


def _slug_from_agents_file(start: Path) -> str | None:
    """Walk up from `start` looking for an AGENTS.md / agents.md file with a `slug:` line.

    Stops at filesystem root. Match is case-insensitive on `slug:` and accepts the
    leading bullet variants ("- slug: foo", "* slug: foo", "slug: foo") that humans
    naturally write.
    """
    cur = start.resolve()
    while True:
        for name in _AGENTS_FILES:
            candidate = cur / name
            if candidate.is_file():
                try:
                    text = candidate.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                m = _SLUG_LINE_RE.search(text)
                if m:
                    return m.group(1).lower()
        if cur.parent == cur:
            return None
        cur = cur.parent


def _slug_from_cwd() -> str:
    return Path.cwd().name.lower()


def _resolve_project(slug_override: str | None) -> tuple[str, int]:
    """Return (slug, project_id) or sys.exit(2) if no project context can be found.

    The fail-loud behavior is intentional (Decision #27 #10): silently falling back
    to globals when someone forgot to cd would be a quiet security regression.
    """
    if slug_override:
        slug = slug_override.lower()
    elif (s := _slug_from_agents_file(Path.cwd())):
        slug = s
    else:
        slug = _slug_from_cwd()

    if not slug or slug == "/":
        _die(
            "Could not resolve a project. Either cd into a repo whose folder name matches a project slug,\n"
            "  or pass --project SLUG explicitly. Use `racred whoami` to debug.",
            code=2,
        )

    if slug in _PROJECT_CACHE:
        return slug, _PROJECT_CACHE[slug]

    try:
        rows = _api_get("/api/projects")
    except httpx.HTTPError as exc:
        _die(f"Failed to query /api/projects: {exc}", code=3)

    project = None
    if isinstance(rows, list):
        project = next((p for p in rows if p.get("slug") == slug), None)
    if project is None:
        _die(
            f"No project with slug '{slug}' on {API_BASE_URL}. "
            f"Add one via /Projects, or pass --project to point at an existing slug.",
            code=2,
        )

    _PROJECT_CACHE[slug] = project["id"]
    return slug, project["id"]


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _http_client() -> httpx.Client:
    return httpx.Client(timeout=30.0, headers=auth_headers())


def _api_get(path: str) -> Any:
    with _http_client() as c:
        r = c.get(f"{API_BASE_URL}{path}")
        r.raise_for_status()
        return r.json() if r.content else {}


def _api_post(path: str, body: dict) -> tuple[int, Any]:
    with _http_client() as c:
        r = c.post(f"{API_BASE_URL}{path}", json=body)
        if r.status_code >= 500:
            r.raise_for_status()
        try:
            payload = r.json() if r.content else {}
        except ValueError:
            payload = {"raw": r.text}
        return r.status_code, payload


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def cmd_list(args: argparse.Namespace) -> int:
    slug, pid = _resolve_project(args.project)
    qs = f"?category={args.category}" if args.category else ""
    rows = _api_get(f"/api/projects/{pid}/credentials{qs}")
    if not rows:
        _print(f"No credentials visible from project '{slug}' (#{pid}).")
        return 0

    globals_ = [r for r in rows if r.get("projectId") is None]
    scoped = [r for r in rows if r.get("projectId") is not None]

    def _emit(group_label: str, items: list[dict]) -> None:
        if not items:
            return
        _print(f"\n{group_label} ({len(items)}):")
        by_cat: dict[str, list[dict]] = {}
        for r in items:
            by_cat.setdefault(r.get("category") or "(uncategorized)", []).append(r)
        for cat in sorted(by_cat):
            _print(f"  [{cat}]")
            for r in by_cat[cat]:
                disabled = " [DISABLED]" if r.get("isDisabled") else ""
                env = r.get("envVar") or r["name"].upper()
                _print(f"    {r['name']:30} → ${env}{disabled}")

    _print(f"Credentials visible to project '{slug}' (#{pid}):")
    _emit("Global", globals_)
    _emit("Project-scoped", scoped)
    _print("")
    _print(f"Total: {len(rows)}")
    return 0


def cmd_get(args: argparse.Namespace) -> int:
    """Print a single credential value to stdout. Designed for `$(racred get FOO)` substitution.

    Output is the raw value followed by a single newline. No prefix, no label.
    Errors go to stderr with a non-zero exit code so shell substitution fails noisily.
    """
    slug, pid = _resolve_project(args.project)
    status, payload = _api_post(
        "/api/credentials/fetch",
        {"name": args.name, "projectId": pid, "commandBasename": None},
    )
    if status == 200:
        # The API returns CredentialFetchResult: {name, value, envVar, projectId}.
        sys.stdout.write(payload.get("value", ""))
        sys.stdout.write("\n")
        return 0
    if status == 404:
        _die(payload.get("error") or f"No credential '{args.name}' visible from project '{slug}'.", code=4)
    if status == 410:
        _die(f"Credential '{args.name}' is disabled. {payload.get('error') or ''}".strip(), code=5)
    if status == 401 or status == 403:
        _die("Unauthorized. Check RECALLACCELERATOR_API_KEY.", code=6)
    _die(f"Unexpected response {status}: {payload}", code=1)


def cmd_run(args: argparse.Namespace) -> int:
    """Fetch every visible cred, inject as env vars, then exec args.command.

    The child process inherits a copy of the parent's environment with credential
    values overlaid (so PATH, HOME, etc. survive). After exec returns, the values
    are gone — they were only ever in the child's process memory.

    Each successful fetch is one audit row, with command_basename = basename(args.command[0]),
    so the audit trail tells you "this run pulled X creds for `gh`."
    """
    if not args.command:
        _die("racred run -- CMD [args]: no command supplied.", code=2)

    slug, pid = _resolve_project(args.project)
    cmd_path = args.command[0]
    cmd_base = os.path.basename(cmd_path)

    # 1. Get the visible-credentials list (metadata).
    rows = _api_get(f"/api/projects/{pid}/credentials")
    visible = [r for r in rows if not r.get("isDisabled")]
    if not visible:
        _stderr(f"racred: no credentials visible from project '{slug}'; running '{cmd_base}' with no injected env.")

    # 2. Fetch each value. Audit row per fetch via the API.
    env = os.environ.copy()
    fetched = 0
    for r in visible:
        status, payload = _api_post(
            "/api/credentials/fetch",
            {"name": r["name"], "projectId": pid, "commandBasename": cmd_base},
        )
        if status != 200:
            _stderr(f"racred: skipping '{r['name']}' (HTTP {status})")
            continue
        env_var = payload.get("envVar") or r["name"].upper()
        # Multi-mapping: the EnvVar field can be "FOO,BAR" — export under both names.
        for name in (n.strip() for n in env_var.split(",") if n.strip()):
            env[name] = payload["value"]
        fetched += 1

    _stderr(f"racred: injected {fetched} credential(s) into env for '{cmd_base}'.")

    # 3. Exec. On POSIX we use os.execvpe to replace the current process; on Windows
    #    that's not actually replacing, but it's still the cleanest way to chain.
    try:
        os.execvpe(cmd_path, args.command, env)
    except FileNotFoundError:
        _die(f"racred: command not found: {cmd_path}", code=127)
    return 0  # unreachable on success


def cmd_whoami(args: argparse.Namespace) -> int:
    """Diagnostic — show project resolution, API config, and a sample credential count."""
    _print(f"racred - RecallAccelerator credential broker CLI")
    _print(f"  api:           {API_BASE_URL}")
    _print(f"  api_key:       {'(set)' if API_KEY else '(not set)'}")
    _print(f"  agent_name:    {DEFAULT_IDENTITY['agent_name']}")
    _print(f"  cwd:           {Path.cwd()}")
    agents_slug = _slug_from_agents_file(Path.cwd())
    _print(f"  AGENTS slug:   {agents_slug or '(not found)'}")
    _print(f"  CWD slug:      {_slug_from_cwd()}")
    if args.project:
        _print(f"  --project:     {args.project}")
    # whoami should never fail-loud — print the diagnostic and exit 0 even if
    # resolution would normally die. Swap _die for an inline soft path.
    try:
        rows = _api_get("/api/projects")
        slug = (
            args.project.lower() if args.project
            else _slug_from_agents_file(Path.cwd()) or _slug_from_cwd()
        )
        match = next((p for p in rows if p.get("slug") == slug), None) if isinstance(rows, list) else None
        if match:
            _print(f"  -> resolved:   {slug} (#{match['id']})")
            try:
                creds = _api_get(f"/api/projects/{match['id']}/credentials")
                _print(f"  -> visible:    {len(creds)} credential(s)")
            except httpx.HTTPError as exc:
                _print(f"  -> visible:    (failed to query: {exc})")
        else:
            _print(f"  -> resolved:   (no project matches slug '{slug}')")
    except httpx.HTTPError as exc:
        _print(f"  -> resolved:   (API call failed: {exc})")
    return 0


# ---------------------------------------------------------------------------
# Output helpers — keep stdout clean for `racred get`, stderr for diagnostics
# ---------------------------------------------------------------------------


def _print(s: str) -> None:
    print(s)


def _stderr(s: str) -> None:
    sys.stderr.write(s + "\n")


def _die(msg: str, code: int = 1) -> None:
    _stderr(f"racred: {msg}")
    sys.exit(code)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="racred",
        description="RecallAccelerator credential broker CLI.",
    )
    p.add_argument("--project", help="Override project slug (default: resolved from CWD or AGENTS.md).")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("list", help="List credentials visible to the current project.")
    sp.add_argument("--category", help="Filter by category (e.g. database, api, ftp).")
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("get", help="Print a credential's value to stdout.")
    sp.add_argument("name", help="Credential name.")
    sp.set_defaults(func=cmd_get)

    sp = sub.add_parser("run", help="Run a command with all visible creds injected as env vars.")
    sp.add_argument("command", nargs=argparse.REMAINDER, help="Command and args. Use -- to separate from racred flags.")
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("whoami", help="Show project resolution + API config diagnostics.")
    sp.set_defaults(func=cmd_whoami)

    return p


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    # `racred run -- gh repo list` lands here as command=["--", "gh", "repo", "list"]
    # because argparse REMAINDER preserves the literal "--". Strip it.
    if args.cmd == "run" and args.command and args.command[0] == "--":
        args.command = args.command[1:]

    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
