"""Test-order and follow-up tools.

Split out of server.py (Phase 0, no behaviour change).
"""

import uuid
from datetime import datetime, timezone
from typing import Annotated

from mcp.server.mcpserver.exceptions import ResourceNotFoundError, ToolError
from pydantic import Field

import review_tracking
from app_core import _append_audit_entry, _logger, mcp
from audit_queries import (
    _all_audit_entries,
    _find_note,
    _find_order,
    _open_care_gaps_for_patient,
    _orders_for_patient,
)
from logging_utils import (
    ACCESS_DENIED,
    TOOL_COMPLETED,
    TOOL_ERROR,
    TOOL_STARTED,
    log_event,
    new_correlation_id,
)
from models import OpenCareGapSummary, PendingItemsSummary, ResultedTestOrder, TestOrder
from tools_suggestions import (
    _accepted_entry,
    _resolve_suggestion_item,
    _test_order_from_entry,
)


@mcp.tool()
def order_test(
    note_id: Annotated[str, Field(min_length=1)],
    test_name: Annotated[str, Field(min_length=1, max_length=200)],
    reason: Annotated[str, Field(min_length=1, max_length=500)],
    suggestion_item_id: str | None = None,
) -> TestOrder:
    """
    Record that a clinician ordered a diagnostic test following an encounter
    (labs, imaging, ...), so the order is traceable in the audit trail
    (REQ-006) and visible when this patient is seen again. Call this only
    for a note_id returned by generate_encounter_note.

    This is a clinician action, not an AI suggestion -- unlike suggest_codes
    or identify_care_gaps there is no confidence/warning here, since the
    calling model isn't proposing anything for review, it's recording a
    decision the clinician already made. `reason` is the clinical
    justification for the order (e.g. "elevated fasting glucose, rule out
    diabetes"), not a confidence explanation.

    The returned order is always status="pending" -- see record_test_result
    to close it once a result comes back, and get_pending_items_for_patient
    to see every still-pending order (and open care gap) for a patient, so a
    follow-up visit isn't started blind to what an earlier one set in
    motion.
    """
    correlation_id = new_correlation_id()
    log_event(
        _logger,
        "info",
        TOOL_STARTED,
        correlation_id,
        tool="order_test",
        note_id=note_id,
        test_name=test_name,
    )

    note_record = _find_note(note_id)
    if note_record is None:
        log_event(
            _logger,
            "warning",
            TOOL_ERROR,
            correlation_id,
            tool="order_test",
            error_class="ResourceNotFoundError",
        )
        raise ResourceNotFoundError(
            f"No generated note found for note_id={note_id!r}; confirm the note "
            "exists (generate_encounter_note) before ordering a test against it."
        )

    entries = _all_audit_entries()
    review = review_tracking.clinician_entered()
    if suggestion_item_id:
        existing = _accepted_entry(entries, "order_test", suggestion_item_id)
        if existing is not None:
            # Idempotent: this AI-suggested test was already ordered.
            return _test_order_from_entry(existing)
        record_entry, item = _resolve_suggestion_item(
            entries,
            suggestion_item_id,
            "test_order",
            note_id,
            "order_test",
            correlation_id,
        )
    generated = note_record["generated"]
    order_id = str(uuid.uuid4())
    ordered_at = datetime.now(timezone.utc).isoformat()
    if suggestion_item_id:
        review = review_tracking.review_fields(
            {
                "test_name": item["fields"].get("test_name") or "",
                "reason": item["fields"].get("reason") or "",
            },
            {"test_name": test_name, "reason": reason},
            record_entry["timestamp"],
            ordered_at,
        )
    _append_audit_entry(
        {
            "timestamp": ordered_at,
            "action": "order_test",
            "order_id": order_id,
            "note_id": note_id,
            "ehr_system": generated["ehr_system"],
            "patient_id": generated["patient_id"],
            "test_name": test_name,
            "reason": reason,
            "status": "pending",
            "suggestion_item_id": suggestion_item_id,
            "review": review.model_dump(),
        }
    )
    log_event(
        _logger,
        "info",
        TOOL_COMPLETED,
        correlation_id,
        tool="order_test",
        note_id=note_id,
        order_id=order_id,
        outcome="success",
    )
    return TestOrder(
        order_id=order_id,
        note_id=note_id,
        patient_id=generated["patient_id"],
        ehr_system=generated["ehr_system"],
        status="pending",
        test_name=test_name,
        reason=reason,
        ordered_at=ordered_at,
        review=review,
    )


