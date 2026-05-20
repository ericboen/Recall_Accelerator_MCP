"""HTTP client for the RecallAccelerator .NET API.

Wraps httpx with two things the bare client doesn't give us:

1. Error transparency (task #145). When the API returns 5xx or the connection
   times out, raise a structured :class:`RaApiError` whose ``__str__`` produces
   a human/agent-readable message containing the HTTP status, response body
   (truncated to ~500 chars), the underlying exception type, the wall-clock
   duration, and how many attempts were tried. Pre-v0.17.2 the same failure
   surfaced as "Tool result missing due to internal error" with no detail —
   the agent had no signal to reason about.

2. Retry-with-backoff on transient errors (task #146). 5xx in {502, 503, 504,
   408} + httpx.TimeoutException / ConnectError / NetworkError are retried up
   to ``max_retries`` times with exponential backoff (250ms / 500ms / 1000ms /
   2000ms) plus jitter. 4xx and 401/403 are not retried (they won't get better
   without intervention); 409 isn't either (the caller should handle the
   conflict). Honors ``Retry-After`` if present.

Defaults: writes (POST/PUT/PATCH/DELETE) get 3 retries; reads (GET) get 1.
Callers can override via the ``max_retries`` kwarg, including 0 for
no-retry. Each retry attempt is logged to stderr so a human reviewing logs
can see the burst happened.

Idempotency caveat for writes: ``create_task`` and other create-style writes
are NOT idempotent. A retried create after a successful-but-disconnected
first attempt could create a duplicate. For solo-dev scale this is a
non-issue (rare network blip → one accidental duplicate); at multi-user
scale the proper fix is client-side request IDs + server-side dedup. Filed
in #146's description as a known limitation.
"""

from __future__ import annotations

import json
import random
import sys
import time
from typing import Any

import httpx

# Status codes considered transient (worth retrying).
RETRIABLE_STATUS = {408, 502, 503, 504}


class RaApiError(Exception):
    """Structured error surfaced by :func:`api_call` when the API returns
    a non-success response or the network fails after all retries.

    Stringified, this becomes the error message FastMCP surfaces to the
    LLM agent — so :meth:`__str__` is intentionally chatty: the agent uses
    it to reason about what went wrong (retry vs give up, file a follow-up
    task vs ask the user).
    """

    def __init__(
        self,
        message: str,
        *,
        method: str | None = None,
        path: str | None = None,
        http_status: int | None = None,
        response_body: str | None = None,
        exception_type: str | None = None,
        duration_ms: int | None = None,
        attempt_count: int = 1,
    ):
        super().__init__(message)
        self.message = message
        self.method = method
        self.path = path
        self.http_status = http_status
        self.response_body = response_body
        self.exception_type = exception_type
        self.duration_ms = duration_ms
        self.attempt_count = attempt_count

    def __str__(self) -> str:
        parts = [self.message]
        if self.http_status is not None:
            parts.append(f"(HTTP {self.http_status})")
        if self.exception_type is not None and self.http_status is None:
            parts.append(f"({self.exception_type})")
        if self.attempt_count > 1:
            parts.append(f"after {self.attempt_count} attempts")
        if self.duration_ms is not None:
            parts.append(f"in {self.duration_ms}ms")
        if self.response_body:
            body = self.response_body if len(self.response_body) <= 500 else self.response_body[:500] + "…"
            parts.append(f"body: {body}")
        return " ".join(parts)


