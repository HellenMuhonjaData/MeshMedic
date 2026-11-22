"""Referral-letter tools: draft, approve, list, mark sent, void.

Split out of server.py (Phase 0, no behaviour change).
"""

import uuid
from datetime import datetime, timezone
from typing import Annotated

from mcp.server.mcpserver.exceptions import ResourceNotFoundError, ToolError
from pydantic import Field

import referral_letters
from app_core import _append_audit_entry, _logger, mcp
from audit_queries import _all_audit_entries, _find_note, _find_patient
from logging_utils import (
    ACCESS_DENIED,
    TOOL_COMPLETED,
    TOOL_ERROR,
    TOOL_STARTED,
    log_event,
    new_correlation_id,
)
from referral_letters import (
    ClinicianField,
    LetterStatus,
    MissingEncounterData,
    ReasonField,
    ReferralLetter,
    ReferralLetterList,
    SendMethod,
    SpecialtyField,
    VoidReasonField,
)


def _referral_tool_error(
    tool: str, correlation_id: str, error_class: str, message: str
) -> ToolError:
    log_event(
        _logger,
        "warning",
        TOOL_ERROR,
        correlation_id,
        tool=tool,
        error_class=error_class,
    )
    return ToolError(message)


@mcp.tool()
def generate_referral_letter(
    note_id: Annotated[str, Field(min_length=1)],
    specialty: SpecialtyField,
    referral_reason: ReasonField,
    referring_clinician: ClinicianField,
) -> ReferralLetter:
    """
    Prepare a referral letter from a completed encounter for clinician review
    (REQ-004 / STORY-011). "Completed" means the encounter note has been
    approved via approve_encounter_note -- the letter is built from that
    approved text plus the patient's identity, never from an unreviewed draft.

    The letter is drafted deterministically by this server (see
    referral_letters.render_letter) and returned as status="draft". It is not
    ready to send until a clinician reviews it and calls
    approve_referral_letter. The creation is recorded in the audit trail.

    Idempotent: calling again with the same note_id, specialty and
    referral_reason returns the existing letter (draft or approved) instead
    of creating a duplicate. Raises ToolError if the note is missing, not yet
    approved, rejected, or its patient can't be resolved.
    """
    tool = "generate_referral_letter"
    correlation_id = new_correlation_id()
    log_event(_logger, "info", TOOL_STARTED, correlation_id, tool=tool, note_id=note_id)

    try:
        approved_note = referral_letters.completed_encounter(
            _find_note(note_id), note_id
        )
    except MissingEncounterData as e:
        raise _referral_tool_error(
            tool, correlation_id, "MissingEncounterData", str(e)
        ) from e
    patient = _find_patient(approved_note["ehr_system"], approved_note["patient_id"])
    if patient is None:
        raise _referral_tool_error(
            tool,
            correlation_id,
            "MissingEncounterData",
            f"Patient {approved_note['patient_id']!r} for note_id={note_id!r} could not "
            "be resolved; the letter needs the patient's identity.",
        )

    entries = _all_audit_entries()
    existing_id = referral_letters.existing_letter_id(
        entries, note_id, specialty, referral_reason
    )
    if existing_id is not None:
        letter = referral_letters.find_letter(entries, existing_id)
        log_event(
            _logger,
            "info",
            TOOL_COMPLETED,
            correlation_id,
            tool=tool,
            letter_id=existing_id,
            outcome="success",
            no_op=True,
        )
        return letter

    letter_id = str(uuid.uuid4())
    letter_text = referral_letters.render_letter(
        patient, approved_note, specialty, referral_reason, referring_clinician
    )
    entry = referral_letters.generate_entry(
        letter_id,
        datetime.now(timezone.utc).isoformat(),
        approved_note,
        specialty,
        referral_reason,
        referring_clinician,
        letter_text,
    )
    _append_audit_entry(entry)
    log_event(
        _logger,
        "info",
        TOOL_COMPLETED,
        correlation_id,
        tool=tool,
        note_id=note_id,
        letter_id=letter_id,
        outcome="success",
        no_op=False,
    )
    return referral_letters.find_letter([entry], letter_id)


