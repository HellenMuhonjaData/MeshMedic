import ast
import json
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal

import httpx
from mcp.server.mcpserver import Context, ListRoots, MCPServer, Resolve, Sample
from mcp.server.mcpserver.exceptions import ResourceNotFoundError, ToolError
from mcp_types import CreateMessageResult, ListRootsResult, SamplingMessage, TextContent
from pydantic import BaseModel, Field

from epic_fhir_client import EpicFHIRError, fetch_patient
from github_client import GitHubAPIError, get_issue_or_pr
from logging_utils import (
    ACCESS_DENIED,
    SAMPLING_REQUEST_FINISHED,
    SAMPLING_REQUEST_STARTED,
    SERVER_STARTED,
    TOOL_COMPLETED,
    TOOL_ERROR,
    TOOL_STARTED,
    configure_json_logging,
    log_event,
    new_correlation_id,
)
from progress_utils import emit_progress
from roots_guard import resolve_within_roots
from sample_patients import SAMPLE_PATIENTS

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

# Bridges a sampling round-trip's start time (and the correlation id logged
# at its start) from the resolver that requests it to the tool body that
# receives the result -- the two are genuinely separate function
# invocations in this SDK's resolver model (see _list_open_gaps_resolver's
# docstring), so there is no other channel between them. Keyed by
# ctx.request_id (unique per in-flight tools/call), popped by the tool body
# on the normal path. If resolution fails before the tool body ever runs
# (e.g. the client's model refuses -- see prioritize_care_gaps's docstring
# for why that specific failure can't be caught here), the entry is never
# popped; this is a small, bounded leak on a rare path, not an unbounded one.
_sampling_calls: dict[str, tuple[str, float]] = {}


class PatientMatch(BaseModel):
    mrn: str
    first_name: str
    last_name: str
    date_of_birth: str
    ehr_system: str
    patient_id: str


class SearchResult(BaseModel):
    matches: list[PatientMatch]
    count: int
    message: str


class GeneratedNote(BaseModel):
    note_id: str
    patient_id: str
    ehr_system: str
    status: Literal["draft"]
    note_text: str
    generated_at: str
    confidence: float
    flagged: bool
    warning: str | None


class ReviewedNote(BaseModel):
    note_id: str
    patient_id: str
    ehr_system: str
    status: Literal["approved", "rejected"]
    note_text: str
    feedback: str | None
    reviewed_at: str
    documentation_time_seconds: float
    exceeded_target: bool
    suggestions: list[str] | None


class EditedNote(BaseModel):
    note_id: str
    patient_id: str
    ehr_system: str
    status: Literal["draft"]
    note_text: str
    edited_at: str


class CodeSuggestion(BaseModel):
    code: Annotated[str, Field(min_length=1, max_length=20)]
    code_system: Literal["icd-10", "cpt"]
    description: Annotated[str, Field(min_length=1, max_length=300)]


class SuggestedCodes(BaseModel):
    suggestion_id: str
    note_id: str
    patient_id: str
    ehr_system: str
    status: Literal["draft"]
    codes: list[CodeSuggestion]
    confidence: float
    flagged: bool
    warning: str | None
    suggested_at: str


class ReviewedCodes(BaseModel):
    suggestion_id: str
    note_id: str
    patient_id: str
    ehr_system: str
    status: Literal["approved", "rejected"]
    codes: list[CodeSuggestion]
    feedback: str | None
    reviewed_at: str


class CareGapInput(BaseModel):
    description: Annotated[str, Field(min_length=1, max_length=300)]
    confidence: Annotated[float, Field(ge=0.0, le=1.0)]
    confidence_reason: Annotated[str | None, Field(max_length=1000)] = None


class CareGap(BaseModel):
    gap_id: str
    note_id: str
    patient_id: str
    ehr_system: str
    status: Literal["flagged"]
    description: str
    confidence: float
    flagged: bool
    warning: str | None
    identified_at: str


class AddressedCareGap(BaseModel):
    gap_id: str
    note_id: str
    patient_id: str
    ehr_system: str
    status: Literal["addressed"]
    description: str
    resolution: str
    addressed_at: str


class OpenCareGapSummary(BaseModel):
    gap_id: str
    description: str


class CareGapPriorityResult(BaseModel):
    patient_id: str
    gap_count: int
    open_gaps: list[OpenCareGapSummary]
    degraded: bool
    degraded_reason: str | None
    ranking_text: str | None
    prioritized_at: str


class GitHubIssueStatus(BaseModel):
    issue_number: int
    ok: bool
    found: bool | None
    state: str | None
    title: str | None
    is_pull_request: bool | None
    error: str | None
    checked_at: str


class TranscriptFileRead(BaseModel):
    file_path: str
    denied: bool
    transcript: str | None
    error: str | None
    read_at: str


class CitationResult(BaseModel):
    note_id: str
    patient_id: str
    ehr_system: str
    claimed_excerpt: str
    found: bool
    matched_text: str | None
    start_offset: int | None
    end_offset: int | None
    explanation: str | None
    requested_at: str


class ComplianceControlResult(BaseModel):
    control_id: str
    description: str
    passed: bool
    detail: str


