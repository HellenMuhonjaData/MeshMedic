"""Encounter-note drafting tools: record an AI draft, save clinician edits.

Split out of server.py (Phase 0, no behaviour change).
"""

import uuid
from datetime import datetime, timezone
from typing import Annotated, Literal

from mcp.server.mcpserver.exceptions import ResourceNotFoundError, ToolError
from pydantic import Field

import attestation
import audit_actor
import clinician_actor
import encounter_records
from app_core import CONFIDENCE_THRESHOLD, _append_audit_entry, _logger, mcp
from audit_queries import _all_audit_entries, _find_note, _find_patient
from logging_utils import (
    ACCESS_DENIED,
    TOOL_COMPLETED,
    TOOL_ERROR,
    TOOL_STARTED,
    log_event,
    new_correlation_id,
)
from models import EditedNote, GeneratedNote


@mcp.tool()
def generate_encounter_note(
    ehr_system: Literal["epic", "oracle_health"],
    patient_id: Annotated[str, Field(min_length=1)],
    transcript: Annotated[str, Field(min_length=1, max_length=20000)],
    note_text: Annotated[str, Field(min_length=1, max_length=5000)],
    confidence: Annotated[float, Field(ge=0.0, le=1.0)],
    confidence_reason: Annotated[str | None, Field(max_length=1000)] = None,
    source: Literal["ai", "clinician"] = "ai",
    encounter_id: Annotated[str | None, Field(max_length=64)] = None,
    idempotency_key: Annotated[str | None, Field(max_length=200)] = None,
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
        _logger,
        "info",
        TOOL_STARTED,
        correlation_id,
        tool="generate_encounter_note",
        ehr_system=ehr_system,
        patient_id=patient_id,
    )

    if _find_patient(ehr_system, patient_id) is None:
        log_event(
            _logger,
            "warning",
            TOOL_ERROR,
            correlation_id,
            tool="generate_encounter_note",
            error_class="ResourceNotFoundError",
        )
        raise ResourceNotFoundError(
            f"No patient found for patient_id={patient_id!r} in ehr_system={ehr_system!r}; "
            "confirm identity with search_ehr_patient before generating a note."
        )

    flagged = confidence < CONFIDENCE_THRESHOLD
    if flagged and not confidence_reason:
        log_event(
            _logger,
            "warning",
            TOOL_ERROR,
            correlation_id,
            tool="generate_encounter_note",
            error_class="ValidationError",
        )
        raise ToolError(
            f"confidence={confidence!r} is below the low-confidence threshold "
            f"({CONFIDENCE_THRESHOLD}); confidence_reason is required so the "
            "warning carries an explanation (REQ-008)."
        )

    # Phase 2: file the note under its encounter (same patient only), and let a
    # retried background job get the note it already created instead of a duplicate.
    if encounter_id is not None or idempotency_key is not None:
        entries = _all_audit_entries()
        if encounter_id is not None:
            encounter = encounter_records.find_encounter(entries, encounter_id)
            if encounter is None:
                raise ResourceNotFoundError(f"No encounter {encounter_id!r}.")
            if (encounter["ehr_system"], encounter["patient_id"]) != (ehr_system, patient_id):
                raise ToolError("This encounter belongs to a different patient; the note was not recorded.")
        existing = encounter_records.find_by_idempotency_key(entries, "generate_note", idempotency_key)
        if existing is not None:
            if existing.get("encounter_id") != encounter_id or existing["patient_id"] != patient_id:
                raise ToolError("That idempotency_key already recorded a note for a different encounter or patient.")
            return GeneratedNote(
                note_id=existing["note_id"], patient_id=existing["patient_id"], ehr_system=existing["ehr_system"],
                status="draft", note_text=existing["note_text"], generated_at=existing["timestamp"],
                confidence=existing["confidence"], flagged=existing["flagged"],
                warning=(f"Low confidence ({existing['confidence']:.2f}): {existing.get('confidence_reason')}"
                         if existing["flagged"] else None),
            )

    note_id = str(uuid.uuid4())
    generated_at = datetime.now(timezone.utc).isoformat()
    warning = (
        f"Low confidence ({confidence:.2f}): {confidence_reason}" if flagged else None
    )

    _append_audit_entry(
        {
            "timestamp": generated_at,
            "action": "generate_note",
            "note_id": note_id,
            "ehr_system": ehr_system,
            "patient_id": patient_id,
            "transcript": transcript,
            "note_text": note_text,
            "source": source,
            "actor": audit_actor.for_source(source),
            "confidence": confidence,
            "flagged": flagged,
            "confidence_reason": confidence_reason,
            "status": "draft",
            "encounter_id": encounter_id,
            "idempotency_key": idempotency_key,
        }
    )
    log_event(
        _logger,
        "info",
        TOOL_COMPLETED,
        correlation_id,
        tool="generate_encounter_note",
        outcome="success",
        note_id=note_id,
        flagged=flagged,
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


@mcp.tool()
def edit_encounter_note(
    note_id: Annotated[str, Field(min_length=1)],
    edited_note_text: Annotated[str, Field(min_length=1, max_length=5000)],
    actor_attestation: Annotated[str | None, Field(max_length=4000)] = None,
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
        _logger,
        "info",
        TOOL_STARTED,
        correlation_id,
        tool="edit_encounter_note",
        note_id=note_id,
    )

    # Signed-in clinician (attested by the web app) or, without an attestation, unverified.
    try:
        actor = clinician_actor.resolve_review_actor(actor_attestation, note_id)
    except attestation.AttestationError as exc:
        raise ToolError(f"Refused: {exc}.") from exc
    record = _find_note(note_id)
    if record is None:
        log_event(
            _logger,
            "warning",
            TOOL_ERROR,
            correlation_id,
            tool="edit_encounter_note",
            error_class="ResourceNotFoundError",
        )
        raise ResourceNotFoundError(f"No generated note found for note_id={note_id!r}.")

    if record["decision"] is not None:
        log_event(
            _logger,
            "warning",
            ACCESS_DENIED,
            correlation_id,
            tool="edit_encounter_note",
            note_id=note_id,
            reason="note_locked_after_decision",
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
            _logger,
            "info",
            TOOL_COMPLETED,
            correlation_id,
            tool="edit_encounter_note",
            note_id=note_id,
            outcome="success",
            no_op=True,
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
            "actor": actor,
            "note_id": note_id,
            "ehr_system": generated["ehr_system"],
            "patient_id": generated["patient_id"],
            "note_text": edited_note_text,
            "status": "draft",
        }
    )
    log_event(
        _logger,
        "info",
        TOOL_COMPLETED,
        correlation_id,
        tool="edit_encounter_note",
        note_id=note_id,
        outcome="success",
        no_op=False,
    )
    return EditedNote(
        note_id=note_id,
        patient_id=generated["patient_id"],
        ehr_system=generated["ehr_system"],
        status="draft",
        note_text=edited_note_text,
        edited_at=edited_at,
    )
