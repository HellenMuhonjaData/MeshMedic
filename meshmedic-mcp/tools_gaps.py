"""Care-gap tools: flag gaps and address them.

Split out of server.py (Phase 0, no behaviour change).
"""

import uuid
from datetime import datetime, timezone
from typing import Annotated

from mcp.server.mcpserver.exceptions import ResourceNotFoundError, ToolError
from pydantic import Field

import review_tracking
from app_core import CONFIDENCE_THRESHOLD, _append_audit_entry, _logger, mcp
from audit_queries import _all_audit_entries, _find_gap, _find_note
from logging_utils import (
    ACCESS_DENIED,
    TOOL_COMPLETED,
    TOOL_ERROR,
    TOOL_STARTED,
    log_event,
    new_correlation_id,
)
from models import AddressedCareGap, CareGap, CareGapInput
from tools_suggestions import (
    _accepted_entry,
    _care_gap_from_entry,
    _resolve_suggestion_item,
)


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
        _logger,
        "info",
        TOOL_STARTED,
        correlation_id,
        tool="identify_care_gaps",
        note_id=note_id,
        gap_count=len(gaps),
    )

    note_record = _find_note(note_id)
    if note_record is None:
        log_event(
            _logger,
            "warning",
            TOOL_ERROR,
            correlation_id,
            tool="identify_care_gaps",
            error_class="ResourceNotFoundError",
        )
        raise ResourceNotFoundError(
            f"No generated note found for note_id={note_id!r}; confirm the note "
            "exists (generate_encounter_note) before identifying care gaps for it."
        )

    for gap_input in gaps:
        if (
            gap_input.confidence < CONFIDENCE_THRESHOLD
            and not gap_input.confidence_reason
        ):
            log_event(
                _logger,
                "warning",
                TOOL_ERROR,
                correlation_id,
                tool="identify_care_gaps",
                error_class="ValidationError",
            )
            raise ToolError(
                f"confidence={gap_input.confidence!r} for {gap_input.description!r} is below "
                f"the low-confidence threshold ({CONFIDENCE_THRESHOLD}); confidence_reason is "
                "required so the warning carries an explanation (REQ-008)."
            )

    entries = _all_audit_entries()
    suggested_items = {}
    for gap_input in gaps:
        if gap_input.suggestion_item_id:
            suggested_items[gap_input.suggestion_item_id] = _resolve_suggestion_item(
                entries,
                gap_input.suggestion_item_id,
                "care_gap",
                note_id,
                "identify_care_gaps",
                correlation_id,
            )

    generated = note_record["generated"]
    identified_at = datetime.now(timezone.utc).isoformat()
    results: list[CareGap] = []
    for gap_input in gaps:
        item_id = gap_input.suggestion_item_id
        existing = _accepted_entry(entries, "identify_care_gap", item_id)
        if existing is not None:
            # Idempotent: this AI suggestion was already accepted -- return
            # that gap instead of flagging a duplicate.
            results.append(_care_gap_from_entry(existing))
            continue
        if item_id:
            record_entry, item = suggested_items[item_id]
            review = review_tracking.review_fields(
                {"description": item["fields"].get("description") or ""},
                {"description": gap_input.description},
                record_entry["timestamp"],
                identified_at,
            )
        else:
            review = review_tracking.clinician_entered()
        gap_id = str(uuid.uuid4())
        flagged = gap_input.confidence < CONFIDENCE_THRESHOLD
        warning = (
            f"Low confidence ({gap_input.confidence:.2f}): {gap_input.confidence_reason}"
            if flagged
            else None
        )
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
                "suggestion_item_id": item_id,
                "review": review.model_dump(),
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
                review=review,
            )
        )
    log_event(
        _logger,
        "info",
        TOOL_COMPLETED,
        correlation_id,
        tool="identify_care_gaps",
        note_id=note_id,
        outcome="success",
        gap_count=len(results),
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
        _logger,
        "info",
        TOOL_STARTED,
        correlation_id,
        tool="address_care_gap",
        gap_id=gap_id,
    )

    record = _find_gap(gap_id)
    if record is None:
        log_event(
            _logger,
            "warning",
            TOOL_ERROR,
            correlation_id,
            tool="address_care_gap",
            error_class="ResourceNotFoundError",
        )
        raise ResourceNotFoundError(f"No care gap found for gap_id={gap_id!r}.")

    identified = record["identified"]
    addressed = record["addressed"]

    if addressed is not None:
        if addressed["resolution"] == resolution:
            log_event(
                _logger,
                "info",
                TOOL_COMPLETED,
                correlation_id,
                tool="address_care_gap",
                gap_id=gap_id,
                outcome="success",
                no_op=True,
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
            _logger,
            "warning",
            ACCESS_DENIED,
            correlation_id,
            tool="address_care_gap",
            gap_id=gap_id,
            reason="gap_already_closed",
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
        _logger,
        "info",
        TOOL_COMPLETED,
        correlation_id,
        tool="address_care_gap",
        gap_id=gap_id,
        outcome="success",
        no_op=False,
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