class ComplianceCheckResult(BaseModel):
    checked_at: str
    controls: list[ComplianceControlResult]
    all_passed: bool


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
    try:
        with AUDIT_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError as e:
        log_event(
            _logger, "error", TOOL_ERROR, new_correlation_id(),
            tool="_append_audit_entry", action=entry.get("action"), error_class="AuditLogWriteFailure",
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


@mcp.tool()
async def search_ehr_patient(
    ehr_system: Literal["epic", "oracle_health"],
    mrn: Annotated[str | None, Field(min_length=3, max_length=20)] = None,
    last_name: Annotated[str | None, Field(min_length=1, max_length=50)] = None,
    date_of_birth: Annotated[str | None, Field(pattern=r"^\d{4}-\d{2}-\d{2}$")] = None,
    max_results: Annotated[int, Field(ge=1, le=5)] = 5,
    *,
    ctx: Context,
) -> SearchResult:
    """
    Use this to confirm a patient's identity in the EHR when you don't already
    have a confirmed patient_id -- for example, an encounter transcript names a
    patient but doesn't include their MRN, or you're not sure whether a name
    mentioned matches an existing record. Search by MRN if you have it;
    otherwise provide the patient's last name together with their date of
    birth. Do not use this to pull chart contents, problem lists, or clinical
    history -- it only returns identity-matching candidates so you can confirm
    who you're working with before requesting anything else.
    """
    correlation_id = new_correlation_id()
    log_event(
        _logger, "info", TOOL_STARTED, correlation_id,
        tool="search_ehr_patient", ehr_system=ehr_system,
        search_key="mrn" if mrn else ("last_name+date_of_birth" if last_name else "none"),
    )
    have_mrn = bool(mrn)
    have_name_dob = bool(last_name) and bool(date_of_birth)

    if not have_mrn and not have_name_dob:
        _write_audit_entry(ehr_system, mrn, last_name, date_of_birth, 0)
        log_event(
            _logger, "info", TOOL_COMPLETED, correlation_id,
            tool="search_ehr_patient", outcome="success", match_count=0,
        )
        return SearchResult(
            matches=[],
            count=0,
            message=(
                "No usable search key provided. Supply either 'mrn', or both "
                "'last_name' and 'date_of_birth'."
            ),
        )

    candidates = [p for p in SAMPLE_PATIENTS if p["ehr_system"] == ehr_system]
    total = len(candidates)

    found = []
    for position, candidate in enumerate(candidates, start=1):
        await emit_progress(
            ctx, position, total, f"Checking candidate {position} of {total} (MRN {candidate['mrn']})"
        )
        if have_mrn:
            if candidate["mrn"].lower() == mrn.lower():
                found.append(candidate)
        elif candidate["last_name"].lower() == last_name.lower() and candidate["date_of_birth"] == date_of_birth:
            found.append(candidate)

    _write_audit_entry(ehr_system, mrn, last_name, date_of_birth, len(found))

    if not found:
        log_event(
            _logger, "info", TOOL_COMPLETED, correlation_id,
            tool="search_ehr_patient", outcome="success", match_count=0,
        )
        return SearchResult(
            matches=[],
            count=0,
            message=f"No patient found in {ehr_system} matching the given search key.",
        )

    trimmed = found[:max_results]
    log_event(
        _logger, "info", TOOL_COMPLETED, correlation_id,
        tool="search_ehr_patient", outcome="success", match_count=len(trimmed),
    )
    return SearchResult(
        matches=[PatientMatch(**p) for p in trimmed],
        count=len(trimmed),
        message="",
    )


def _find_patient(ehr_system: str, patient_id: str) -> dict | None:
    for p in SAMPLE_PATIENTS:
        if p["ehr_system"] == ehr_system and p["patient_id"] == patient_id:
            return p
    return None


@mcp.resource(
    "ehr://{ehr_system}/patient/{patient_id}/chart",
    name="patient-chart",
    mime_type="application/fhir+json",
)
def get_patient_chart(ehr_system: str, patient_id: str) -> dict:
    """Read-only patient chart, addressed by EHR system and patient_id (as
    returned in search_ehr_patient's matches). Currently demographics only --
    sample_patients.py has no problem-list/condition data yet, so this is a
    minimal FHIR Patient resource, not a full chart bundle."""
    patient = _find_patient(ehr_system, patient_id)
    if patient is None:
        raise ResourceNotFoundError(
            f"No chart found for patient_id={patient_id!r} in ehr_system={ehr_system!r}"
        )
    return {
        "resourceType": "Patient",
        "id": patient["patient_id"],
        "identifier": [{"system": "urn:meshmedic:mrn", "value": patient["mrn"]}],
        "name": [{"family": patient["last_name"], "given": [patient["first_name"]]}],
        "birthDate": patient["date_of_birth"],
        "managingOrganization": {"display": ehr_system},
    }


@mcp.tool()
async def fetch_patient_from_ehr(
    ehr_system: Literal["epic", "oracle_health"],
    fhir_patient_id: Annotated[str, Field(min_length=1)],
    *,
    ctx: Context,
) -> dict:
    """
    Retrieve a patient's FHIR resource directly from a real EHR system's FHIR
    API (REQ-009), logging every retrieval attempt in the audit trail
    regardless of outcome (REQ-006). Same interface across EHR systems
    (REQ-010: EHR-agnostic) -- only `ehr_system` changes which backend is
    called.

    `fhir_patient_id` must be the EHR's own FHIR-assigned patient ID (not an
    MRN, and not one of this server's local sample_patients.py IDs -- those
    are a separate, unrelated ID space used by search_ehr_patient /
    generate_encounter_note for the note-review walking skeleton).

    Only Epic has a real sandbox connection in this build. Oracle Health/
    Cerner is not connected -- this raises ToolError rather than returning
    fabricated data, consistent with this project's rule against showing a
    result the system hasn't actually produced.

    This is a real network call (JWT-signed OAuth2 token exchange, then a
    FHIR fetch, both against Epic's live sandbox) and can genuinely take a
    couple of seconds, so it reports progress -- but only one tick, with no
    total: `fetch_patient()` in epic_fhir_client.py performs its own two
    HTTP calls internally as a single opaque unit from this tool's
    perspective, so there's no real, already-known step count to report
    against here without restructuring that module's public API purely to
    serve this. A single "no total" progress notification (REQ pattern:
    never invent a fake percentage) is the honest signal that real network
    work is in flight.
    """
    correlation_id = new_correlation_id()
    log_event(
        _logger, "info", TOOL_STARTED, correlation_id,
        tool="fetch_patient_from_ehr", ehr_system=ehr_system,
    )
    timestamp = datetime.now(timezone.utc).isoformat()

    if ehr_system == "oracle_health":
        _append_audit_entry(
            {
                "timestamp": timestamp,
                "action": "fetch_ehr_patient",
                "ehr_system": ehr_system,
                "fhir_patient_id": fhir_patient_id,
                "outcome": "failure",
                "error_class": "UpstreamUnavailable",
            }
        )
        log_event(
            _logger, "warning", ACCESS_DENIED, correlation_id,
            tool="fetch_patient_from_ehr", ehr_system=ehr_system,
            reason="ehr_system_not_connected", error_class="AccessDenied",
        )
        raise ToolError(
            "oracle_health has no real FHIR sandbox connected in this build -- "
            "only epic is wired up."
        )

    await emit_progress(
        ctx, 0, None,
        f"Contacting {ehr_system}'s FHIR sandbox for patient {fhir_patient_id} (token exchange + fetch, no fixed step count)",
    )
    try:
        patient = fetch_patient(fhir_patient_id, correlation_id)
    except EpicFHIRError as e:
        _append_audit_entry(
            {
                "timestamp": timestamp,
                "action": "fetch_ehr_patient",
                "ehr_system": ehr_system,
                "fhir_patient_id": fhir_patient_id,
                "outcome": "failure",
                "error_class": "UpstreamUnavailable",
            }
        )
        log_event(
            _logger, "error", TOOL_ERROR, correlation_id,
            tool="fetch_patient_from_ehr", ehr_system=ehr_system,
            error_class="UpstreamUnavailable",
        )
        raise ToolError(f"Could not retrieve patient from Epic: {e}") from e

    if patient is None:
        _append_audit_entry(
            {
                "timestamp": timestamp,
                "action": "fetch_ehr_patient",
                "ehr_system": ehr_system,
                "fhir_patient_id": fhir_patient_id,
                "outcome": "failure",
                "error_class": "ResourceNotFoundError",
            }
        )
        log_event(
            _logger, "warning", TOOL_ERROR, correlation_id,
            tool="fetch_patient_from_ehr", ehr_system=ehr_system,
            error_class="ResourceNotFoundError",
        )
        raise ResourceNotFoundError(
            f"No patient found in {ehr_system} for fhir_patient_id={fhir_patient_id!r}."
        )

    _append_audit_entry(
        {
            "timestamp": timestamp,
            "action": "fetch_ehr_patient",
            "ehr_system": ehr_system,
            "fhir_patient_id": fhir_patient_id,
            "outcome": "success",
        }
    )
    log_event(
        _logger, "info", TOOL_COMPLETED, correlation_id,
        tool="fetch_patient_from_ehr", ehr_system=ehr_system, outcome="success",
    )
    return patient


def _list_client_roots() -> ListRoots:
    """Resolver body for the `roots` parameter below: returns the marker that
    tells the framework to send a `roots/list` request to the connected
    client and inject the `ListRootsResult` it answers with. See
    roots_guard.py for how that result is used."""
    return ListRoots()


@mcp.tool()
def read_transcript_file(
    file_path: Annotated[str, Field(min_length=1, max_length=1000)],
    roots: Annotated[ListRootsResult, Resolve(_list_client_roots)],
) -> TranscriptFileRead:
    """
    Load an encounter transcript from a local file on disk, so a clinician
    can point at a transcript file instead of pasting its text inline
    before calling generate_encounter_note.

    This is the one tool in this server that touches a filesystem path the
    caller supplies, so it is roots-enforced: `roots` is not something you
    (the calling model) pass -- it is filled automatically by asking the
    connected client for its declared roots (`roots/list`), and `file_path`
    is only served if its RESOLVED, real on-disk location (symlinks
    followed, ".." collapsed) falls inside one of those roots. A path
    outside every declared root is denied even if it looks like it's under
    an allowed directory as a raw string -- see roots_guard.py's module
    docstring for concrete examples of why a string check alone would not
    be a real boundary.

    A denial does not raise: it comes back as a normal result with
    `denied=true` and `transcript=None`, so you see the outcome and can
    decide what to do next (e.g. tell the clinician the path isn't
    reachable) instead of the call just failing. Every attempt -- allowed
    or denied -- is logged to the structured JSON stderr log (denials at
    warning level with the requested path) and to this server's
    audit_log.jsonl (REQ-006), same as every other tool here. A denial's
    audit entry additionally carries `security_incident: true` (REQ-011 /
    STORY-010) -- this is the one real unauthorized-access boundary this
    server enforces, so it's tagged explicitly rather than left for a
    reviewer (or run_compliance_check) to infer from `error_class` alone.
    """
    correlation_id = new_correlation_id()
    log_event(
        _logger, "info", TOOL_STARTED, correlation_id,
        tool="read_transcript_file",
    )

    resolved = resolve_within_roots(
        file_path, roots, logger=_logger, correlation_id=correlation_id, tool="read_transcript_file",
    )
    read_at = datetime.now(timezone.utc).isoformat()

    if resolved is None:
        _append_audit_entry(
            {
                "timestamp": read_at,
                "action": "read_transcript_file",
                "requested_path": file_path,
                "outcome": "failure",
                "error_class": "AccessDenied",
                "security_incident": True,
            }
        )
        log_event(
            _logger, "info", TOOL_COMPLETED, correlation_id,
            tool="read_transcript_file", outcome="denied",
        )
        return TranscriptFileRead(
            file_path=file_path,
            denied=True,
            transcript=None,
            error="Access denied: this path is outside every root the client declared.",
            read_at=read_at,
        )

    if not resolved.is_file():
        log_event(
            _logger, "warning", TOOL_ERROR, correlation_id,
            tool="read_transcript_file", error_class="ResourceNotFoundError",
        )
        raise ResourceNotFoundError(f"No file found at the resolved path for {file_path!r}.")

    size = resolved.stat().st_size
    if size > MAX_TRANSCRIPT_FILE_BYTES:
        log_event(
            _logger, "warning", TOOL_ERROR, correlation_id,
            tool="read_transcript_file", error_class="ValidationError",
        )
        raise ToolError(f"File is {size} bytes, over this tool's {MAX_TRANSCRIPT_FILE_BYTES}-byte limit.")

    try:
        text = resolved.read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        log_event(
            _logger, "warning", TOOL_ERROR, correlation_id,
            tool="read_transcript_file", error_class="ValidationError",
        )
        raise ToolError("File is not valid UTF-8 text.") from e

    _append_audit_entry(
        {
            "timestamp": read_at,
            "action": "read_transcript_file",
            "requested_path": file_path,
            "outcome": "success",
        }
    )
    log_event(
        _logger, "info", TOOL_COMPLETED, correlation_id,
        tool="read_transcript_file", outcome="success", byte_count=len(text.encode("utf-8")),
    )
    return TranscriptFileRead(
        file_path=file_path,
        denied=False,
        transcript=text,
        error=None,
        read_at=read_at,
    )


@mcp.tool()
def generate_encounter_note(
    ehr_system: Literal["epic", "oracle_health"],
    patient_id: Annotated[str, Field(min_length=1)],
    transcript: Annotated[str, Field(min_length=1, max_length=20000)],
    note_text: Annotated[str, Field(min_length=1, max_length=5000)],
    confidence: Annotated[float, Field(ge=0.0, le=1.0)],
    confidence_reason: Annotated[str | None, Field(max_length=1000)] = None,
) -> GeneratedNote:
    """
    Record an AI-drafted encounter note for a confirmed patient so it can later
    be approved or rejected (REQ-005 / REQ-014) and traced in the audit trail
    (REQ-006). Call this only after search_ehr_patient has resolved exactly one
    patient_id -- do not invent a patient_id from the transcript alone.

    You (the calling model) compose `note_text` yourself, grounded in the
    patient-chart resource and `transcript`, following the structure and
    guardrails in the prepare-encounter-note prompt. This tool does not draft
    the note -- it persists your draft and writes the audit entry. `transcript`
    is kept alongside the note so the audit trail can later show what the note
    was grounded in.

    You also assess your own `confidence` (0.0-1.0) in how well the transcript
    and chart support this note. Below the low-confidence threshold, this tool
    flags the note (REQ-008) and requires `confidence_reason` -- a flag with no
    explanation is refused (ToolError), since REQ-008 requires a warning *and*
    an explanation. There is no UI in this build, so the flag surfaces as a
    `warning` string in the returned note -- relay it to the clinician
    yourself. A flagged note still returns status="draft" like any other; the
    clinician can manually correct it via edit_encounter_note (STORY-002)
    before deciding to approve or reject.

    The returned note is always status="draft": it is not clinical
    documentation until a clinician reviews and approves it.
    """
    correlation_id = new_correlation_id()
    log_event(
        _logger, "info", TOOL_STARTED, correlation_id,
        tool="generate_encounter_note", ehr_system=ehr_system, patient_id=patient_id,
    )

    if _find_patient(ehr_system, patient_id) is None:
        log_event(
            _logger, "warning", TOOL_ERROR, correlation_id,
            tool="generate_encounter_note", error_class="ResourceNotFoundError",
        )
        raise ResourceNotFoundError(
            f"No patient found for patient_id={patient_id!r} in ehr_system={ehr_system!r}; "
            "confirm identity with search_ehr_patient before generating a note."
        )

    flagged = confidence < CONFIDENCE_THRESHOLD
    if flagged and not confidence_reason:
        log_event(
            _logger, "warning", TOOL_ERROR, correlation_id,
            tool="generate_encounter_note", error_class="ValidationError",
        )
        raise ToolError(
            f"confidence={confidence!r} is below the low-confidence threshold "
            f"({CONFIDENCE_THRESHOLD}); confidence_reason is required so the "
            "warning carries an explanation (REQ-008)."
        )

    note_id = str(uuid.uuid4())
    generated_at = datetime.now(timezone.utc).isoformat()
    warning = f"Low confidence ({confidence:.2f}): {confidence_reason}" if flagged else None

    _append_audit_entry(
        {
            "timestamp": generated_at,
            "action": "generate_note",
            "note_id": note_id,
            "ehr_system": ehr_system,
            "patient_id": patient_id,
            "transcript": transcript,
            "note_text": note_text,
            "confidence": confidence,
            "flagged": flagged,
            "confidence_reason": confidence_reason,
            "status": "draft",
        }
    )
    log_event(
        _logger, "info", TOOL_COMPLETED, correlation_id,
        tool="generate_encounter_note", outcome="success", note_id=note_id, flagged=flagged,
    )

    return GeneratedNote(
        note_id=note_id,
        patient_id=patient_id,
        ehr_system=ehr_system,
        status="draft",
        note_text=note_text,
        generated_at=generated_at,
        confidence=confidence,
        flagged=flagged,
        warning=warning,
    )


def _find_note(note_id: str) -> dict | None:
    """Reconstruct a note's current state by replaying the audit log --
    the audit trail is the single source of truth for note text/status
    (REQ-006), so no separate in-memory store can drift from what it says
    happened."""
    if not AUDIT_LOG_PATH.exists():
        return None
    generated = None
    latest_edit = None
    decision = None
    with AUDIT_LOG_PATH.open(encoding="utf-8") as f:
        for line in f:
            entry = json.loads(line)
            if entry.get("note_id") != note_id:
                continue
            if entry["action"] == "generate_note":
                generated = entry
            elif entry["action"] == "edit_note":
                latest_edit = entry
            elif entry["action"] in ("approve_note", "reject_note"):
                decision = entry
    if generated is None:
        return None
    return {"generated": generated, "latest_edit": latest_edit, "decision": decision}


def _current_note_text(record: dict) -> str:
    """The note's text as a clinician would see it right now: the latest
    saved edit if one exists, otherwise the AI's original draft."""
    if record["latest_edit"] is not None:
        return record["latest_edit"]["note_text"]
    return record["generated"]["note_text"]


def _documentation_time_seconds(generated_at: str, reviewed_at: str) -> float:
    """Elapsed time between note-draft creation and a review decision
    (REQ-012's calculation: "draft creation to clinician approval"), from two
    audit-trail timestamps -- both always UTC ISO-8601, written by this same
    file, so no timezone-normalization is needed here."""
    start = datetime.fromisoformat(generated_at)
    end = datetime.fromisoformat(reviewed_at)
    return (end - start).total_seconds()


def _speed_up_suggestions(documentation_time_seconds: float) -> list[str] | None:
    """None once the review met the REQ-012 target; the fixed tip list once
    it didn't. A pure function of the already-computed duration so a replayed
    no-op decision (see approve/reject_encounter_note) reproduces the same
    suggestions without re-deriving them from anything time-dependent."""
    if documentation_time_seconds <= REVIEW_TIME_TARGET_SECONDS:
        return None
    return list(SPEED_UP_SUGGESTIONS)


@mcp.tool()
def edit_encounter_note(
    note_id: Annotated[str, Field(min_length=1)],
    edited_note_text: Annotated[str, Field(min_length=1, max_length=5000)],
) -> EditedNote:
    """
    Save a clinician's edit to a draft note while they're still reviewing
    it, before they decide to approve or reject (REQ-005's "review, edit,
    approve/reject"), so the audit trail shows the edit as its own action
    distinct from that later decision (REQ-006). Call this only for a
    note_id returned by generate_encounter_note.

    A later approve_encounter_note or reject_encounter_note call picks up
    this saved edit as the note's current text automatically -- you do not
    need to pass the edited text again at decision time.

    Saving an edit identical to the note's current text is a no-op that
    returns the existing state -- it does not write a duplicate audit
    entry. Editing a note that already has a recorded decision raises
    ToolError: once approved or rejected, a note's record is closed.
    """
    correlation_id = new_correlation_id()
    log_event(
        _logger, "info", TOOL_STARTED, correlation_id,
        tool="edit_encounter_note", note_id=note_id,
    )

    record = _find_note(note_id)
    if record is None:
        log_event(
            _logger, "warning", TOOL_ERROR, correlation_id,
            tool="edit_encounter_note", error_class="ResourceNotFoundError",
        )
        raise ResourceNotFoundError(f"No generated note found for note_id={note_id!r}.")

    if record["decision"] is not None:
        log_event(
            _logger, "warning", ACCESS_DENIED, correlation_id,
            tool="edit_encounter_note", note_id=note_id, reason="note_locked_after_decision",
            error_class="AccessDenied",
        )
        raise ToolError(
            f"note_id={note_id!r} already has a recorded decision "
            f"({record['decision']['action']}); it can no longer be edited."
        )

    generated = record["generated"]
    current_entry = record["latest_edit"] or generated
    if current_entry["note_text"] == edited_note_text:
        log_event(
            _logger, "info", TOOL_COMPLETED, correlation_id,
            tool="edit_encounter_note", note_id=note_id, outcome="success", no_op=True,
        )
        return EditedNote(
            note_id=note_id,
            patient_id=current_entry["patient_id"],
            ehr_system=current_entry["ehr_system"],
            status="draft",
            note_text=edited_note_text,
            edited_at=current_entry["timestamp"],
        )

    edited_at = datetime.now(timezone.utc).isoformat()
    _append_audit_entry(
        {
            "timestamp": edited_at,
            "action": "edit_note",
            "note_id": note_id,
            "ehr_system": generated["ehr_system"],
            "patient_id": generated["patient_id"],
            "note_text": edited_note_text,
            "status": "draft",
        }
    )
    log_event(
        _logger, "info", TOOL_COMPLETED, correlation_id,
        tool="edit_encounter_note", note_id=note_id, outcome="success", no_op=False,
    )
    return EditedNote(
        note_id=note_id,
        patient_id=generated["patient_id"],
        ehr_system=generated["ehr_system"],
        status="draft",
        note_text=edited_note_text,
        edited_at=edited_at,
    )


@mcp.tool()
def approve_encounter_note(
    note_id: Annotated[str, Field(min_length=1)],
    edited_note_text: Annotated[str | None, Field(max_length=5000)] = None,
) -> ReviewedNote:
    """
    Record a clinician's approval of a previously generated draft note
    (REQ-005 / REQ-014), so the audit trail shows both the AI's note and
    the clinician's decision (REQ-006). Call this only for a note_id
    returned by generate_encounter_note.

    Pass `edited_note_text` if the clinician is changing the draft at the
    moment of approval; otherwise any edit already saved via
    edit_encounter_note is used automatically, falling back to the AI's
    original draft if there was none.

    Approving an already-approved note with the same edited_note_text is a
    no-op that returns the existing decision -- it does not write a second
    audit entry. Approving a note that was already rejected (or vice
    versa) raises ToolError: a note gets exactly one clinician decision.

    The returned result also carries `documentation_time_seconds` -- the
    elapsed time from note-draft creation to this decision (REQ-012) -- plus
    `exceeded_target` and, when the review ran over the 2-minute target,
    `suggestions` for speeding up the next one (STORY-009). This is logged to
    the audit trail regardless of outcome, same as every other field here.
    """
    correlation_id = new_correlation_id()
    log_event(
        _logger, "info", TOOL_STARTED, correlation_id,
        tool="approve_encounter_note", note_id=note_id,
    )

    record = _find_note(note_id)
    if record is None:
        log_event(
            _logger, "warning", TOOL_ERROR, correlation_id,
            tool="approve_encounter_note", error_class="ResourceNotFoundError",
        )
        raise ResourceNotFoundError(f"No generated note found for note_id={note_id!r}.")

    generated = record["generated"]
    decision = record["decision"]
    final_text = edited_note_text if edited_note_text is not None else _current_note_text(record)

    if decision is not None:
        if decision["action"] == "approve_note" and decision["note_text"] == final_text:
            documentation_time_seconds = decision.get(
                "documentation_time_seconds",
                _documentation_time_seconds(generated["timestamp"], decision["timestamp"]),
            )
            log_event(
                _logger, "info", TOOL_COMPLETED, correlation_id,
                tool="approve_encounter_note", note_id=note_id, outcome="success", no_op=True,
                documentation_time_seconds=documentation_time_seconds,
            )
            return ReviewedNote(
                note_id=note_id,
                patient_id=decision["patient_id"],
                ehr_system=decision["ehr_system"],
                status="approved",
                note_text=decision["note_text"],
                feedback=None,
                reviewed_at=decision["timestamp"],
                documentation_time_seconds=documentation_time_seconds,
                exceeded_target=documentation_time_seconds > REVIEW_TIME_TARGET_SECONDS,
                suggestions=_speed_up_suggestions(documentation_time_seconds),
            )
        log_event(
            _logger, "warning", ACCESS_DENIED, correlation_id,
            tool="approve_encounter_note", note_id=note_id, reason="conflicting_decision",
            error_class="AccessDenied",
        )
        raise ToolError(
            f"note_id={note_id!r} already has a recorded decision ({decision['action']}); "
            "a note gets exactly one clinician decision."
        )

    reviewed_at = datetime.now(timezone.utc).isoformat()
    documentation_time_seconds = _documentation_time_seconds(generated["timestamp"], reviewed_at)
    exceeded_target = documentation_time_seconds > REVIEW_TIME_TARGET_SECONDS
    suggestions = _speed_up_suggestions(documentation_time_seconds)
    _append_audit_entry(
        {
            "timestamp": reviewed_at,
            "action": "approve_note",
            "note_id": note_id,
            "ehr_system": generated["ehr_system"],
            "patient_id": generated["patient_id"],
            "note_text": final_text,
            "status": "approved",
            "documentation_time_seconds": documentation_time_seconds,
        }
    )
    log_event(
        _logger, "info", TOOL_COMPLETED, correlation_id,
        tool="approve_encounter_note", note_id=note_id, outcome="success", no_op=False,
        documentation_time_seconds=documentation_time_seconds, exceeded_target=exceeded_target,
    )
    return ReviewedNote(
        note_id=note_id,
        patient_id=generated["patient_id"],
        ehr_system=generated["ehr_system"],
        status="approved",
        note_text=final_text,
        feedback=None,
        reviewed_at=reviewed_at,
        documentation_time_seconds=documentation_time_seconds,
        exceeded_target=exceeded_target,
        suggestions=suggestions,
    )


@mcp.tool()
def reject_encounter_note(
    note_id: Annotated[str, Field(min_length=1)],
    feedback: Annotated[str, Field(min_length=1, max_length=2000)],
) -> ReviewedNote:
    """
    Record a clinician's rejection of a previously generated draft note,
    together with their feedback (REQ-005 / REQ-014), so the audit trail
    shows both the AI's note and the clinician's decision (REQ-006). Call
    this only for a note_id returned by generate_encounter_note.

    Rejecting an already-rejected note with the same feedback is a no-op
    that returns the existing decision -- it does not write a second audit
    entry. Rejecting a note that was already approved (or vice versa)
    raises ToolError: a note gets exactly one clinician decision.

    Same as approve_encounter_note, the result carries
    `documentation_time_seconds` (REQ-012), `exceeded_target`, and
    `suggestions` when the review ran over the 2-minute target (STORY-009).
    """
    correlation_id = new_correlation_id()
    log_event(
        _logger, "info", TOOL_STARTED, correlation_id,
        tool="reject_encounter_note", note_id=note_id,
    )

    record = _find_note(note_id)
    if record is None:
        log_event(
            _logger, "warning", TOOL_ERROR, correlation_id,
            tool="reject_encounter_note", error_class="ResourceNotFoundError",
        )
        raise ResourceNotFoundError(f"No generated note found for note_id={note_id!r}.")

    generated = record["generated"]
    decision = record["decision"]
    current_text = _current_note_text(record)

    if decision is not None:
        if decision["action"] == "reject_note" and decision.get("feedback") == feedback:
            documentation_time_seconds = decision.get(
                "documentation_time_seconds",
                _documentation_time_seconds(generated["timestamp"], decision["timestamp"]),
            )
            log_event(
                _logger, "info", TOOL_COMPLETED, correlation_id,
                tool="reject_encounter_note", note_id=note_id, outcome="success", no_op=True,
                documentation_time_seconds=documentation_time_seconds,
            )
            return ReviewedNote(
                note_id=note_id,
                patient_id=decision["patient_id"],
                ehr_system=decision["ehr_system"],
                status="rejected",
                note_text=decision["note_text"],
                feedback=decision.get("feedback"),
                reviewed_at=decision["timestamp"],
                documentation_time_seconds=documentation_time_seconds,
                exceeded_target=documentation_time_seconds > REVIEW_TIME_TARGET_SECONDS,
                suggestions=_speed_up_suggestions(documentation_time_seconds),
            )
        log_event(
            _logger, "warning", ACCESS_DENIED, correlation_id,
            tool="reject_encounter_note", note_id=note_id, reason="conflicting_decision",
            error_class="AccessDenied",
        )
        raise ToolError(
            f"note_id={note_id!r} already has a recorded decision ({decision['action']}); "
            "a note gets exactly one clinician decision."
        )

    reviewed_at = datetime.now(timezone.utc).isoformat()
    documentation_time_seconds = _documentation_time_seconds(generated["timestamp"], reviewed_at)
    exceeded_target = documentation_time_seconds > REVIEW_TIME_TARGET_SECONDS
    suggestions = _speed_up_suggestions(documentation_time_seconds)
    _append_audit_entry(
        {
            "timestamp": reviewed_at,
            "action": "reject_note",
            "note_id": note_id,
            "ehr_system": generated["ehr_system"],
            "patient_id": generated["patient_id"],
            "note_text": current_text,
            "feedback": feedback,
            "status": "rejected",
            "documentation_time_seconds": documentation_time_seconds,
        }
    )
    log_event(
        _logger, "info", TOOL_COMPLETED, correlation_id,
        tool="reject_encounter_note", note_id=note_id, outcome="success", no_op=False,
        documentation_time_seconds=documentation_time_seconds, exceeded_target=exceeded_target,
    )
    return ReviewedNote(
        note_id=note_id,
        patient_id=generated["patient_id"],
        ehr_system=generated["ehr_system"],
        status="rejected",
        note_text=current_text,
        feedback=feedback,
        reviewed_at=reviewed_at,
        documentation_time_seconds=documentation_time_seconds,
        exceeded_target=exceeded_target,
        suggestions=suggestions,
    )


def _find_suggestion(suggestion_id: str) -> dict | None:
    """Reconstruct a code suggestion's current state by replaying the audit
    log, same pattern as _find_note -- the audit trail is the single source
    of truth (REQ-006)."""
    if not AUDIT_LOG_PATH.exists():
        return None
    suggested = None
    decision = None
    with AUDIT_LOG_PATH.open(encoding="utf-8") as f:
        for line in f:
            entry = json.loads(line)
            if entry.get("suggestion_id") != suggestion_id:
                continue
            if entry["action"] == "suggest_codes":
                suggested = entry
            elif entry["action"] in ("approve_codes", "reject_codes"):
                decision = entry
    if suggested is None:
        return None
    return {"suggested": suggested, "decision": decision}


@mcp.tool()
def suggest_codes(
    note_id: Annotated[str, Field(min_length=1)],
    codes: Annotated[list[CodeSuggestion], Field(min_length=1)],
    confidence: Annotated[float, Field(ge=0.0, le=1.0)],
    confidence_reason: Annotated[str | None, Field(max_length=1000)] = None,
) -> SuggestedCodes:
    """
    Record AI-suggested ICD-10/CPT codes for a previously generated encounter
    note so a clinician can review and approve or reject them (REQ-014) and
    trace the suggestion in the audit trail (REQ-006). Call this only for a
    note_id returned by generate_encounter_note.

    You (the calling model) compose `codes` yourself, grounded in the note and
    its transcript, each as {code, code_system: "icd-10"|"cpt", description}.
    This tool does not derive or validate codes -- there is no external
    ICD-10/CPT database wired into this system, so correctness is checked by
    clinician review, not computed here.

    You also assess your own `confidence` (0.0-1.0) in this whole code set --
    one score for the set, same as this server's confidence pattern
    elsewhere. Below CONFIDENCE_THRESHOLD, this tool flags the suggestion and
    requires `confidence_reason` (REQ-008: a warning needs an explanation).
    There's no UI here, so the flag surfaces as a `warning` string -- relay it
    to the clinician yourself.

    The returned suggestion set is always status="draft": it is not billing
    documentation until a clinician reviews and approves it.
    """
    correlation_id = new_correlation_id()
    log_event(
        _logger, "info", TOOL_STARTED, correlation_id,
        tool="suggest_codes", note_id=note_id, code_count=len(codes),
    )

    note_record = _find_note(note_id)
    if note_record is None:
        log_event(
            _logger, "warning", TOOL_ERROR, correlation_id,
            tool="suggest_codes", error_class="ResourceNotFoundError",
        )
        raise ResourceNotFoundError(
            f"No generated note found for note_id={note_id!r}; confirm the note "
            "exists (generate_encounter_note) before suggesting codes for it."
        )

    flagged = confidence < CONFIDENCE_THRESHOLD
    if flagged and not confidence_reason:
        log_event(
            _logger, "warning", TOOL_ERROR, correlation_id,
            tool="suggest_codes", error_class="ValidationError",
        )
        raise ToolError(
            f"confidence={confidence!r} is below the low-confidence threshold "
            f"({CONFIDENCE_THRESHOLD}); confidence_reason is required so the "
            "warning carries an explanation (REQ-008)."
        )

    generated = note_record["generated"]
    suggestion_id = str(uuid.uuid4())
    suggested_at = datetime.now(timezone.utc).isoformat()
    warning = f"Low confidence ({confidence:.2f}): {confidence_reason}" if flagged else None

    _append_audit_entry(
        {
            "timestamp": suggested_at,
            "action": "suggest_codes",
            "suggestion_id": suggestion_id,
            "note_id": note_id,
            "ehr_system": generated["ehr_system"],
            "patient_id": generated["patient_id"],
            "codes": [c.model_dump() for c in codes],
            "confidence": confidence,
            "flagged": flagged,
            "confidence_reason": confidence_reason,
            "status": "draft",
        }
    )
    log_event(
        _logger, "info", TOOL_COMPLETED, correlation_id,
        tool="suggest_codes", outcome="success", suggestion_id=suggestion_id,
        code_count=len(codes), flagged=flagged,
    )

    return SuggestedCodes(
        suggestion_id=suggestion_id,
        note_id=note_id,
        patient_id=generated["patient_id"],
        ehr_system=generated["ehr_system"],
        status="draft",
        codes=codes,
        confidence=confidence,
        flagged=flagged,
        warning=warning,
        suggested_at=suggested_at,
    )


@mcp.tool()
def approve_codes(
    suggestion_id: Annotated[str, Field(min_length=1)],
    edited_codes: list[CodeSuggestion] | None = None,
) -> ReviewedCodes:
    """
    Record a clinician's approval of a previously suggested code set
    (REQ-014), so the audit trail shows both the AI's suggestions and the
    clinician's decision (REQ-006). Call this only for a suggestion_id
    returned by suggest_codes.

    Pass `edited_codes` if the clinician changed the list before approving
    (e.g. dropped an incorrect code); otherwise the originally suggested
    codes are used as-is.

    Approving an already-approved suggestion set with the same edited_codes
    is a no-op that returns the existing decision -- it does not write a
    second audit entry. Approving a set that was already rejected (or vice
    versa) raises ToolError: a suggestion set gets exactly one clinician
    decision.
    """
    correlation_id = new_correlation_id()
    log_event(
        _logger, "info", TOOL_STARTED, correlation_id,
        tool="approve_codes", suggestion_id=suggestion_id,
    )

    record = _find_suggestion(suggestion_id)
    if record is None:
        log_event(
            _logger, "warning", TOOL_ERROR, correlation_id,
            tool="approve_codes", error_class="ResourceNotFoundError",
        )
        raise ResourceNotFoundError(f"No code suggestion found for suggestion_id={suggestion_id!r}.")

    suggested = record["suggested"]
    decision = record["decision"]
    final_codes = edited_codes if edited_codes is not None else [CodeSuggestion(**c) for c in suggested["codes"]]
    final_codes_dump = [c.model_dump() for c in final_codes]

    if decision is not None:
        if decision["action"] == "approve_codes" and decision["codes"] == final_codes_dump:
            log_event(
                _logger, "info", TOOL_COMPLETED, correlation_id,
                tool="approve_codes", suggestion_id=suggestion_id, outcome="success", no_op=True,
            )
            return ReviewedCodes(
                suggestion_id=suggestion_id,
                note_id=decision["note_id"],
                patient_id=decision["patient_id"],
                ehr_system=decision["ehr_system"],
                status="approved",
                codes=[CodeSuggestion(**c) for c in decision["codes"]],
                feedback=None,
                reviewed_at=decision["timestamp"],
            )
        log_event(
            _logger, "warning", ACCESS_DENIED, correlation_id,
            tool="approve_codes", suggestion_id=suggestion_id, reason="conflicting_decision",
            error_class="AccessDenied",
        )
        raise ToolError(
            f"suggestion_id={suggestion_id!r} already has a recorded decision "
            f"({decision['action']}); a suggestion set gets exactly one clinician decision."
        )

    reviewed_at = datetime.now(timezone.utc).isoformat()
    _append_audit_entry(
        {
            "timestamp": reviewed_at,
            "action": "approve_codes",
            "suggestion_id": suggestion_id,
            "note_id": suggested["note_id"],
            "ehr_system": suggested["ehr_system"],
            "patient_id": suggested["patient_id"],
            "codes": final_codes_dump,
            "status": "approved",
        }
    )
    log_event(
        _logger, "info", TOOL_COMPLETED, correlation_id,
        tool="approve_codes", suggestion_id=suggestion_id, outcome="success", no_op=False,
    )
    return ReviewedCodes(
        suggestion_id=suggestion_id,
        note_id=suggested["note_id"],
        patient_id=suggested["patient_id"],
        ehr_system=suggested["ehr_system"],
        status="approved",
        codes=final_codes,
        feedback=None,
        reviewed_at=reviewed_at,
    )


@mcp.tool()
def reject_codes(
    suggestion_id: Annotated[str, Field(min_length=1)],
    feedback: Annotated[str, Field(min_length=1, max_length=2000)],
) -> ReviewedCodes:
    """
    Record a clinician's rejection of a previously suggested code set,
    together with feedback for improvement (REQ-014), so the audit trail
    shows both the AI's suggestions and the clinician's decision (REQ-006).
    Call this only for a suggestion_id returned by suggest_codes.

    Rejecting an already-rejected suggestion set with the same feedback is a
    no-op that returns the existing decision -- it does not write a second
    audit entry. Rejecting a set that was already approved (or vice versa)
    raises ToolError: a suggestion set gets exactly one clinician decision.
    """
    correlation_id = new_correlation_id()
    log_event(
        _logger, "info", TOOL_STARTED, correlation_id,
        tool="reject_codes", suggestion_id=suggestion_id,
    )

    record = _find_suggestion(suggestion_id)
    if record is None:
        log_event(
            _logger, "warning", TOOL_ERROR, correlation_id,
            tool="reject_codes", error_class="ResourceNotFoundError",
        )
        raise ResourceNotFoundError(f"No code suggestion found for suggestion_id={suggestion_id!r}.")

    suggested = record["suggested"]
    decision = record["decision"]

    if decision is not None:
        if decision["action"] == "reject_codes" and decision.get("feedback") == feedback:
            log_event(
                _logger, "info", TOOL_COMPLETED, correlation_id,
                tool="reject_codes", suggestion_id=suggestion_id, outcome="success", no_op=True,
            )
            return ReviewedCodes(
                suggestion_id=suggestion_id,
                note_id=decision["note_id"],
                patient_id=decision["patient_id"],
                ehr_system=decision["ehr_system"],
                status="rejected",
                codes=[CodeSuggestion(**c) for c in decision["codes"]],
                feedback=decision.get("feedback"),
                reviewed_at=decision["timestamp"],
            )
        log_event(
            _logger, "warning", ACCESS_DENIED, correlation_id,
            tool="reject_codes", suggestion_id=suggestion_id, reason="conflicting_decision",
            error_class="AccessDenied",
        )
        raise ToolError(
            f"suggestion_id={suggestion_id!r} already has a recorded decision "
            f"({decision['action']}); a suggestion set gets exactly one clinician decision."
        )

    reviewed_at = datetime.now(timezone.utc).isoformat()
    original_codes = [CodeSuggestion(**c) for c in suggested["codes"]]
    _append_audit_entry(
        {
            "timestamp": reviewed_at,
            "action": "reject_codes",
            "suggestion_id": suggestion_id,
            "note_id": suggested["note_id"],
            "ehr_system": suggested["ehr_system"],
            "patient_id": suggested["patient_id"],
            "codes": suggested["codes"],
            "feedback": feedback,
            "status": "rejected",
        }
    )
    log_event(
        _logger, "info", TOOL_COMPLETED, correlation_id,
        tool="reject_codes", suggestion_id=suggestion_id, outcome="success", no_op=False,
    )
    return ReviewedCodes(
        suggestion_id=suggestion_id,
        note_id=suggested["note_id"],
        patient_id=suggested["patient_id"],
        ehr_system=suggested["ehr_system"],
        status="rejected",
        codes=original_codes,
        feedback=feedback,
        reviewed_at=reviewed_at,
    )


def _find_gap(gap_id: str) -> dict | None:
    """Reconstruct a single care gap's current state by replaying the audit
    log, same pattern as _find_note/_find_suggestion -- the audit trail is
    the single source of truth (REQ-006)."""
    if not AUDIT_LOG_PATH.exists():
        return None
    identified = None
    addressed = None
    with AUDIT_LOG_PATH.open(encoding="utf-8") as f:
        for line in f:
            entry = json.loads(line)
            if entry.get("gap_id") != gap_id:
                continue
            if entry["action"] == "identify_care_gap":
                identified = entry
            elif entry["action"] == "address_care_gap":
                addressed = entry
    if identified is None:
        return None
    return {"identified": identified, "addressed": addressed}


@mcp.tool()
def identify_care_gaps(
    note_id: Annotated[str, Field(min_length=1)],
    gaps: Annotated[list[CareGapInput], Field(min_length=1)],
) -> list[CareGap]:
    """
    Flag potential care gaps found in a previously generated encounter note
    for clinician review (REQ-003), each traceable independently in the
    audit trail (REQ-006). Call this only for a note_id returned by
    generate_encounter_note.

    You (the calling model) compose each gap yourself as {description,
    confidence, confidence_reason}, grounded in the note and its
    transcript/chart -- e.g. an overdue screening or a missing vaccination
    implied by the encounter. This tool does not derive or validate care
    gaps against any clinical-guideline database; there is none wired into
    this system, so false positives are caught by clinician review, not
    computed here.

    Confidence is per-gap, not per-call: gaps from the same encounter can
    have different confidence, and each is flagged/explained independently
    (REQ-008) the same way it's addressed independently -- below
    CONFIDENCE_THRESHOLD, that gap's `confidence_reason` is required, or
    this raises ToolError before any gap in the call is recorded.

    Each input becomes its own CareGap with its own gap_id, addressed
    independently via address_care_gap -- a clinician might resolve one gap
    today and leave another flagged for weeks. Unlike suggest_codes, these
    are deliberately not bundled into one shared decision: bundling would
    misrepresent partial progress on the gaps from a single encounter.
    """
    correlation_id = new_correlation_id()
    log_event(
        _logger, "info", TOOL_STARTED, correlation_id,
        tool="identify_care_gaps", note_id=note_id, gap_count=len(gaps),
    )

    note_record = _find_note(note_id)
    if note_record is None:
        log_event(
            _logger, "warning", TOOL_ERROR, correlation_id,
            tool="identify_care_gaps", error_class="ResourceNotFoundError",
        )
        raise ResourceNotFoundError(
            f"No generated note found for note_id={note_id!r}; confirm the note "
            "exists (generate_encounter_note) before identifying care gaps for it."
        )

    for gap_input in gaps:
        if gap_input.confidence < CONFIDENCE_THRESHOLD and not gap_input.confidence_reason:
            log_event(
                _logger, "warning", TOOL_ERROR, correlation_id,
                tool="identify_care_gaps", error_class="ValidationError",
            )
            raise ToolError(
                f"confidence={gap_input.confidence!r} for {gap_input.description!r} is below "
                f"the low-confidence threshold ({CONFIDENCE_THRESHOLD}); confidence_reason is "
                "required so the warning carries an explanation (REQ-008)."
            )

    generated = note_record["generated"]
    identified_at = datetime.now(timezone.utc).isoformat()
    results: list[CareGap] = []
    for gap_input in gaps:
        gap_id = str(uuid.uuid4())
        flagged = gap_input.confidence < CONFIDENCE_THRESHOLD
        warning = f"Low confidence ({gap_input.confidence:.2f}): {gap_input.confidence_reason}" if flagged else None
        _append_audit_entry(
            {
                "timestamp": identified_at,
                "action": "identify_care_gap",
                "gap_id": gap_id,
                "note_id": note_id,
                "ehr_system": generated["ehr_system"],
                "patient_id": generated["patient_id"],
                "description": gap_input.description,
                "confidence": gap_input.confidence,
                "flagged": flagged,
                "confidence_reason": gap_input.confidence_reason,
                "status": "flagged",
            }
        )
        results.append(
            CareGap(
                gap_id=gap_id,
                note_id=note_id,
                patient_id=generated["patient_id"],
                ehr_system=generated["ehr_system"],
                status="flagged",
                description=gap_input.description,
                confidence=gap_input.confidence,
                flagged=flagged,
                warning=warning,
                identified_at=identified_at,
            )
        )
    log_event(
        _logger, "info", TOOL_COMPLETED, correlation_id,
        tool="identify_care_gaps", note_id=note_id, outcome="success", gap_count=len(results),
    )
    return results


@mcp.tool()
def address_care_gap(
    gap_id: Annotated[str, Field(min_length=1)],
    resolution: Annotated[str, Field(min_length=1, max_length=1000)],
) -> AddressedCareGap:
    """
    Record what a clinician actually did about a flagged care gap (ordered
    the missing screening, documented why it doesn't apply, deferred to a
    follow-up, etc.) so the action is traceable in the audit trail (REQ-006).
    Call this only for a gap_id returned by identify_care_gaps.

    Unlike approve_codes/reject_codes, there is no accept/decline choice
    here -- "addressing" a care gap is whatever the clinician actually did,
    described in `resolution`. Each gap is closed independently of any
    others from the same identify_care_gaps call.

    Addressing an already-addressed gap with the identical resolution is a
    no-op that returns the existing record -- it does not write a second
    audit entry. Addressing it again with a different resolution raises
    ToolError: a gap gets exactly one closing action, same as a note gets
    exactly one clinician decision.
    """
    correlation_id = new_correlation_id()
    log_event(
        _logger, "info", TOOL_STARTED, correlation_id,
        tool="address_care_gap", gap_id=gap_id,
    )

    record = _find_gap(gap_id)
    if record is None:
        log_event(
            _logger, "warning", TOOL_ERROR, correlation_id,
            tool="address_care_gap", error_class="ResourceNotFoundError",
        )
        raise ResourceNotFoundError(f"No care gap found for gap_id={gap_id!r}.")

    identified = record["identified"]
    addressed = record["addressed"]

    if addressed is not None:
        if addressed["resolution"] == resolution:
            log_event(
                _logger, "info", TOOL_COMPLETED, correlation_id,
                tool="address_care_gap", gap_id=gap_id, outcome="success", no_op=True,
            )
            return AddressedCareGap(
                gap_id=gap_id,
                note_id=addressed["note_id"],
                patient_id=addressed["patient_id"],
                ehr_system=addressed["ehr_system"],
                status="addressed",
                description=identified["description"],
                resolution=addressed["resolution"],
                addressed_at=addressed["timestamp"],
            )
        log_event(
            _logger, "warning", ACCESS_DENIED, correlation_id,
            tool="address_care_gap", gap_id=gap_id, reason="gap_already_closed",
            error_class="AccessDenied",
        )
        raise ToolError(
            f"gap_id={gap_id!r} already has a recorded resolution; "
            "a care gap gets exactly one closing action."
        )

    addressed_at = datetime.now(timezone.utc).isoformat()
    _append_audit_entry(
        {
            "timestamp": addressed_at,
            "action": "address_care_gap",
            "gap_id": gap_id,
            "note_id": identified["note_id"],
            "ehr_system": identified["ehr_system"],
            "patient_id": identified["patient_id"],
            "resolution": resolution,
            "status": "addressed",
        }
    )
    log_event(
        _logger, "info", TOOL_COMPLETED, correlation_id,
        tool="address_care_gap", gap_id=gap_id, outcome="success", no_op=False,
    )
    return AddressedCareGap(
        gap_id=gap_id,
        note_id=identified["note_id"],
        patient_id=identified["patient_id"],
        ehr_system=identified["ehr_system"],
        status="addressed",
        description=identified["description"],
        resolution=resolution,
        addressed_at=addressed_at,
    )


def _open_care_gaps_for_patient(patient_id: str) -> list[dict]:
    """Every currently-open (identified but not yet addressed) care gap for
    a patient, across every note/encounter, reconstructed by replaying the
    audit log -- same source-of-truth pattern as _find_gap, aggregated
    across every gap_id for this patient instead of looking up just one.
    Plain local file I/O, no model call -- REQ pattern for
    prioritize_care_gaps's "fetch the real data itself" requirement."""
    if not AUDIT_LOG_PATH.exists():
        return []
    identified: dict[str, dict] = {}
    addressed_ids: set[str] = set()
    with AUDIT_LOG_PATH.open(encoding="utf-8") as f:
        for line in f:
            entry = json.loads(line)
            if entry.get("patient_id") != patient_id:
                continue
            if entry["action"] == "identify_care_gap":
                identified[entry["gap_id"]] = entry
            elif entry["action"] == "address_care_gap":
                addressed_ids.add(entry["gap_id"])
    return [gap for gap_id, gap in identified.items() if gap_id not in addressed_ids]


def _extract_ranking_text(content) -> str:
    """The client's sampled response as plain text. Sampling content can be
    text, image, or audio (SamplingContent); this tool only ever asks a
    text question, but a non-conformant client could still answer with a
    non-text block, so this returns a clear fallback string rather than
    crashing on a missing `.text` attribute -- never an empty answer."""
    if isinstance(content, TextContent):
        return content.text
    return f"[client returned a non-text response ({content.type}); no ranking text available]"


async def _list_open_gaps_resolver(patient_id: str, ctx: Context) -> CreateMessageResult | Sample | None:
    """Resolver body for prioritize_care_gaps's `completion` parameter.

    Returning None means "don't attempt sampling at all" -- the framework
    never contacts the client in that case, only a real Sample marker
    does. Two things short-circuit to None: no open gaps (nothing to rank)
    and no declared sampling capability -- checking `ctx.client_capabilities`
    here, before ever building the marker, is what keeps a client that
    doesn't support sampling from ever seeing a MISSING_REQUIRED_CLIENT_CAPABILITY
    protocol error: that error is only raised once a Sample marker actually
    reaches the framework's fulfillment step, so simply never emitting one
    avoids it entirely and lets the tool body return a normal degraded
    result instead.

    This does NOT cover every failure mode by itself. If the client DOES
    declare sampling but then the specific request is refused or errors (a
    human declining an approval prompt, a timeout, a malformed response),
    that failure happens inside the framework's own fulfillment of the
    Sample marker -- entirely after this resolver has already returned and
    exited, so there is no code of mine positioned to catch it. It surfaces
    as a raised MCPError, which this SDK's tool-call handler re-raises
    rather than converting to a normal tool result (verified by reading
    mcp/server/mcpserver/server.py's _handle_call_tool: MCPError is
    special-cased to `raise` again, unlike ToolError/generic Exception,
    which do get turned into an is_error tool result). That path still
    doesn't crash the server process and doesn't return silence -- the
    calling model sees a clear MCP-level tool-call error -- but it does
    arrive as a protocol error rather than this tool's own
    CareGapPriorityResult(degraded=True, ...) shape. Doing better than that
    would mean bypassing this SDK's non-deprecated declarative sampling
    path for the deprecated imperative one (ctx.session.create_message,
    SEP-2577) just to get a try/except around the request -- a worse
    trade for a rarer failure mode, so this file doesn't make it.
    """
    gaps = _open_care_gaps_for_patient(patient_id)
    if not gaps:
        return None
    if ctx.client_capabilities is None or ctx.client_capabilities.sampling is None:
        return None

    correlation_id = new_correlation_id()
    log_event(
        _logger, "info", SAMPLING_REQUEST_STARTED, correlation_id,
        tool="prioritize_care_gaps", gap_count=len(gaps),
    )
    _sampling_calls[ctx.request_id] = (correlation_id, time.perf_counter())
    await emit_progress(
        ctx, 0, None,
        f"Asking the client's model to prioritize {len(gaps)} open care gap(s) (single round-trip, no fixed step count)",
    )

    gap_list_text = "\n".join(f"- ({gap['gap_id']}) {gap['description']}" for gap in gaps)
    return Sample(
        messages=[
            SamplingMessage(
                role="user",
                content=TextContent(
                    type="text",
                    text=f"Here are this patient's currently open care gaps, each with an opaque id:\n\n{gap_list_text}",
                ),
            )
        ],
        max_tokens=PRIORITIZE_GAPS_MAX_TOKENS,
        system_prompt=PRIORITIZE_GAPS_SYSTEM_PROMPT,
    )


@mcp.tool()
async def prioritize_care_gaps(
    patient_id: Annotated[str, Field(min_length=1)],
    completion: Annotated[CreateMessageResult | None, Resolve(_list_open_gaps_resolver)],
    *,
    ctx: Context,
) -> CareGapPriorityResult:
    """
    Ask the connected client's own model to reason about which of a
    patient's currently open care gaps (from identify_care_gaps, not yet
    closed by address_care_gap) are most clinically urgent to act on
    first, and why. This is judgment, not lookup -- urgency isn't stored
    anywhere in this system, so it can't be computed by sorting a field.

    This server fetches the open gaps itself, in plain Python, straight
    from the audit log (see _open_care_gaps_for_patient) -- no model call
    is involved in gathering that data. Only the actual prioritization
    reasoning goes through the client via MCP sampling. This server never
    names a model or holds an API key anywhere: sampling is answered by
    whatever LLM the connected client has configured, entirely on the
    client's side.

    A degraded result -- never a crash, never silent emptiness -- comes
    back with `degraded=true` and a `degraded_reason` in two cases: no
    open gaps exist for this patient (`"no_open_care_gaps"`), or the
    client hasn't declared MCP sampling support
    (`"sampling_not_supported_by_client"`). See
    _list_open_gaps_resolver's docstring for the one narrower failure mode
    (a client that declares sampling but then refuses or errors the
    specific request) that this tool cannot catch and convert into this
    same graceful shape -- it surfaces as an MCP tool-call error instead,
    which still isn't a crash or silence, just not this result type.

    `ranking_text` is the model's free-text response -- a ranked list with
    short reasons, per the system prompt -- not a decision. A clinician
    still reviews and acts on it, same as every other AI-drafted
    recommendation in this server.
    """
    gaps = _open_care_gaps_for_patient(patient_id)
    prioritized_at = datetime.now(timezone.utc).isoformat()
    open_gaps = [OpenCareGapSummary(gap_id=gap["gap_id"], description=gap["description"]) for gap in gaps]

    if not gaps:
        log_event(
            _logger, "info", TOOL_COMPLETED, new_correlation_id(),
            tool="prioritize_care_gaps", outcome="degraded", degraded_reason="no_open_care_gaps", gap_count=0,
        )
        return CareGapPriorityResult(
            patient_id=patient_id,
            gap_count=0,
            open_gaps=[],
            degraded=True,
            degraded_reason="no_open_care_gaps",
            ranking_text=None,
            prioritized_at=prioritized_at,
        )

    if completion is None:
        reason = (
            "sampling_not_supported_by_client"
            if ctx.client_capabilities is None or ctx.client_capabilities.sampling is None
            else "sampling_unavailable"
        )
        log_event(
            _logger, "warning", TOOL_COMPLETED, new_correlation_id(),
            tool="prioritize_care_gaps", outcome="degraded", degraded_reason=reason, gap_count=len(gaps),
        )
        return CareGapPriorityResult(
            patient_id=patient_id,
            gap_count=len(gaps),
            open_gaps=open_gaps,
            degraded=True,
            degraded_reason=reason,
            ranking_text=None,
            prioritized_at=prioritized_at,
        )

    started = _sampling_calls.pop(ctx.request_id, None)
    correlation_id, start_time = started if started is not None else (new_correlation_id(), None)
    duration_ms = round((time.perf_counter() - start_time) * 1000, 1) if start_time is not None else None
    log_event(
        _logger, "info", SAMPLING_REQUEST_FINISHED, correlation_id,
        tool="prioritize_care_gaps", outcome="success", duration_ms=duration_ms, gap_count=len(gaps),
    )
    await emit_progress(ctx, 1, None, "Received the client model's prioritization")
    return CareGapPriorityResult(
        patient_id=patient_id,
        gap_count=len(gaps),
        open_gaps=open_gaps,
        degraded=False,
        degraded_reason=None,
        ranking_text=_extract_ranking_text(completion.content),
        prioritized_at=prioritized_at,
    )


def _find_citation(transcript: str, claimed_excerpt: str) -> tuple[int, int, str] | None:
    """Locate claimed_excerpt in transcript, tolerant of whitespace/case
    differences (line breaks, capitalization) but NOT of paraphrasing or
    punctuation changes -- a real match, not a fuzzy guess. Returns
    (start, end, matched_text) in the transcript's own coordinates -- the
    offsets a real UI would use to highlight -- or None if no match."""
    normalized_excerpt = " ".join(claimed_excerpt.split())
    if not normalized_excerpt:
        return None
    pattern = re.escape(normalized_excerpt).replace(r"\ ", r"\s+")
    match = re.search(pattern, transcript, re.IGNORECASE)
    if match is None:
        return None
    return match.start(), match.end(), transcript[match.start() : match.end()]


@mcp.tool()
def request_citation(
    note_id: Annotated[str, Field(min_length=1)],
    claimed_excerpt: Annotated[str, Field(min_length=1, max_length=1000)],
) -> CitationResult:
    """
    Verify a claimed source citation for an AI recommendation (a note, a
    code suggestion, a care gap -- anything drafted from this note's
    transcript) against the actual encounter transcript (REQ-007), logging
    every request and its outcome regardless of result (REQ-006). Call this
    only for a note_id returned by generate_encounter_note.

    You (the calling model) supply `claimed_excerpt` -- the specific text
    you believe supports a recommendation. This tool checks whether that
    text genuinely appears in the transcript (tolerant of whitespace/case
    differences but not paraphrasing or punctuation changes) and returns the
    matched text plus its character offsets in the transcript if found --
    those offsets are what a real UI would use to highlight the source
    ("highlights the relevant transcript section"). If not found, `found` is
    false with an explanation, rather than silently failing.

    Important limitation, stated plainly: this proves the excerpt is not
    fabricated -- it genuinely appears in the transcript -- not that it is
    definitively the true reason behind the recommendation. There is no
    semantic-grounding system in this build; a real-but-unrelated excerpt
    would still pass this check.
    """
    correlation_id = new_correlation_id()
    log_event(
        _logger, "info", TOOL_STARTED, correlation_id,
        tool="request_citation", note_id=note_id,
    )

    note_record = _find_note(note_id)
    if note_record is None:
        log_event(
            _logger, "warning", TOOL_ERROR, correlation_id,
            tool="request_citation", error_class="ResourceNotFoundError",
        )
        raise ResourceNotFoundError(
            f"No generated note found for note_id={note_id!r}; confirm the note "
            "exists (generate_encounter_note) before requesting a citation for it."
        )

    generated = note_record["generated"]
    transcript = generated["transcript"]
    requested_at = datetime.now(timezone.utc).isoformat()

    match = _find_citation(transcript, claimed_excerpt)
    if match is None:
        found = False
        matched_text = None
        start_offset = None
        end_offset = None
        explanation = (
            "This text does not appear verbatim in the encounter transcript "
            "(checked case- and whitespace-insensitively). It may be "
            "paraphrased, drawn from outside the transcript, or fabricated."
        )
    else:
        start_offset, end_offset, matched_text = match
        found = True
        explanation = None

    _append_audit_entry(
        {
            "timestamp": requested_at,
            "action": "request_citation",
            "note_id": note_id,
            "ehr_system": generated["ehr_system"],
            "patient_id": generated["patient_id"],
            "claimed_excerpt": claimed_excerpt,
            "found": found,
            "matched_text": matched_text,
            "start_offset": start_offset,
            "end_offset": end_offset,
        }
    )
    log_event(
        _logger, "info", TOOL_COMPLETED, correlation_id,
        tool="request_citation", note_id=note_id, outcome="success", found=found,
    )

    return CitationResult(
        note_id=note_id,
        patient_id=generated["patient_id"],
        ehr_system=generated["ehr_system"],
        claimed_excerpt=claimed_excerpt,
        found=found,
        matched_text=matched_text,
        start_offset=start_offset,
        end_offset=end_offset,
        explanation=explanation,
        requested_at=requested_at,
    )


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
        _logger, "info", TOOL_STARTED, correlation_id,
        tool="check_github_issue_status", issue_number=issue_number,
    )
    await emit_progress(
        ctx, 0, None,
        f"Looking up GitHub issue/PR #{issue_number} (single external call, no fixed step count)",
    )
    checked_at = datetime.now(timezone.utc).isoformat()

    try:
        result = get_issue_or_pr(issue_number, correlation_id)
    except httpx.TimeoutException:
        log_event(
            _logger, "warning", TOOL_ERROR, correlation_id,
            tool="check_github_issue_status", error_class="TimeoutError",
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
            issue_number=issue_number, ok=False, found=None, state=None, title=None,
            is_pull_request=None, error="Timed out contacting GitHub.", checked_at=checked_at,
        )
    except GitHubAPIError:
        log_event(
            _logger, "warning", TOOL_ERROR, correlation_id,
            tool="check_github_issue_status", error_class="UpstreamUnavailable",
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
            issue_number=issue_number, ok=False, found=None, state=None, title=None,
            is_pull_request=None, error="GitHub's API did not return a usable response.", checked_at=checked_at,
        )
    except Exception:
        # Last-resort catch: REQ-003 is "on ANY failure, return the result,
        # never throw" -- this is the backstop for a failure mode not
        # already named above, not the primary error path.
        log_event(
            _logger, "error", TOOL_ERROR, correlation_id,
            tool="check_github_issue_status", error_class="UnexpectedError",
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
            issue_number=issue_number, ok=False, found=None, state=None, title=None,
            is_pull_request=None, error="An unexpected error occurred.", checked_at=checked_at,
        )

    if not result["found"]:
        log_event(
            _logger, "info", TOOL_COMPLETED, correlation_id,
            tool="check_github_issue_status", outcome="not_found",
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
            issue_number=issue_number, ok=True, found=False, state=None, title=None,
            is_pull_request=None, error=None, checked_at=checked_at,
        )

    data = result["data"]
    log_event(
        _logger, "info", TOOL_COMPLETED, correlation_id,
        tool="check_github_issue_status", outcome="success", found=True, state=data.get("state"),
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


def _all_audit_entries() -> list[dict]:
    """Every entry currently in the audit trail, oldest first. Unlike
    _find_note/_find_suggestion/_find_gap (each keyed to one id), the
    compliance checks below need the whole trail at once -- this is their
    shared read, so each control does its own single pass over it rather than
    re-opening the file per control."""
    if not AUDIT_LOG_PATH.exists():
        return []
    with AUDIT_LOG_PATH.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f]


