"""GitHub issue/PR status tool.

Split out of server.py (Phase 0, no behaviour change).
"""

from datetime import datetime, timezone
from typing import Annotated

import httpx
from mcp.server.mcpserver import Context
from pydantic import Field

from app_core import _append_audit_entry, _logger, mcp
from github_client import GitHubAPIError, get_issue_or_pr
from logging_utils import (
    TOOL_COMPLETED,
    TOOL_ERROR,
    TOOL_STARTED,
    log_event,
    new_correlation_id,
)
from models import GitHubIssueStatus
from progress_utils import emit_progress


@mcp.tool()
async def check_github_issue_status(
    issue_number: Annotated[int, Field(ge=1)],
    *,
    ctx: Context,
) -> GitHubIssueStatus:
    """
    Answers one question: is issue/PR #<issue_number> on this project's
    own GitHub repo (HellenMuhonjaData/MeshMedic) currently open, and
    what's its title? A real GitHub REST API call, not a mock -- this is
    the one tool in this server that reads from GitHub rather than the
    local audit log or Epic's FHIR sandbox.

    `issue_number` is a strictly-typed, range-validated integer -- it can
    never contain a "/", "..", or anything else that could redirect the
    request to a different URL path. The request is only ever built from
    this validated int, never from a raw string concatenated into a path;
    see github_client.py's docstring for the same point made about its
    one outbound call.

    On a timeout, a non-2xx response, or any other failure, this returns
    `ok=false` with a plain-English `error` -- it never raises, so one bad
    call to GitHub can't take down this server's connection to its own
    client. Nothing about the request (host, headers, token) ever appears
    in that error message or in any log line this tool writes -- only the
    issue number, outcome, and duration do.
    """
    correlation_id = new_correlation_id()
    log_event(
        _logger,
        "info",
        TOOL_STARTED,
        correlation_id,
        tool="check_github_issue_status",
        issue_number=issue_number,
    )
    await emit_progress(
        ctx,
        0,
        None,
        f"Looking up GitHub issue/PR #{issue_number} (single external call, no fixed step count)",
    )
    checked_at = datetime.now(timezone.utc).isoformat()

    try:
        result = get_issue_or_pr(issue_number, correlation_id)
    except httpx.TimeoutException:
        log_event(
            _logger,
            "warning",
            TOOL_ERROR,
            correlation_id,
            tool="check_github_issue_status",
            error_class="TimeoutError",
        )
        _append_audit_entry(
            {
                "timestamp": checked_at,
                "action": "check_github_issue_status",
                "issue_number": issue_number,
                "outcome": "failure",
                "error_class": "TimeoutError",
            }
        )
        return GitHubIssueStatus(
            issue_number=issue_number,
            ok=False,
            found=None,
            state=None,
            title=None,
            is_pull_request=None,
            error="Timed out contacting GitHub.",
            checked_at=checked_at,
        )
    except GitHubAPIError:
        log_event(
            _logger,
            "warning",
            TOOL_ERROR,
            correlation_id,
            tool="check_github_issue_status",
            error_class="UpstreamUnavailable",
        )
        _append_audit_entry(
            {
                "timestamp": checked_at,
                "action": "check_github_issue_status",
                "issue_number": issue_number,
                "outcome": "failure",
                "error_class": "UpstreamUnavailable",
            }
        )
        return GitHubIssueStatus(
            issue_number=issue_number,
            ok=False,
            found=None,
            state=None,
            title=None,
            is_pull_request=None,
            error="GitHub's API did not return a usable response.",
            checked_at=checked_at,
        )
    except Exception:
        # Last-resort catch: REQ-003 is "on ANY failure, return the result,
        # never throw" -- this is the backstop for a failure mode not
        # already named above, not the primary error path.
        log_event(
            _logger,
            "error",
            TOOL_ERROR,
            correlation_id,
            tool="check_github_issue_status",
            error_class="UnexpectedError",
        )
        _append_audit_entry(
            {
                "timestamp": checked_at,
                "action": "check_github_issue_status",
                "issue_number": issue_number,
                "outcome": "failure",
                "error_class": "UnexpectedError",
            }
        )
        return GitHubIssueStatus(
            issue_number=issue_number,
            ok=False,
            found=None,
            state=None,
            title=None,
            is_pull_request=None,
            error="An unexpected error occurred.",
            checked_at=checked_at,
        )

    if not result["found"]:
        log_event(
            _logger,
            "info",
            TOOL_COMPLETED,
            correlation_id,
            tool="check_github_issue_status",
            outcome="not_found",
        )
        _append_audit_entry(
            {
                "timestamp": checked_at,
                "action": "check_github_issue_status",
                "issue_number": issue_number,
                "outcome": "success",
                "found": False,
            }
        )
        return GitHubIssueStatus(
            issue_number=issue_number,
            ok=True,
            found=False,
            state=None,
            title=None,
            is_pull_request=None,
            error=None,
            checked_at=checked_at,
        )

    data = result["data"]
    log_event(
        _logger,
        "info",
        TOOL_COMPLETED,
        correlation_id,
        tool="check_github_issue_status",
        outcome="success",
        found=True,
        state=data.get("state"),
    )
    _append_audit_entry(
        {
            "timestamp": checked_at,
            "action": "check_github_issue_status",
            "issue_number": issue_number,
            "outcome": "success",
            "found": True,
            "state": data.get("state"),
        }
    )
    return GitHubIssueStatus(
        issue_number=issue_number,
        ok=True,
        found=True,
        state=data.get("state"),
        title=data.get("title"),
        is_pull_request="pull_request" in data,
        error=None,
        checked_at=checked_at,
    )