@mcp.tool()
def record_test_result(
    order_id: Annotated[str, Field(min_length=1)],
    result_text: Annotated[str, Field(min_length=1, max_length=2000)],
) -> ResultedTestOrder:
    """
    Record that a previously ordered test's result has come back (REQ-006),
    closing the order so the next call to get_pending_items_for_patient
    shows the result instead of an open order. Call this only for an
    order_id returned by order_test.

    Recording an already-resulted order with the identical result_text is a
    no-op that returns the existing record -- it does not write a second
    audit entry. Recording it again with a different result_text raises
    ToolError: an order gets exactly one result, same as a note gets exactly
    one clinician decision and a care gap gets exactly one closing action.
    """
    correlation_id = new_correlation_id()
    log_event(
        _logger,
        "info",
        TOOL_STARTED,
        correlation_id,
        tool="record_test_result",
        order_id=order_id,
    )

    record = _find_order(order_id)
    if record is None:
        log_event(
            _logger,
            "warning",
            TOOL_ERROR,
            correlation_id,
            tool="record_test_result",
            error_class="ResourceNotFoundError",
        )
        raise ResourceNotFoundError(f"No test order found for order_id={order_id!r}.")

    ordered = record["ordered"]
    resulted = record["resulted"]

    if resulted is not None:
        if resulted["result_text"] == result_text:
            log_event(
                _logger,
                "info",
                TOOL_COMPLETED,
                correlation_id,
                tool="record_test_result",
                order_id=order_id,
                outcome="success",
                no_op=True,
            )
            return ResultedTestOrder(
                order_id=order_id,
                note_id=ordered["note_id"],
                patient_id=ordered["patient_id"],
                ehr_system=ordered["ehr_system"],
                status="resulted",
                test_name=ordered["test_name"],
                reason=ordered["reason"],
                result_text=resulted["result_text"],
                ordered_at=ordered["timestamp"],
                resulted_at=resulted["timestamp"],
            )
        log_event(
            _logger,
            "warning",
            ACCESS_DENIED,
            correlation_id,
            tool="record_test_result",
            order_id=order_id,
            reason="order_already_resulted",
            error_class="AccessDenied",
        )
        raise ToolError(
            f"order_id={order_id!r} already has a recorded result; "
            "a test order gets exactly one result."
        )

    resulted_at = datetime.now(timezone.utc).isoformat()
    _append_audit_entry(
        {
            "timestamp": resulted_at,
            "action": "record_test_result",
            "order_id": order_id,
            "note_id": ordered["note_id"],
            "ehr_system": ordered["ehr_system"],
            "patient_id": ordered["patient_id"],
            "result_text": result_text,
            "status": "resulted",
        }
    )
    log_event(
        _logger,
        "info",
        TOOL_COMPLETED,
        correlation_id,
        tool="record_test_result",
        order_id=order_id,
        outcome="success",
        no_op=False,
    )
    return ResultedTestOrder(
        order_id=order_id,
        note_id=ordered["note_id"],
        patient_id=ordered["patient_id"],
        ehr_system=ordered["ehr_system"],
        status="resulted",
        test_name=ordered["test_name"],
        reason=ordered["reason"],
        result_text=result_text,
        ordered_at=ordered["timestamp"],
        resulted_at=resulted_at,
    )


@mcp.tool()
def get_pending_items_for_patient(
    patient_id: Annotated[str, Field(min_length=1)],
) -> PendingItemsSummary:
    """
    Continuity view for a returning patient (REQ-006): every care gap still
    open, every test order still pending, and every test result recorded --
    across every past encounter for this patient, not just the most recent
    one. Call this at the start of a new encounter (after search_ehr_patient
    confirms identity, before drafting a note) so a follow-up visit isn't
    started blind to what an earlier one set in motion -- e.g. a test was
    ordered and the result is now back, or a care gap from a prior visit was
    never addressed.

    Nothing here is computed or inferred -- it's a replay of this patient's
    own audit trail, the same source-of-truth pattern every other read in
    this server uses (see _open_care_gaps_for_patient, _orders_for_patient).
    """
    correlation_id = new_correlation_id()
    log_event(
        _logger,
        "info",
        TOOL_STARTED,
        correlation_id,
        tool="get_pending_items_for_patient",
        patient_id=patient_id,
    )

    open_gaps = [
        OpenCareGapSummary(gap_id=g["gap_id"], description=g["description"])
        for g in _open_care_gaps_for_patient(patient_id)
    ]
    pending_raw, resulted_raw = _orders_for_patient(patient_id)
    pending_orders = [
        TestOrder(
            order_id=o["order_id"],
            note_id=o["note_id"],
            patient_id=o["patient_id"],
            ehr_system=o["ehr_system"],
            status="pending",
            test_name=o["test_name"],
            reason=o["reason"],
            ordered_at=o["timestamp"],
        )
        for o in pending_raw
    ]
    resulted_orders = [
        ResultedTestOrder(
            order_id=r["resulted"]["order_id"],
            note_id=r["ordered"]["note_id"],
            patient_id=r["ordered"]["patient_id"],
            ehr_system=r["ordered"]["ehr_system"],
            status="resulted",
            test_name=r["ordered"]["test_name"],
            reason=r["ordered"]["reason"],
            result_text=r["resulted"]["result_text"],
            ordered_at=r["ordered"]["timestamp"],
            resulted_at=r["resulted"]["timestamp"],
        )
        for r in resulted_raw
    ]
    checked_at = datetime.now(timezone.utc).isoformat()
    log_event(
        _logger,
        "info",
        TOOL_COMPLETED,
        correlation_id,
        tool="get_pending_items_for_patient",
        outcome="success",
        open_gap_count=len(open_gaps),
        pending_order_count=len(pending_orders),
        resulted_order_count=len(resulted_orders),
    )
    return PendingItemsSummary(
        patient_id=patient_id,
        open_care_gaps=open_gaps,
        pending_orders=pending_orders,
        resulted_orders=resulted_orders,
        checked_at=checked_at,
    )