def _is_retriable(exc: BaseException) -> bool:
    """True if ``exc`` is a transient error worth retrying."""
    if isinstance(exc, (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in RETRIABLE_STATUS
    return False


def _build_error(
    exc: BaseException, method: str, path: str, attempt_count: int, duration_ms: int
) -> RaApiError:
    """Translate the final exception (after retries) into a :class:`RaApiError`."""
    http_status: int | None = None
    response_body: str | None = None
    if isinstance(exc, httpx.HTTPStatusError):
        http_status = exc.response.status_code
        try:
            response_body = exc.response.text
        except Exception:  # noqa: BLE001 — best-effort body read
            response_body = None
        msg = f"RA API {method} {path} returned HTTP {http_status}"
    elif isinstance(exc, httpx.TimeoutException):
        msg = f"RA API {method} {path} timed out"
    elif isinstance(exc, httpx.ConnectError):
        msg = f"RA API {method} {path} could not connect"
    elif isinstance(exc, httpx.NetworkError):
        msg = f"RA API {method} {path} network error"
    else:
        msg = f"RA API {method} {path} failed unexpectedly: {type(exc).__name__}"

    return RaApiError(
        message=msg,
        method=method,
        path=path,
        http_status=http_status,
        response_body=response_body,
        exception_type=type(exc).__name__,
        duration_ms=duration_ms,
        attempt_count=attempt_count,
    )


def _default_retries_for(method: str) -> int:
    """Reads tolerate one retry; writes get three."""
    return 1 if method == "GET" else 3


# Exponential-backoff schedule (seconds). attempts beyond len() use the last value.
_BACKOFF_DELAYS = (0.25, 0.5, 1.0, 2.0)


def api_call(
    method: str,
    url: str,
    *,
    json_body: dict | None = None,
    headers: dict | None = None,
    timeout: float = 60.0,
    max_retries: int | None = None,
    expect_json: bool = True,
) -> Any:
    """Send an HTTP request to the RA API with retries and structured errors.

    On success, returns the parsed JSON body (when ``expect_json=True``),
    ``{"raw": <text>}`` when the response isn't JSON, ``{}`` for empty
    bodies, or the raw text when ``expect_json=False``.

    On failure (after retries), raises :class:`RaApiError`.
    """
    if max_retries is None:
        max_retries = _default_retries_for(method)
    if headers is None:
        headers = {}

    attempt = 0
    while True:
        attempt += 1
        start = time.monotonic()
        try:
            with httpx.Client(timeout=timeout, headers=headers) as client:
                if method == "GET":
                    resp = client.get(url)
                elif method == "POST":
                    resp = client.post(url, json=json_body or {})
                elif method == "PUT":
                    resp = client.put(url, json=json_body or {})
                elif method == "PATCH":
                    resp = client.patch(url, json=json_body or {})
                elif method == "DELETE":
                    resp = client.delete(url)
                else:
                    raise ValueError(f"Unsupported HTTP method: {method}")
                resp.raise_for_status()
                if not expect_json:
                    return resp.text
                if not resp.content:
                    return {}
                try:
                    return resp.json()
                except json.JSONDecodeError:
                    return {"raw": resp.text}
        except Exception as exc:  # noqa: BLE001 — we intentionally catch everything to retry/repackage
            duration_ms = int((time.monotonic() - start) * 1000)
            # Non-retriable OR retries exhausted → raise structured error.
            if not _is_retriable(exc) or attempt > max_retries:
                raise _build_error(exc, method, url, attempt, duration_ms) from exc

            # Compute backoff. Honor Retry-After if present on 5xx.
            delay = _BACKOFF_DELAYS[min(attempt - 1, len(_BACKOFF_DELAYS) - 1)]
            if isinstance(exc, httpx.HTTPStatusError):
                ra = exc.response.headers.get("Retry-After")
                if ra:
                    try:
                        delay = max(delay, float(ra))
                    except ValueError:
                        pass
            delay_with_jitter = delay + random.uniform(0, delay * 0.1)
            status_part = (
                f" HTTP {exc.response.status_code}"  # type: ignore[attr-defined]
                if isinstance(exc, httpx.HTTPStatusError)
                else ""
            )
            sys.stderr.write(
                f"[RA-MCP retry] {method} {url}: attempt {attempt} failed "
                f"({type(exc).__name__}{status_part}); retrying in {delay_with_jitter:.2f}s\n"
            )
            sys.stderr.flush()
            time.sleep(delay_with_jitter)
