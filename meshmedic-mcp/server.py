import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ResourceNotFoundError
from pydantic import BaseModel, Field

from sample_patients import SAMPLE_PATIENTS

mcp = MCPServer("meshmedic")

AUDIT_LOG_PATH = Path(__file__).parent / "audit_log.jsonl"


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


def _append_audit_entry(entry: dict) -> None:
    """Local stand-in for a real audit trail (REQ-006). Not HIPAA-grade --
    a production audit log needs its own access controls and durability
    guarantees, which this demo file does not provide."""
    with AUDIT_LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


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
    have_mrn = bool(mrn)
    have_name_dob = bool(last_name) and bool(date_of_birth)

    if not have_mrn and not have_name_dob:
        _write_audit_entry(ehr_system, mrn, last_name, date_of_birth, 0)
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
    meta = ctx.request_context.meta
    progress_token = meta.get("progress_token") if meta else None

    found = []
    for position, candidate in enumerate(candidates, start=1):
        if progress_token is not None:
            # Only emit when the caller supplied a progress token -- no token means no one is listening.
            await ctx.report_progress(
                position, total, f"Checking candidate {position} of {total} (MRN {candidate['mrn']})"
            )
        if have_mrn:
            if candidate["mrn"].lower() == mrn.lower():
                found.append(candidate)
        elif candidate["last_name"].lower() == last_name.lower() and candidate["date_of_birth"] == date_of_birth:
            found.append(candidate)

    _write_audit_entry(ehr_system, mrn, last_name, date_of_birth, len(found))

    if not found:
        return SearchResult(
            matches=[],
            count=0,
            message=f"No patient found in {ehr_system} matching the given search key.",
        )

    trimmed = found[:max_results]
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
def generate_encounter_note(
    ehr_system: Literal["epic", "oracle_health"],
    patient_id: Annotated[str, Field(min_length=1)],
    transcript: Annotated[str, Field(min_length=1, max_length=20000)],
    note_text: Annotated[str, Field(min_length=1, max_length=5000)],
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

    The returned note is always status="draft": it is not clinical
    documentation until a clinician reviews and approves it.
    """
    if _find_patient(ehr_system, patient_id) is None:
        raise ResourceNotFoundError(
            f"No patient found for patient_id={patient_id!r} in ehr_system={ehr_system!r}; "
            "confirm identity with search_ehr_patient before generating a note."
        )

    note_id = str(uuid.uuid4())
    generated_at = datetime.now(timezone.utc).isoformat()

    _append_audit_entry(
        {
            "timestamp": generated_at,
            "action": "generate_note",
            "note_id": note_id,
            "ehr_system": ehr_system,
            "patient_id": patient_id,
            "transcript": transcript,
            "note_text": note_text,
            "status": "draft",
        }
    )

    return GeneratedNote(
        note_id=note_id,
        patient_id=patient_id,
        ehr_system=ehr_system,
        status="draft",
        note_text=note_text,
        generated_at=generated_at,
    )


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

Handle these three situations explicitly, the way you would explain an edge case to a colleague -- do not guess past any of them:
- Information missing: search_ehr_patient has neither an MRN nor a last_name+date_of_birth to work with. Say plainly what's missing and stop; do not fabricate an identifier.
- Input ambiguous: search_ehr_patient returns more than one candidate. List them (name, DOB, MRN) and ask which one is meant; do not pick for the clinician.
- Nothing to report: search_ehr_patient returns zero matches. Say plainly that no patient was found in {ehr_system}, suggest checking the details or trying the other EHR system, and stop -- do not invent a patient or draft a note without one."""


if __name__ == "__main__":
    mcp.run()