def _check_audit_completeness(entries: list[dict]) -> ComplianceControlResult:
    """REQ-006: every clinician decision in the audit trail has a matching
    AI-drafted entry -- no decision exists whose note/suggestion/gap was
    never actually recorded as drafted. This is already structurally
    guaranteed by each decision tool's own _find_note/_find_suggestion/
    _find_gap lookup (a decision can't be written for an id that lookup
    doesn't find) -- this control re-verifies that guarantee held over the
    real, persisted log, which is what would catch a corrupted or
    hand-edited audit file that the code-level guarantee alone can't see."""
    drafted: set[tuple[str, str]] = set()
    for entry in entries:
        if entry["action"] == "generate_note":
            drafted.add(("note", entry["note_id"]))
        elif entry["action"] == "suggest_codes":
            drafted.add(("suggestion", entry["suggestion_id"]))
        elif entry["action"] == "identify_care_gap":
            drafted.add(("gap", entry["gap_id"]))

    orphaned: list[str] = []
    for entry in entries:
        action = entry["action"]
        if action in ("approve_note", "reject_note") and ("note", entry["note_id"]) not in drafted:
            orphaned.append(f"{action} for note_id={entry['note_id']!r} has no generate_note entry")
        elif action in ("approve_codes", "reject_codes") and ("suggestion", entry["suggestion_id"]) not in drafted:
            orphaned.append(f"{action} for suggestion_id={entry['suggestion_id']!r} has no suggest_codes entry")
        elif action == "address_care_gap" and ("gap", entry["gap_id"]) not in drafted:
            orphaned.append(f"address_care_gap for gap_id={entry['gap_id']!r} has no identify_care_gap entry")

    passed = not orphaned
    return ComplianceControlResult(
        control_id="REQ-006-audit-completeness",
        description=(
            "Every clinician decision (approve/reject a note or code set, address a care "
            "gap) in the audit trail has a matching AI-drafted entry."
        ),
        passed=passed,
        detail="No orphaned decisions found." if passed else "; ".join(orphaned),
    )