@mcp.tool()
def approve_referral_letter(
    letter_id: Annotated[str, Field(min_length=1)],
    approved_by: ClinicianField,
    edited_letter_text: Annotated[
        str | None, Field(min_length=1, max_length=referral_letters.MAX_LETTER_CHARS)
    ] = None,
) -> ReferralLetter:
    """
    Record a clinician's approval of a reviewed referral letter, marking it
    status="ready_to_send" (STORY-011). Pass `edited_letter_text` if the
    clinician changed the draft during review; otherwise the drafted text is
    approved as-is. The approval (who, when, final text, whether it was
    edited) is recorded in the audit trail.

    Approving again with the same final text is a no-op returning the
    existing approval. Approving an already-approved letter with different
    text raises ToolError -- an approved letter is closed. If the audit write
    fails, ToolError is raised and the letter stays a draft.
    """
    tool = "approve_referral_letter"
    correlation_id = new_correlation_id()
    log_event(
        _logger, "info", TOOL_STARTED, correlation_id, tool=tool, letter_id=letter_id
    )

    letter = referral_letters.find_letter(_all_audit_entries(), letter_id)
    if letter is None:
        log_event(
            _logger,
            "warning",
            TOOL_ERROR,
            correlation_id,
            tool=tool,
            error_class="ResourceNotFoundError",
        )
        raise ResourceNotFoundError(
            f"No referral letter found for letter_id={letter_id!r}."
        )

    if letter.status == "voided":
        raise _voided_letter_error(tool, correlation_id, letter)
    final_text = (
        edited_letter_text if edited_letter_text is not None else letter.letter_text
    )
    if letter.status != "draft":
        if final_text == letter.letter_text:
            log_event(
                _logger,
                "info",
                TOOL_COMPLETED,
                correlation_id,
                tool=tool,
                letter_id=letter_id,
                outcome="success",
                no_op=True,
            )
            return letter
        log_event(
            _logger,
            "warning",
            ACCESS_DENIED,
            correlation_id,
            tool=tool,
            letter_id=letter_id,
            reason="letter_locked_after_approval",
            error_class="AccessDenied",
        )
        raise ToolError(
            f"letter_id={letter_id!r} is already approved ({letter.status}); "
            "it can no longer be changed."
        )

    entry = referral_letters.approve_entry(
        letter, datetime.now(timezone.utc).isoformat(), approved_by, final_text
    )
    _append_audit_entry(entry)
    log_event(
        _logger,
        "info",
        TOOL_COMPLETED,
        correlation_id,
        tool=tool,
        letter_id=letter_id,
        outcome="success",
        no_op=False,
        edited=entry["edited"],
    )
    return referral_letters.find_letter(_all_audit_entries(), letter_id)


@mcp.tool()
def list_referral_letters(
    status: LetterStatus | None = None,
    patient_id: Annotated[str | None, Field(min_length=1)] = None,
) -> ReferralLetterList:
    """
    List referral letters from the audit trail, oldest first, optionally
    filtered by status and/or patient_id. status="ready_to_send" is the
    queue of approved letters that have not been sent yet -- use it to find a
    letter again after the encounter is closed. Read-only: writes no audit
    entry.
    """
    tool = "list_referral_letters"
    correlation_id = new_correlation_id()
    log_event(_logger, "info", TOOL_STARTED, correlation_id, tool=tool, status=status)
    letters = referral_letters.list_letters(_all_audit_entries(), status, patient_id)
    log_event(
        _logger,
        "info",
        TOOL_COMPLETED,
        correlation_id,
        tool=tool,
        outcome="success",
        count=len(letters),
    )
    return ReferralLetterList(
        letters=letters,
        count=len(letters),
        listed_at=datetime.now(timezone.utc).isoformat(),
    )


