"""GitHub REST API client backing check_github_issue_status: answers one
question -- is issue/PR #N on this project's own repo currently open, and
what's its title? Read-only, single endpoint, real network calls.

Credential handling: the token is read fresh from the environment on
every call (never cached as a module constant, never logged, never
included in an exception message) -- an absent token degrades to an
unauthenticated request rather than failing outright, since GitHub's REST
API serves public repo data either way (just at a lower rate limit).
There is no fallback/default token anywhere in this file.
"""

import os
import time
from typing import Any

import httpx

from logging_utils import (
    EXTERNAL_CALL_FINISHED,
    EXTERNAL_CALL_STARTED,
    configure_json_logging,
    log_event,
)

_logger = configure_json_logging()

GITHUB_OWNER = "HellenMuhonjaData"
GITHUB_REPO = "MeshMedic"
GITHUB_API_BASE = "https://api.github.com"
REQUEST_TIMEOUT_SECONDS = 10.0

# One shared, reused httpx.Client -- created once at import time and kept
# for this process's lifetime, so repeated calls reuse pooled/keep-alive
# TCP connections instead of paying a fresh connection setup every time.
_http_client = httpx.Client(timeout=httpx.Timeout(REQUEST_TIMEOUT_SECONDS))


class GitHubAPIError(Exception):
    """Raised for a non-2xx GitHub response. Messages never include the
    request URL, headers, or token -- only the status code, which reveals
    nothing about this project's credentials."""


def get_issue_or_pr(issue_number: int, correlation_id: str) -> dict[str, Any]:
    """Fetch one issue/PR by number from this project's own repo.

    `issue_number` must already be a validated int by the time it reaches
    here -- the caller (check_github_issue_status in server.py) only ever
    passes a Pydantic-validated integer, never a raw string. An int has no
    "/", no "..", no way to redirect this request to a different URL path
    -- that is what "never concatenate model output into a URL path"
    means in practice for a REST path segment: constrain the type before
    it ever touches a string, don't sanitize a string after the fact.

    Returns {"found": False} on a 404 (not an error -- a real, valid
    answer to "does this exist"), or {"found": True, "data": <issue
    JSON>} on success. Raises GitHubAPIError on any other non-2xx status.
    httpx.TimeoutException is not caught here -- it propagates to the
    caller, which is the one exception type the tool layer distinguishes
    with its own stable error class.
    """
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    url = f"{GITHUB_API_BASE}/repos/{GITHUB_OWNER}/{GITHUB_REPO}/issues/{issue_number}"

    log_event(
        _logger, "info", EXTERNAL_CALL_STARTED, correlation_id,
        target="github_get_issue", issue_number=issue_number,
    )
    start = time.perf_counter()
    response: httpx.Response | None = None
    try:
        response = _http_client.get(url, headers=headers)
        duration_ms = round((time.perf_counter() - start) * 1000, 1)

        if response.status_code == 404:
            log_event(
                _logger, "info", EXTERNAL_CALL_FINISHED, correlation_id,
                target="github_get_issue", duration_ms=duration_ms,
                outcome="success", status_code=404,
            )
            return {"found": False}

        if response.status_code >= 400:
            log_event(
                _logger, "warning", EXTERNAL_CALL_FINISHED, correlation_id,
                target="github_get_issue", duration_ms=duration_ms,
                outcome="failure", status_code=response.status_code, error_class="UpstreamUnavailable",
            )
            raise GitHubAPIError(f"GitHub API rejected the request (status {response.status_code}).")

        log_event(
            _logger, "info", EXTERNAL_CALL_FINISHED, correlation_id,
            target="github_get_issue", duration_ms=duration_ms,
            outcome="success", status_code=response.status_code,
        )
        return {"found": True, "data": response.json()}
    finally:
        # Release the response back to the pooled client regardless of
        # outcome (success, 4xx, or an exception before this point) -- a
        # timeout raised by _http_client.get() itself leaves `response`
        # None, so there is nothing to release in that case.
        if response is not None:
            response.close()