def _check_single_decision_integrity(entries: list[dict]) -> ComplianceControlResult:
    """REQ-014: no note or code-suggestion ever accumulates more than one
    recorded clinician decision. Same re-verification purpose as
    _check_audit_completeness -- approve_encounter_note/reject_encounter_note
    (and their _codes equivalents) already refuse a conflicting second
    decision via ToolError, so this checks that guarantee actually held over
    the persisted log rather than trusting the code path alone."""
    note_decisions: dict[str, int] = {}
    suggestion_decisions: dict[str, int] = {}
    for entry in entries:
        action = entry["action"]
        if action in ("approve_note", "reject_note"):
            note_decisions[entry["note_id"]] = note_decisions.get(entry["note_id"], 0) + 1
        elif action in ("approve_codes", "reject_codes"):
            suggestion_decisions[entry["suggestion_id"]] = suggestion_decisions.get(entry["suggestion_id"], 0) + 1

    violations = [f"note_id={note_id!r} has {count} decisions" for note_id, count in note_decisions.items() if count > 1]
    violations += [
        f"suggestion_id={suggestion_id!r} has {count} decisions"
        for suggestion_id, count in suggestion_decisions.items()
        if count > 1
    ]

    passed = not violations
    return ComplianceControlResult(
        control_id="REQ-014-single-decision-integrity",
        description="No note or code-suggestion set has more than one recorded clinician decision.",
        passed=passed,
        detail="Every note and suggestion set has at most one decision." if passed else "; ".join(violations),
    )