@mcp.tool()
def mark_referral_letter_sent(
    letter_id: Annotated[str, Field(min_length=1)],
    sent_by: ClinicianField,
    send_method: SendMethod,
) -> ReferralLetter:
    """
    Record that an approved referral letter was delivered, and how
    (handed to the patient, mailed, faxed, ...). This server does not deliver
    the letter itself -- the clinician prints/downloads it and sends it; this
    records that it left, closing the ready_to_send queue entry in the audit
    trail.

    Only a ready_to_send letter can be marked sent: a draft raises ToolError
    (approve it first). Marking an already-sent letter again with the same
    send_method is a no-op returning the existing record; a different
    send_method raises ToolError -- a letter is sent once.
    """
    tool = "mark_referral_letter_sent"
    correlation_id = new_correlation_id()
    log_event(
        _logger, "info", TOOL_STARTED, correlation_id, tool=tool, letter_id=letter_id
    )

    letter = referral_letters.find_letter(_all_audit_entries(), letter_id)
    if letter is None:
        log_event(
            _logger,
            "warning",
            TOOL_ERROR,
            correlation_id,
            tool=tool,
            error_class="ResourceNotFoundError",
        )
        raise ResourceNotFoundError(
            f"No referral letter found for letter_id={letter_id!r}."
        )
    if letter.status == "voided":
        raise _voided_letter_error(tool, correlation_id, letter)
    if letter.status == "draft":
        raise _referral_tool_error(
            tool,
            correlation_id,
            "ValidationError",
            f"letter_id={letter_id!r} is still a draft; a clinician must approve it "
            "before it can be marked sent.",
        )
    if letter.status == "sent":
        if letter.send_method == send_method:
            log_event(
                _logger,
                "info",
                TOOL_COMPLETED,
                correlation_id,
                tool=tool,
                letter_id=letter_id,
                outcome="success",
                no_op=True,
            )
            return letter
        log_event(
            _logger,
            "warning",
            ACCESS_DENIED,
            correlation_id,
            tool=tool,
            letter_id=letter_id,
            reason="letter_already_sent",
            error_class="AccessDenied",
        )
        raise ToolError(
            f"letter_id={letter_id!r} was already marked sent via {letter.send_method!r}; "
            "a letter is sent once."
        )

    entry = referral_letters.send_entry(
        letter, datetime.now(timezone.utc).isoformat(), sent_by, send_method
    )
    _append_audit_entry(entry)
    log_event(
        _logger,
        "info",
        TOOL_COMPLETED,
        correlation_id,
        tool=tool,
        letter_id=letter_id,
        send_method=send_method,
        outcome="success",
        no_op=False,
    )
    return referral_letters.find_letter(_all_audit_entries(), letter_id)


def _voided_letter_error(
    tool: str, correlation_id: str, letter: ReferralLetter
) -> ToolError:
    log_event(
        _logger,
        "warning",
        ACCESS_DENIED,
        correlation_id,
        tool=tool,
        letter_id=letter.letter_id,
        reason="letter_voided",
        error_class="AccessDenied",
    )
    return ToolError(
        f"letter_id={letter.letter_id!r} was voided by {letter.voided_by} and can no "
        "longer be approved or sent. Prepare a new letter if one is still needed."
    )


@mcp.tool()
def void_referral_letter(
    letter_id: Annotated[str, Field(min_length=1)],
    voided_by: ClinicianField,
    void_reason: VoidReasonField,
) -> ReferralLetter:
    """
    Withdraw a referral letter that must not go out -- e.g. it was prepared
    for the wrong patient or contains an error. Allowed for draft and
    ready_to_send letters; a voided letter drops out of the ready-to-send
    queue and can never be approved or marked sent. Who voided it, when, and
    why (void_reason is required) are recorded in the audit trail; nothing
    is deleted, so the letter's full history stays reviewable.

    A sent letter can't be voided (it has already left -- raises ToolError).
    Voiding an already-voided letter is a no-op returning the existing
    record. The same note/specialty/reason can be prepared again afterwards
    with generate_referral_letter, which creates a fresh letter.
    """
    tool = "void_referral_letter"
    correlation_id = new_correlation_id()
    log_event(
        _logger, "info", TOOL_STARTED, correlation_id, tool=tool, letter_id=letter_id
    )

    letter = referral_letters.find_letter(_all_audit_entries(), letter_id)
    if letter is None:
        log_event(
            _logger,
            "warning",
            TOOL_ERROR,
            correlation_id,
            tool=tool,
            error_class="ResourceNotFoundError",
        )
        raise ResourceNotFoundError(
            f"No referral letter found for letter_id={letter_id!r}."
        )
    if letter.status == "voided":
        log_event(
            _logger,
            "info",
            TOOL_COMPLETED,
            correlation_id,
            tool=tool,
            letter_id=letter_id,
            outcome="success",
            no_op=True,
        )
        return letter
    if letter.status == "sent":
        log_event(
            _logger,
            "warning",
            ACCESS_DENIED,
            correlation_id,
            tool=tool,
            letter_id=letter_id,
            reason="letter_already_sent",
            error_class="AccessDenied",
        )
        raise ToolError(
            f"letter_id={letter_id!r} was already sent via {letter.send_method!r} and "
            "can't be voided; contact the recipient directly to correct it."
        )

    entry = referral_letters.void_entry(
        letter, datetime.now(timezone.utc).isoformat(), voided_by, void_reason
    )
    _append_audit_entry(entry)
    log_event(
        _logger,
        "info",
        TOOL_COMPLETED,
        correlation_id,
        tool=tool,
        letter_id=letter_id,
        status_before=letter.status,
        outcome="success",
        no_op=False,
    )
    return referral_letters.find_letter(_all_audit_entries(), letter_id)
