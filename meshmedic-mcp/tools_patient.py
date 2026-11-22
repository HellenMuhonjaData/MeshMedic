"""Patient lookup tools: EHR search, chart resource, live Epic fetch, transcript file read.

Split out of server.py (Phase 0, no behaviour change).
"""

from datetime import datetime, timezone
from typing import Annotated, Literal

from mcp.server.mcpserver import Context, ListRoots, Resolve
from mcp.server.mcpserver.exceptions import ResourceNotFoundError, ToolError
from mcp_types import ListRootsResult
from pydantic import Field

from app_core import (
    MAX_TRANSCRIPT_FILE_BYTES,
    _append_audit_entry,
    _logger,
    _write_audit_entry,
    mcp,
)
from audit_queries import _find_patient
from epic_fhir_client import EpicFHIRError, fetch_patient
from logging_utils import (
    ACCESS_DENIED,
    TOOL_COMPLETED,
    TOOL_ERROR,
    TOOL_STARTED,
    log_event,
    new_correlation_id,
)
from models import PatientMatch, SearchResult, TranscriptFileRead
from progress_utils import emit_progress
from roots_guard import resolve_within_roots
from sample_patients import SAMPLE_PATIENTS


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
        _logger,
        "info",
        TOOL_STARTED,
        correlation_id,
        tool="search_ehr_patient",
        ehr_system=ehr_system,
        search_key="mrn"
        if mrn
        else ("last_name+date_of_birth" if last_name else "none"),
    )
    have_mrn = bool(mrn)
    have_name_dob = bool(last_name) and bool(date_of_birth)

    if not have_mrn and not have_name_dob:
        _write_audit_entry(ehr_system, mrn, last_name, date_of_birth, 0)
        log_event(
            _logger,
            "info",
            TOOL_COMPLETED,
            correlation_id,
            tool="search_ehr_patient",
            outcome="success",
            match_count=0,
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
            ctx,
            position,
            total,
            f"Checking candidate {position} of {total} (MRN {candidate['mrn']})",
        )
        if have_mrn:
            if candidate["mrn"].lower() == mrn.lower():
                found.append(candidate)
        elif (
            candidate["last_name"].lower() == last_name.lower()
            and candidate["date_of_birth"] == date_of_birth
        ):
            found.append(candidate)

    _write_audit_entry(ehr_system, mrn, last_name, date_of_birth, len(found))

    if not found:
        log_event(
            _logger,
            "info",
            TOOL_COMPLETED,
            correlation_id,
            tool="search_ehr_patient",
            outcome="success",
            match_count=0,
        )
        return SearchResult(
            matches=[],
            count=0,
            message=f"No patient found in {ehr_system} matching the given search key.",
        )

    trimmed = found[:max_results]
    log_event(
        _logger,
        "info",
        TOOL_COMPLETED,
        correlation_id,
        tool="search_ehr_patient",
        outcome="success",
        match_count=len(trimmed),
    )
    return SearchResult(
        matches=[PatientMatch(**p) for p in trimmed],
        count=len(trimmed),
        message="",
    )


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
        _logger,
        "info",
        TOOL_STARTED,
        correlation_id,
        tool="fetch_patient_from_ehr",
        ehr_system=ehr_system,
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
            _logger,
            "warning",
            ACCESS_DENIED,
            correlation_id,
            tool="fetch_patient_from_ehr",
            ehr_system=ehr_system,
            reason="ehr_system_not_connected",
            error_class="AccessDenied",
        )
        raise ToolError(
            "oracle_health has no real FHIR sandbox connected in this build -- "
            "only epic is wired up."
        )

    await emit_progress(
        ctx,
        0,
        None,
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
            _logger,
            "error",
            TOOL_ERROR,
            correlation_id,
            tool="fetch_patient_from_ehr",
            ehr_system=ehr_system,
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
            _logger,
            "warning",
            TOOL_ERROR,
            correlation_id,
            tool="fetch_patient_from_ehr",
            ehr_system=ehr_system,
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
        _logger,
        "info",
        TOOL_COMPLETED,
        correlation_id,
        tool="fetch_patient_from_ehr",
        ehr_system=ehr_system,
        outcome="success",
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
        _logger,
        "info",
        TOOL_STARTED,
        correlation_id,
        tool="read_transcript_file",
    )

    resolved = resolve_within_roots(
        file_path,
        roots,
        logger=_logger,
        correlation_id=correlation_id,
        tool="read_transcript_file",
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
            _logger,
            "info",
            TOOL_COMPLETED,
            correlation_id,
            tool="read_transcript_file",
            outcome="denied",
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
            _logger,
            "warning",
            TOOL_ERROR,
            correlation_id,
            tool="read_transcript_file",
            error_class="ResourceNotFoundError",
        )
        raise ResourceNotFoundError(
            f"No file found at the resolved path for {file_path!r}."
        )

    size = resolved.stat().st_size
    if size > MAX_TRANSCRIPT_FILE_BYTES:
        log_event(
            _logger,
            "warning",
            TOOL_ERROR,
            correlation_id,
            tool="read_transcript_file",
            error_class="ValidationError",
        )
        raise ToolError(
            f"File is {size} bytes, over this tool's {MAX_TRANSCRIPT_FILE_BYTES}-byte limit."
        )

    try:
        text = resolved.read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        log_event(
            _logger,
            "warning",
            TOOL_ERROR,
            correlation_id,
            tool="read_transcript_file",
            error_class="ValidationError",
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
        _logger,
        "info",
        TOOL_COMPLETED,
        correlation_id,
        tool="read_transcript_file",
        outcome="success",
        byte_count=len(text.encode("utf-8")),
    )
    return TranscriptFileRead(
        file_path=file_path,
        denied=False,
        transcript=text,
        error=None,
        read_at=read_at,
    )