def _check_security_incident_logging(entries: list[dict]) -> ComplianceControlResult:
    """REQ-011: every entry explicitly tagged `security_incident: true` in
    the audit trail (currently written only by read_transcript_file's
    roots-guard denial -- the one real unauthorized-access boundary this
    server enforces) is a complete record: timestamp, action, outcome, and
    error_class all present, not just a bare tag with no context a reviewer
    could act on. Keying off this explicit tag, rather than inferring from
    `error_class == "AccessDenied"` alone, matters because AccessDenied is
    also used for ordinary business-rule conflicts elsewhere in this file
    (e.g. approve_encounter_note's "already decided" case) that are not
    security incidents -- those don't reach the audit trail today, but the
    explicit tag keeps this control correct even if one someday does."""
    incidents = [entry for entry in entries if entry.get("security_incident") is True]
    incomplete = [
        entry
        for entry in incidents
        if not entry.get("timestamp") or not entry.get("action") or entry.get("outcome") != "failure" or not entry.get("error_class")
    ]

    passed = not incomplete
    if passed:
        detail = f"{len(incidents)} security-incident event(s) in the audit trail, each fully logged."
    else:
        detail = f"{len(incomplete)} of {len(incidents)} security-incident event(s) are missing a required field."
    return ComplianceControlResult(
        control_id="REQ-011-security-incident-logging",
        description="Every security-incident event (tagged security_incident=true) carries timestamp, action, outcome, and error_class.",
        passed=passed,
        detail=detail,
    )


