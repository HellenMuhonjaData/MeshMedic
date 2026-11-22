"""Encounter tools (Phase 2, STORY-015): start an encounter, list them, open one.

Every note, code set and suggestion produced in the background is filed
under an encounter, and the server refuses to file anything under an
encounter that belongs to a different patient.
"""

import uuid
from datetime import datetime, timezone
from typing import Annotated, Literal

from mcp.server.mcpserver.exceptions import ResourceNotFoundError, ToolError
from pydantic import BaseModel, Field

import attestation
import audit_actor
import clinician_actor
import encounter_records
import encounters
from app_core import _append_audit_entry, mcp
from audit_queries import _all_audit_entries, _find_patient


class Encounter(BaseModel):
    encounter_id: str
    ehr_system: str
    patient_id: str
    started_at: str
    started_by: str | None


class EncounterSummary(BaseModel):
    encounter_id: str
    ehr_system: str
    patient_id: str
    patient_name: str
    mrn: str | None
    started_at: str
    started_by: str | None
    note_id: str | None
    note_status: str | None
    note_flagged: bool
    signed_by: str | None
    signed_at: str | None


class EncounterList(BaseModel):
    encounters: list[EncounterSummary]


class EncounterBundle(BaseModel):
    encounter: EncounterSummary
    note: dict | None
    code_set: dict | None
    suggestions: list[dict]
    care_gaps: list[dict]
    orders: list[dict]


def _encounter(entry: dict) -> Encounter:
    actor = audit_actor.entry_actor(entry)
    return Encounter(
        encounter_id=entry["encounter_id"], ehr_system=entry["ehr_system"], patient_id=entry["patient_id"],
        started_at=entry["timestamp"], started_by=actor.get("clinician_id"),
    )


def _summary(entries: list[dict], start: dict) -> EncounterSummary:
    patient = _find_patient(start["ehr_system"], start["patient_id"]) or {}
    note = encounter_records.bundle(entries, start["encounter_id"])["note"]
    return EncounterSummary(
        **_encounter(start).model_dump(),
        patient_name=f"{patient.get('first_name', '')} {patient.get('last_name', '')}".strip() or start["patient_id"],
        mrn=patient.get("mrn"),
        note_id=(note or {}).get("note_id"),
        note_status=(note or {}).get("status"),
        note_flagged=bool((note or {}).get("flagged")),
        signed_by=(note or {}).get("signed_by"),
        signed_at=(note or {}).get("signed_at"),
    )


def _actor(actor_attestation: str | None, record_id: str) -> dict:
    try:
        return clinician_actor.resolve_review_actor(actor_attestation, record_id)
    except attestation.AttestationError as exc:
        raise ToolError(f"Refused: {exc}.") from exc


@mcp.tool()
def start_encounter(
    ehr_system: Literal["epic", "oracle_health"],
    patient_id: Annotated[str, Field(min_length=1)],
    idempotency_key: Annotated[str, Field(min_length=8, max_length=100)],
    actor_attestation: Annotated[str | None, Field(max_length=4000)] = None,
) -> Encounter:
    """
    Start one encounter (patient visit) for a confirmed patient. Its random
    encounter_id carries no patient information. Starting again with the same
    idempotency_key returns the same encounter instead of creating a second.
    """
    if _find_patient(ehr_system, patient_id) is None:
        raise ResourceNotFoundError(f"No patient {patient_id!r} in {ehr_system}.")
    entries = _all_audit_entries()
    existing = encounter_records.find_by_idempotency_key(entries, encounter_records.START_ACTION, idempotency_key)
    if existing is not None:
        if (existing["ehr_system"], existing["patient_id"]) != (ehr_system, patient_id):
            raise ToolError("That idempotency_key already started an encounter for a different patient.")
        return _encounter(existing)
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "action": encounter_records.START_ACTION,
        "encounter_id": encounters.new_encounter_id(),
        "ehr_system": ehr_system,
        "patient_id": patient_id,
        "idempotency_key": idempotency_key,
        "actor": _actor(actor_attestation, idempotency_key),
    }
    _append_audit_entry(entry)
    return _encounter(entry)


@mcp.tool()
def list_encounters(
    started_by: Annotated[str | None, Field(max_length=64)] = None,
    limit: Annotated[int, Field(ge=1, le=500)] = 100,
) -> EncounterList:
    """Encounters, newest first, with the note's review status. Optionally only those one clinician started."""
    entries = _all_audit_entries()
    starts = [e for e in entries if e.get("action") == encounter_records.START_ACTION]
    if started_by:
        starts = [e for e in starts if audit_actor.entry_actor(e).get("clinician_id") == started_by]
    return EncounterList(encounters=[_summary(entries, s) for s in reversed(starts[-limit:])])


@mcp.tool()
def get_encounter_bundle(
    encounter_id: Annotated[str, Field(min_length=1, max_length=64)],
    actor_attestation: Annotated[str | None, Field(max_length=4000)] = None,
) -> EncounterBundle:
    """
    Open one encounter for review: the note as it stands (with any saved
    edits), its code set, AI suggestions and their status, care gaps and
    orders. Each view is recorded in the audit trail (ids only).
    """
    if not encounters.is_encounter_id(encounter_id):
        raise ToolError("encounter_id is not a valid encounter identifier.")
    entries = _all_audit_entries()
    data = encounter_records.bundle(entries, encounter_id)
    if data is None:
        raise ResourceNotFoundError(f"No encounter {encounter_id!r}.")
    _append_audit_entry({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "action": encounter_records.VIEW_ACTION,
        "view_id": str(uuid.uuid4()),
        "encounter_id": encounter_id,
        "patient_id": data["encounter"]["patient_id"],
        "actor": _actor(actor_attestation, encounter_id),
    })
    return EncounterBundle(encounter=_summary(entries, data["encounter"]),
                           **{k: v for k, v in data.items() if k != "encounter"})
