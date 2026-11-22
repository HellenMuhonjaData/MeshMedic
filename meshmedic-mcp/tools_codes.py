"""ICD-10/CPT code suggestion tools and their clinician decisions.

Split out of server.py (Phase 0, no behaviour change).
"""

import uuid
from datetime import datetime, timezone
from typing import Annotated, Literal

from mcp.server.mcpserver.exceptions import ResourceNotFoundError, ToolError
from pydantic import Field

import audit_actor
import review_tracking
from app_core import CONFIDENCE_THRESHOLD, _append_audit_entry, _logger, mcp
from audit_queries import _find_note, _find_suggestion
from logging_utils import (
    ACCESS_DENIED,
    TOOL_COMPLETED,
    TOOL_ERROR,
    TOOL_STARTED,
    log_event,
    new_correlation_id,
)
from models import CodeSuggestion, ReviewedCodes, SuggestedCodes


@mcp.tool()
def suggest_codes(
    note_id: Annotated[str, Field(min_length=1)],
    codes: Annotated[list[CodeSuggestion], Field(min_length=1)],
    confidence: Annotated[float, Field(ge=0.0, le=1.0)],
    confidence_reason: Annotated[str | None, Field(max_length=1000)] = None,
    source: Literal["ai", "clinician"] = "ai",
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
        _logger,
        "info",
        TOOL_STARTED,
        correlation_id,
        tool="suggest_codes",
        note_id=note_id,
        code_count=len(codes),
    )

    note_record = _find_note(note_id)
    if note_record is None:
        log_event(
            _logger,
            "warning",
            TOOL_ERROR,
            correlation_id,
            tool="suggest_codes",
            error_class="ResourceNotFoundError",
        )
        raise ResourceNotFoundError(
            f"No generated note found for note_id={note_id!r}; confirm the note "
            "exists (generate_encounter_note) before suggesting codes for it."
        )

    flagged = confidence < CONFIDENCE_THRESHOLD
    if flagged and not confidence_reason:
        log_event(
            _logger,
            "warning",
            TOOL_ERROR,
            correlation_id,
            tool="suggest_codes",
            error_class="ValidationError",
        )
        raise ToolError(
            f"confidence={confidence!r} is below the low-confidence threshold "
            f"({CONFIDENCE_THRESHOLD}); confidence_reason is required so the "
            "warning carries an explanation (REQ-008)."
        )

    generated = note_record["generated"]
    suggestion_id = str(uuid.uuid4())
    suggested_at = datetime.now(timezone.utc).isoformat()
    warning = (
        f"Low confidence ({confidence:.2f}): {confidence_reason}" if flagged else None
    )

    _append_audit_entry(
        {
            "timestamp": suggested_at,
            "action": "suggest_codes",
            "suggestion_id": suggestion_id,
            "note_id": note_id,
            "ehr_system": generated["ehr_system"],
            "patient_id": generated["patient_id"],
            "codes": [c.model_dump() for c in codes],
            "source": source,
            "actor": audit_actor.for_source(source),
            "confidence": confidence,
            "flagged": flagged,
            "confidence_reason": confidence_reason,
            "status": "draft",
        }
    )
    log_event(
        _logger,
        "info",
        TOOL_COMPLETED,
        correlation_id,
        tool="suggest_codes",
        outcome="success",
        suggestion_id=suggestion_id,
        code_count=len(codes),
        flagged=flagged,
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
        _logger,
        "info",
        TOOL_STARTED,
        correlation_id,
        tool="approve_codes",
        suggestion_id=suggestion_id,
    )

    record = _find_suggestion(suggestion_id)
    if record is None:
        log_event(
            _logger,
            "warning",
            TOOL_ERROR,
            correlation_id,
            tool="approve_codes",
            error_class="ResourceNotFoundError",
        )
        raise ResourceNotFoundError(
            f"No code suggestion found for suggestion_id={suggestion_id!r}."
        )

    suggested = record["suggested"]
    decision = record["decision"]
    final_codes = (
        edited_codes
        if edited_codes is not None
        else [CodeSuggestion(**c) for c in suggested["codes"]]
    )
    final_codes_dump = [c.model_dump() for c in final_codes]

    if decision is not None:
        if (
            decision["action"] == "approve_codes"
            and decision["codes"] == final_codes_dump
        ):
            log_event(
                _logger,
                "info",
                TOOL_COMPLETED,
                correlation_id,
                tool="approve_codes",
                suggestion_id=suggestion_id,
                outcome="success",
                no_op=True,
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
                review=decision.get("review"),
            )
        log_event(
            _logger,
            "warning",
            ACCESS_DENIED,
            correlation_id,
            tool="approve_codes",
            suggestion_id=suggestion_id,
            reason="conflicting_decision",
            error_class="AccessDenied",
        )
        raise ToolError(
            f"suggestion_id={suggestion_id!r} already has a recorded decision "
            f"({decision['action']}); a suggestion set gets exactly one clinician decision."
        )

    reviewed_at = datetime.now(timezone.utc).isoformat()
    review = (
        review_tracking.clinician_entered()
        if suggested.get("source") == "clinician"
        else review_tracking.review_codes(
            suggested["codes"], final_codes_dump, suggested["timestamp"], reviewed_at
        )
    )
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
            "review": review.model_dump(),
        }
    )
    log_event(
        _logger,
        "info",
        TOOL_COMPLETED,
        correlation_id,
        tool="approve_codes",
        suggestion_id=suggestion_id,
        outcome="success",
        no_op=False,
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
        review=review,
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
        _logger,
        "info",
        TOOL_STARTED,
        correlation_id,
        tool="reject_codes",
        suggestion_id=suggestion_id,
    )

    record = _find_suggestion(suggestion_id)
    if record is None:
        log_event(
            _logger,
            "warning",
            TOOL_ERROR,
            correlation_id,
            tool="reject_codes",
            error_class="ResourceNotFoundError",
        )
        raise ResourceNotFoundError(
            f"No code suggestion found for suggestion_id={suggestion_id!r}."
        )

    suggested = record["suggested"]
    decision = record["decision"]

    if decision is not None:
        if (
            decision["action"] == "reject_codes"
            and decision.get("feedback") == feedback
        ):
            log_event(
                _logger,
                "info",
                TOOL_COMPLETED,
                correlation_id,
                tool="reject_codes",
                suggestion_id=suggestion_id,
                outcome="success",
                no_op=True,
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
            _logger,
            "warning",
            ACCESS_DENIED,
            correlation_id,
            tool="reject_codes",
            suggestion_id=suggestion_id,
            reason="conflicting_decision",
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
        _logger,
        "info",
        TOOL_COMPLETED,
        correlation_id,
        tool="reject_codes",
        suggestion_id=suggestion_id,
        outcome="success",
        no_op=False,
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