# Keyword argument names log_event(...) must never carry (REQ-011): raw
# patient identity, free-text clinical content, or anything else
# logging_utils.log_event's own contract calls out as forbidden --
# "identifiers, counts, and durations only... never... a raw patient record
# (name, DOB, MRN, note/transcript text)". Opaque ids already logged
# throughout this file today (note_id, patient_id, suggestion_id, gap_id,
# ehr_system) are deliberately NOT in this set -- REQ-011 forbids raw PHI
# content, not the identifiers this codebase already treats as safe to log.
FORBIDDEN_LOG_CONTEXT_KEYS = frozenset({
    "transcript", "note_text", "edited_note_text",
    "first_name", "last_name", "date_of_birth", "mrn", "patient_name",
    "feedback", "confidence_reason", "claimed_excerpt", "matched_text",
    "resolution", "description", "ranking_text",
})


def _log_event_violations(source: str) -> list[str]:
    """Static scan: every log_event(...) call site in `source`, checked for
    a keyword argument name in FORBIDDEN_LOG_CONTEXT_KEYS. Returns one
    human-readable violation string per bad call site (empty if none).
    AST-based, not a text/regex search, so a forbidden name appearing only
    as a string *value* elsewhere (e.g. tool="note_text") or in a comment
    can't produce a false positive -- only an actual keyword-argument name
    at an actual log_event(...) call site counts."""
    tree = ast.parse(source)
    violations = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "log_event"):
            continue
        for kw in node.keywords:
            if kw.arg in FORBIDDEN_LOG_CONTEXT_KEYS:
                violations.append(f"log_event(...) at line {node.lineno} passes forbidden key {kw.arg!r}")
    return violations


