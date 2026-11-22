"""Shared MCP server instance, logger, constants and the audit-trail writer.

Split out of server.py (Phase 0, no behaviour change).
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

import audit_actor
from logging_utils import (
    TOOL_ERROR,
    configure_json_logging,
    log_event,
    new_correlation_id,
)

mcp = MCPServer("meshmedic")

# Without declaring the logging capability, a client is free to silently
# drop every notifications/message this server sends -- that's the whole
# reason to declare it up front, not an afterthought. This build can't:
# the installed mcp SDK (2.1.1) deprecated the protocol logging capability
# (SEP-2577, 2026-07-28) and MCPServer never threaded on_set_logging_level
# through to the underlying lowlevel Server in the first place, so there is
# no supported call on MCPServer's public API that declares it (confirmed
# by inspecting MCPServer.__init__'s signature directly -- the parameter
# doesn't exist to pass). Reaching into MCPServer._lowlevel_server (a
# private attribute) to hand-register a handler was considered and
# rejected: it would fight the SDK's own deprecation of the entire
# capability for a channel (notifications/message) this file doesn't use
# anyway. Every tool/external-call/access-denied/error boundary below is
# logged as structured JSON on stderr instead (see logging_utils.py) --
# stderr from a stdio-launched server is what every MCP client already
# captures, unconditionally, with no capability negotiation involved, so
# these lines are never silently dropped the way an undeclared
# notifications/message would be.
_logger = configure_json_logging()

AUDIT_LOG_PATH = Path(__file__).parent / "audit_log.jsonl"

# Below this, generate_encounter_note flags the note as low-confidence (REQ-008).
CONFIDENCE_THRESHOLD = 0.7

# REQ-012 target: documentation review time (note-draft creation to clinician
# decision) should be under 2 minutes. Above this, approve/reject_encounter_note
# surface suggestions to speed up the review (STORY-009).
REVIEW_TIME_TARGET_SECONDS = 120.0

# Fixed, clinician-facing tips returned when a review exceeds
# REVIEW_TIME_TARGET_SECONDS. Generic on purpose -- this server has no signal
# for *why* a specific review ran long, so these name the shortcuts this
# server's own tools already provide rather than guessing a per-note cause.
SPEED_UP_SUGGESTIONS = [
    "Review only the sections flagged as low-confidence instead of re-reading the entire note.",
    "Use request_citation to jump straight to the supporting transcript excerpt instead of searching the full transcript by hand.",
    "Make inline corrections via edit_encounter_note rather than rewriting the note from scratch.",
    "Address newly identified care gaps in a follow-up pass instead of resolving them before approving the note.",
]

# read_transcript_file's cap, generous for a text transcript but bounded so a
# huge file can't be read into the response wholesale.
MAX_TRANSCRIPT_FILE_BYTES = 200_000

# prioritize_care_gaps's sampling request. Short and generic on purpose --
# no model name or API key belongs here; the client's own LLM handles both.
PRIORITIZE_GAPS_SYSTEM_PROMPT = (
    "You are helping a clinician triage a list of already-identified care "
    "gaps for one patient. You are not given the chart, only the gap "
    "descriptions below -- reason only from those. Rank them most urgent "
    "to least urgent and give one short reason for each. This is a "
    "suggestion for the clinician to review, not a decision."
)
PRIORITIZE_GAPS_MAX_TOKENS = 400


def _append_audit_entry(entry: dict) -> None:
    """Local stand-in for a real audit trail (REQ-006). Not HIPAA-grade --
    a production audit log needs its own access controls and durability
    guarantees, which this demo file does not provide.

    STORY-010's "system fails to log incidents" failure path: if this write
    itself fails (disk full, permission denied, the path removed underneath
    the process), that is never swallowed and never allowed to look like the
    entry was written when it wasn't. Every tool in this file treats
    audit_log.jsonl as the single source of truth (see _find_note and its
    siblings, which reconstruct state entirely by replaying it) -- a caller
    that returned a normal result after a failed write would silently
    corrupt that guarantee, making REQ-006's "auditable history" a lie for
    that action. So this logs the failure with a stable error_class to the
    structured JSON stream (the fallback channel -- stderr, not the file
    that just failed to write) and re-raises as ToolError, aborting the
    calling tool rather than reporting an action that was never actually
    recorded.
    """
    # Every entry says who acted (audit_actor.py). Callers that know set it;
    # anything else is a person acting without an authenticated identity.
    entry.setdefault("actor", dict(audit_actor.UNVERIFIED))
    try:
        with AUDIT_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError as e:
        log_event(
            _logger,
            "error",
            TOOL_ERROR,
            new_correlation_id(),
            tool="_append_audit_entry",
            action=entry.get("action"),
            error_class="AuditLogWriteFailure",
        )
        raise ToolError(
            "Failed to write to the audit trail; this action was not recorded and cannot be treated as completed."
        ) from e


def _write_audit_entry(
    ehr_system: str,
    mrn: str | None,
    last_name: str | None,
    date_of_birth: str | None,
    match_count: int,
) -> None:
    _append_audit_entry(
        {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "action": "search_ehr_patient",
            "ehr_system": ehr_system,
            "search_key": "mrn" if mrn else "last_name+date_of_birth",
            "mrn": mrn,
            "last_name": last_name,
            "date_of_birth": date_of_birth,
            "match_count": match_count,
        }
    )
