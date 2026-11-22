"""Encounter-note decision tools: approve or reject a draft (one decision per note).

Split out of server.py (Phase 0, no behaviour change).
"""

from datetime import datetime, timezone
from typing import Annotated

from mcp.server.mcpserver.exceptions import ResourceNotFoundError, ToolError
from pydantic import Field

import review_tracking
from app_core import (
    REVIEW_TIME_TARGET_SECONDS,
    SPEED_UP_SUGGESTIONS,
    _append_audit_entry,
    _logger,
    mcp,
)
from audit_queries import _current_note_text, _find_note
from logging_utils import (
    ACCESS_DENIED,
    TOOL_COMPLETED,
    TOOL_ERROR,
    TOOL_STARTED,
    log_event,
    new_correlation_id,
)
from models import ReviewedNote


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
        _logger,
        "info",
        TOOL_STARTED,
        correlation_id,
        tool="approve_encounter_note",
        note_id=note_id,
    )

    record = _find_note(note_id)
    if record is None:
        log_event(
            _logger,
            "warning",
            TOOL_ERROR,
            correlation_id,
            tool="approve_encounter_note",
            error_class="ResourceNotFoundError",
        )
        raise ResourceNotFoundError(f"No generated note found for note_id={note_id!r}.")

    generated = record["generated"]
    decision = record["decision"]
    final_text = (
        edited_note_text if edited_note_text is not None else _current_note_text(record)
    )

    if decision is not None:
        if decision["action"] == "approve_note" and decision["note_text"] == final_text:
            documentation_time_seconds = decision.get(
                "documentation_time_seconds",
                _documentation_time_seconds(
                    generated["timestamp"], decision["timestamp"]
                ),
            )
            log_event(
                _logger,
                "info",
                TOOL_COMPLETED,
                correlation_id,
                tool="approve_encounter_note",
                note_id=note_id,
                outcome="success",
                no_op=True,
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
                review=decision.get("review"),
            )
        log_event(
            _logger,
            "warning",
            ACCESS_DENIED,
            correlation_id,
            tool="approve_encounter_note",
            note_id=note_id,
            reason="conflicting_decision",
            error_class="AccessDenied",
        )
        raise ToolError(
            f"note_id={note_id!r} already has a recorded decision ({decision['action']}); "
            "a note gets exactly one clinician decision."
        )

    reviewed_at = datetime.now(timezone.utc).isoformat()
    documentation_time_seconds = _documentation_time_seconds(
        generated["timestamp"], reviewed_at
    )
    exceeded_target = documentation_time_seconds > REVIEW_TIME_TARGET_SECONDS
    suggestions = _speed_up_suggestions(documentation_time_seconds)
    review = (
        review_tracking.clinician_entered()
        if generated.get("source") == "clinician"
        else review_tracking.review_text(
            generated["note_text"], final_text, generated["timestamp"], reviewed_at
        )
    )
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
            "review": review.model_dump(),
        }
    )
    log_event(
        _logger,
        "info",
        TOOL_COMPLETED,
        correlation_id,
        tool="approve_encounter_note",
        note_id=note_id,
        outcome="success",
        no_op=False,
        documentation_time_seconds=documentation_time_seconds,
        exceeded_target=exceeded_target,
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
        review=review,
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
        _logger,
        "info",
        TOOL_STARTED,
        correlation_id,
        tool="reject_encounter_note",
        note_id=note_id,
    )

    record = _find_note(note_id)
    if record is None:
        log_event(
            _logger,
            "warning",
            TOOL_ERROR,
            correlation_id,
            tool="reject_encounter_note",
            error_class="ResourceNotFoundError",
        )
        raise ResourceNotFoundError(f"No generated note found for note_id={note_id!r}.")

    generated = record["generated"]
    decision = record["decision"]
    current_text = _current_note_text(record)

    if decision is not None:
        if decision["action"] == "reject_note" and decision.get("feedback") == feedback:
            documentation_time_seconds = decision.get(
                "documentation_time_seconds",
                _documentation_time_seconds(
                    generated["timestamp"], decision["timestamp"]
                ),
            )
            log_event(
                _logger,
                "info",
                TOOL_COMPLETED,
                correlation_id,
                tool="reject_encounter_note",
                note_id=note_id,
                outcome="success",
                no_op=True,
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
            _logger,
            "warning",
            ACCESS_DENIED,
            correlation_id,
            tool="reject_encounter_note",
            note_id=note_id,
            reason="conflicting_decision",
            error_class="AccessDenied",
        )
        raise ToolError(
            f"note_id={note_id!r} already has a recorded decision ({decision['action']}); "
            "a note gets exactly one clinician decision."
        )

    reviewed_at = datetime.now(timezone.utc).isoformat()
    documentation_time_seconds = _documentation_time_seconds(
        generated["timestamp"], reviewed_at
    )
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
        _logger,
        "info",
        TOOL_COMPLETED,
        correlation_id,
        tool="reject_encounter_note",
        note_id=note_id,
        outcome="success",
        no_op=False,
        documentation_time_seconds=documentation_time_seconds,
        exceeded_target=exceeded_target,
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