def _check_no_phi_in_structured_logs() -> ComplianceControlResult:
    """REQ-011: no log_event(...) call site in this server's own source
    passes a keyword argument that could carry raw PHI to the structured
    stderr stream -- the one log channel in this system that is NOT
    supposed to carry it (unlike audit_log.jsonl, which legitimately does,
    by design, as REQ-006's source of truth). Unlike this file's other three
    controls, this one is static (checks source code, not audit_log.jsonl)
    -- it catches a violation the moment it's written, before it ever logs a
    single real byte of PHI, rather than after the fact."""
    violations = _log_event_violations(Path(__file__).read_text(encoding="utf-8"))
    passed = not violations
    return ComplianceControlResult(
        control_id="REQ-011-no-phi-in-structured-logs",
        description="No log_event(...) call site passes a keyword argument that could carry raw PHI.",
        passed=passed,
        detail="No forbidden keys found in any log_event(...) call." if passed else "; ".join(violations),
    )


@mcp.tool()
def run_compliance_check() -> ComplianceCheckResult:
    """
    Evaluate this server's audit trail against a fixed, named set of internal
    compliance controls (REQ-011 / REQ-018) and record the check itself in
    the audit trail (REQ-006 / STORY-010's "log all compliance checks").

    This does not claim certification against any external standard (HIPAA,
    HITRUST, or otherwise) -- no such standard is named anywhere in this
    project's requirements, and claiming one without doing the actual
    compliance work behind it would be a false claim, not a shortcut. What it
    does check, honestly: four controls this codebase can actually verify --
    audit completeness (REQ-006), single-decision integrity (REQ-014),
    security-incident logging completeness (REQ-011), and no PHI reaching
    the structured stderr log stream (REQ-011). A "compliance audit" against
    this tool means reviewing these four named controls and their pass/fail
    detail, not an opaque yes/no.

    Three controls are checked from `audit_log.jsonl` as it actually is,
    never assumed to pass because the code that writes it looks correct --
    a control failing here means the persisted data disagrees with what the
    code guarantees (a real regression, or a hand-edited/corrupted log), and
    is reported as such rather than hidden. The fourth (no-PHI-in-logs) is a
    static check of this file's own source instead, since there's nothing
    to replay for "what wasn't logged." `all_passed` is only true if every
    control passed; a partial pass is reported in full, not rounded up.
    """
    correlation_id = new_correlation_id()
    log_event(
        _logger, "info", TOOL_STARTED, correlation_id,
        tool="run_compliance_check",
    )

    entries = _all_audit_entries()
    controls = [
        _check_audit_completeness(entries),
        _check_single_decision_integrity(entries),
        _check_security_incident_logging(entries),
        _check_no_phi_in_structured_logs(),
    ]
    all_passed = all(control.passed for control in controls)
    checked_at = datetime.now(timezone.utc).isoformat()

    _append_audit_entry(
        {
            "timestamp": checked_at,
            "action": "compliance_check",
            "all_passed": all_passed,
            "controls": [control.model_dump() for control in controls],
        }
    )
    log_event(
        _logger, "info", TOOL_COMPLETED, correlation_id,
        tool="run_compliance_check", outcome="success", all_passed=all_passed,
    )

    return ComplianceCheckResult(checked_at=checked_at, controls=controls, all_passed=all_passed)


