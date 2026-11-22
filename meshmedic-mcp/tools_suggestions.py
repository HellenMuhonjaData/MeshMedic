"""AI suggestions (care gaps, test orders, referral) recorded before a clinician accepts them.

Split out of server.py (Phase 0, no behaviour change).
"""

from datetime import datetime, timezone
from typing import Annotated

from mcp.server.mcpserver.exceptions import ResourceNotFoundError, ToolError
from pydantic import Field

import ai_suggestions
import audit_actor
from ai_suggestions import RecordedSuggestions, ReferralSuggestion, TestOrderSuggestion
from app_core import CONFIDENCE_THRESHOLD, _append_audit_entry, _logger, mcp
from audit_queries import _all_audit_entries, _find_note
from logging_utils import (
    TOOL_COMPLETED,
    TOOL_ERROR,
    TOOL_STARTED,
    log_event,
    new_correlation_id,
)
from models import CareGap, CareGapInput, TestOrder


def _resolve_suggestion_item(
    entries: list[dict],
    item_id: str,
    kind: str,
    note_id: str,
    tool: str,
    correlation_id: str,
) -> tuple[dict, dict]:
    """Look up an AI-suggested item being accepted; it must exist, be the
    right kind, and belong to this note (never accept another chart's item)."""
    found = ai_suggestions.find_item(entries, item_id)
    if (
        found is None
        or found[1].get("kind") != kind
        or found[0].get("note_id") != note_id
    ):
        log_event(
            _logger,
            "warning",
            TOOL_ERROR,
            correlation_id,
            tool=tool,
            error_class="ValidationError",
        )
        raise ToolError(
            f"suggestion_item_id={item_id!r} is not a recorded AI {kind} suggestion for "
            f"note_id={note_id!r}. Use an item_id returned by record_ai_suggestions for this note."
        )
    return found


def _accepted_entry(
    entries: list[dict], action: str, item_id: str | None
) -> dict | None:
    if not item_id:
        return None
    for entry in entries:
        if entry.get("action") == action and entry.get("suggestion_item_id") == item_id:
            return entry
    return None


def _care_gap_from_entry(entry: dict) -> "CareGap":
    flagged = bool(entry.get("flagged"))
    return CareGap(
        gap_id=entry["gap_id"],
        note_id=entry["note_id"],
        patient_id=entry["patient_id"],
        ehr_system=entry["ehr_system"],
        status="flagged",
        description=entry["description"],
        confidence=entry["confidence"],
        flagged=flagged,
        warning=(
            f"Low confidence ({entry['confidence']:.2f}): {entry.get('confidence_reason')}"
            if flagged
            else None
        ),
        identified_at=entry["timestamp"],
        review=entry.get("review"),
    )


def _test_order_from_entry(entry: dict) -> "TestOrder":
    return TestOrder(
        order_id=entry["order_id"],
        note_id=entry["note_id"],
        patient_id=entry["patient_id"],
        ehr_system=entry["ehr_system"],
        status="pending",
        test_name=entry["test_name"],
        reason=entry["reason"],
        ordered_at=entry["timestamp"],
        review=entry.get("review"),
    )


@mcp.tool()
def record_ai_suggestions(
    note_id: Annotated[str, Field(min_length=1)],
    care_gaps: list[CareGapInput] | None = None,
    test_orders: list[TestOrderSuggestion] | None = None,
    referral: ReferralSuggestion | None = None,
) -> RecordedSuggestions:
    """
    Record what the AI proposed for an encounter -- care gaps, test orders
    and a referral -- before the clinician acts on any of it (REQ-006,
    REQ-014). Nothing here is a clinical record: each item gets an
    `item_id`, and only when the clinician accepts an item (possibly after
    editing it) does it become a care gap (identify_care_gaps with
    suggestion_item_id) or a test order (order_test with suggestion_item_id).
    The server then compares the accepted version with this original, so the
    audit trail shows whether the clinician approved the AI's version as-is
    or changed it, and how quickly.

    Idempotent: recording identical suggestions for the same note again
    returns the first record instead of writing a duplicate.
    """
    tool = "record_ai_suggestions"
    correlation_id = new_correlation_id()
    log_event(_logger, "info", TOOL_STARTED, correlation_id, tool=tool, note_id=note_id)

    note_record = _find_note(note_id)
    if note_record is None:
        log_event(
            _logger,
            "warning",
            TOOL_ERROR,
            correlation_id,
            tool=tool,
            error_class="ResourceNotFoundError",
        )
        raise ResourceNotFoundError(
            f"No generated note found for note_id={note_id!r}; record the note "
            "(generate_encounter_note) before its suggestions."
        )
    for gap in care_gaps or []:
        if gap.confidence < CONFIDENCE_THRESHOLD and not gap.confidence_reason:
            log_event(
                _logger,
                "warning",
                TOOL_ERROR,
                correlation_id,
                tool=tool,
                error_class="ValidationError",
            )
            raise ToolError(
                f"confidence={gap.confidence!r} for {gap.description!r} is below the "
                f"low-confidence threshold ({CONFIDENCE_THRESHOLD}); confidence_reason is "
                "required (REQ-008)."
            )

    gap_fields = [
        {
            "description": g.description,
            "confidence": g.confidence,
            "confidence_reason": g.confidence_reason,
        }
        for g in care_gaps or []
    ]
    order_fields = [o.model_dump() for o in test_orders or []]
    referral_fields = referral.model_dump() if referral else None
    key = ai_suggestions.content_key(note_id, gap_fields, order_fields, referral_fields)
    generated = note_record["generated"]

    existing = ai_suggestions.find_existing(_all_audit_entries(), key)
    if existing is None:
        existing = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "action": ai_suggestions.RECORD_ACTION,
            "note_id": note_id,
            "ehr_system": generated["ehr_system"],
            "patient_id": generated["patient_id"],
            "content_key": key,
            "actor": dict(audit_actor.AI),
            "items": ai_suggestions.build_items(
                gap_fields, order_fields, referral_fields
            ),
        }
        _append_audit_entry(existing)
        no_op = False
    else:
        no_op = True
    log_event(
        _logger,
        "info",
        TOOL_COMPLETED,
        correlation_id,
        tool=tool,
        note_id=note_id,
        outcome="success",
        no_op=no_op,
        item_count=len(existing["items"]),
    )
    return RecordedSuggestions(
        note_id=note_id,
        patient_id=existing["patient_id"],
        recorded_at=existing["timestamp"],
        items=existing["items"],
    )