@mcp.prompt(name="prepare-encounter-note")
def prepare_encounter_note(
    patient_hint: str = "",
    ehr_system: Literal["epic", "oracle_health"] = "epic",
) -> str:
    """Clinician-triggered workflow: resolve the patient, read their chart,
    draft a structured encounter note. Returns text; a multi-turn version of
    this workflow could instead return list[Message] (e.g. a user turn plus
    a pre-filled assistant turn) -- not needed here since one instruction
    block covers the whole flow."""
    return f"""You are preparing to document a clinical encounter. Two things are available to you: the search_ehr_patient tool and the patient-chart resource (ehr://{ehr_system}/patient/{{patient_id}}/chart). Use them in this order, and do not skip ahead.

Known so far:
- Patient hint: {patient_hint or "(none given -- nothing to go on but this conversation)"}
- EHR system to check first: {ehr_system}

1. Confirm identity. If the hint above doesn't already give you a confirmed patient_id, call search_ehr_patient with ehr_system={ehr_system} and whatever you have -- the MRN if you have it, otherwise last_name together with date_of_birth. Do not invent either value.
2. Once you have exactly one confirmed patient_id, read the patient-chart resource for that patient_id before drafting anything, so the note is grounded in the real record rather than the conversation alone.
3. Draft the note as: patient identity (name, MRN, DOB), a one-paragraph encounter summary, and status: draft -- never mark a note final. A clinician must review and approve it before it counts as anything (see REQ-005 / REQ-014).
4. Assess your own confidence (0.0-1.0) in how well the transcript and chart actually support the note you drafted -- vague symptoms, a short or ambiguous transcript, or details the chart doesn't corroborate all lower it. Pass this as `confidence` to generate_encounter_note. If it's below 0.7, you must also pass `confidence_reason` explaining specifically what's uncertain (REQ-008) -- do not round confidence up just to skip writing a reason.

Handle these three situations explicitly, the way you would explain an edge case to a colleague -- do not guess past any of them:
- Information missing: search_ehr_patient has neither an MRN nor a last_name+date_of_birth to work with. Say plainly what's missing and stop; do not fabricate an identifier.
- Input ambiguous: search_ehr_patient returns more than one candidate. List them (name, DOB, MRN) and ask which one is meant; do not pick for the clinician.
- Nothing to report: search_ehr_patient returns zero matches. Say plainly that no patient was found in {ehr_system}, suggest checking the details or trying the other EHR system, and stop -- do not invent a patient or draft a note without one."""


if __name__ == "__main__":
    # STDIO, single-user, single-process -- per docs/TRANSPORT_DECISION.md.
    # This process assumes exactly one connected client for its whole
    # lifetime (matching .mcp.json's spawn-per-session launch) and is not
    # safe to run as multiple concurrent instances: audit_log.jsonl is a
    # single-writer local file with no locking, and roots enforcement /
    # MCP sampling both address "the currently connected client," which
    # only means something with one client per process. Do not scale this
    # by running more copies of server.py without redoing that design.
    log_event(
        _logger, "info", SERVER_STARTED, new_correlation_id(),
        transport="stdio", state_model="single-user-single-process",
    )
    mcp.run(transport="stdio")
