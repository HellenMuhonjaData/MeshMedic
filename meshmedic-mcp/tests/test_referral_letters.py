"""STORY-011 / REQ-004: referral letters prepared for clinician review."""

import json
import os
import stat

import pytest
from mcp.server.mcpserver.exceptions import ResourceNotFoundError, ToolError
from pydantic import TypeAdapter, ValidationError

import referral_letters
import server


@pytest.fixture(autouse=True)
def isolated_audit_log(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "AUDIT_LOG_PATH", tmp_path / "audit_log.jsonl")


@pytest.fixture
def read_only_log():
    """Make the real audit log read-only so _append_audit_entry's open("a")
    raises a genuine OSError (works on Windows and POSIX), restoring it after."""
    from contextlib import contextmanager

    @contextmanager
    def _ctx():
        os.chmod(server.AUDIT_LOG_PATH, stat.S_IREAD)
        try:
            yield
        finally:
            os.chmod(server.AUDIT_LOG_PATH, stat.S_IREAD | stat.S_IWRITE)

    return _ctx


def _entries():
    if not server.AUDIT_LOG_PATH.exists():
        return []
    with server.AUDIT_LOG_PATH.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f]


def _note(approve=True):
    note = server.generate_encounter_note(
        ehr_system="epic",
        patient_id="epic-pt-10293847",
        transcript="Intermittent chest tightness on exertion for three weeks.",
        note_text="Grace Whitfield reports exertional chest tightness for three weeks.",
        confidence=0.9,
    )
    if approve:
        server.approve_encounter_note(note_id=note.note_id)
    return note


def _letter(
    note_id, specialty="Cardiology", reason="Evaluate exertional chest tightness."
):
    return server.generate_referral_letter(
        note_id=note_id,
        specialty=specialty,
        referral_reason=reason,
        referring_clinician="Dr. A. Rivera",
    )


# --- Criterion 1: generate from a completed encounter, for review -----------


def test_generate_builds_draft_from_approved_encounter():
    note = _note()

    letter = _letter(note.note_id)

    assert letter.status == "draft"
    assert letter.note_id == note.note_id
    assert letter.patient_id == "epic-pt-10293847"
    assert letter.approved_at is None and letter.approved_by is None
    text = letter.letter_text
    assert "Re: Referral to Cardiology" in text
    assert "Grace Whitfield" in text and "E10293847" in text and "1968-03-14" in text
    assert "Evaluate exertional chest tightness." in text
    assert note.note_text in text
    assert text.rstrip().endswith("Dr. A. Rivera")


def test_generate_uses_the_clinician_edited_note_text_not_the_ai_draft():
    note = _note(approve=False)
    server.approve_encounter_note(
        note_id=note.note_id, edited_note_text="Clinician-corrected summary."
    )

    letter = _letter(note.note_id)

    assert "Clinician-corrected summary." in letter.letter_text
    assert note.note_text not in letter.letter_text


def test_generate_unknown_note_fails_and_writes_nothing():
    with pytest.raises(ToolError, match="No encounter note"):
        _letter("does-not-exist")
    assert _entries() == []


def test_generate_refuses_an_unapproved_draft_note():
    note = _note(approve=False)
    before = len(_entries())

    with pytest.raises(ToolError, match="has not been approved"):
        _letter(note.note_id)
    assert len(_entries()) == before


def test_generate_refuses_a_rejected_note():
    note = _note(approve=False)
    server.reject_encounter_note(
        note_id=note.note_id, feedback="Wrong patient history."
    )

    with pytest.raises(ToolError, match="rejected"):
        _letter(note.note_id)


def test_generate_fails_when_patient_cannot_be_resolved(monkeypatch):
    note = _note()
    monkeypatch.setattr(server, "_find_patient", lambda ehr_system, patient_id: None)

    with pytest.raises(ToolError, match="could not be resolved"):
        _letter(note.note_id)
    assert not any(e["action"] == "generate_referral_letter" for e in _entries())


def test_generate_is_idempotent_for_the_same_request():
    note = _note()

    first = _letter(note.note_id)
    second = _letter(note.note_id, specialty="  cardiology ")

    assert second.letter_id == first.letter_id
    generated = [e for e in _entries() if e["action"] == "generate_referral_letter"]
    assert len(generated) == 1


def test_generate_a_different_specialty_is_a_separate_letter():
    note = _note()

    cardio = _letter(note.note_id)
    pulm = _letter(note.note_id, specialty="Pulmonology")

    assert cardio.letter_id != pulm.letter_id


@pytest.mark.parametrize(
    "field_type, value",
    [
        (referral_letters.SpecialtyField, ""),
        (referral_letters.SpecialtyField, "x" * 101),
        (referral_letters.ReasonField, ""),
        (referral_letters.ReasonField, "x" * 1001),
        (referral_letters.ClinicianField, ""),
    ],
)
def test_input_contracts_reject_empty_and_oversized_values(field_type, value):
    with pytest.raises(ValidationError):
        TypeAdapter(field_type).validate_python(value)


def test_max_length_reason_is_accepted():
    letter = _letter(_note().note_id, reason="x" * 1000)
    assert "x" * 1000 in letter.letter_text


def test_render_letter_is_deterministic():
    patient = server._find_patient("epic", "epic-pt-10293847")
    approved = {"timestamp": "2026-10-01T09:00:00+00:00", "note_text": "Summary."}
    args = (patient, approved, "Cardiology", "Reason.", "Dr. X")

    assert referral_letters.render_letter(*args) == referral_letters.render_letter(
        *args
    )
    assert referral_letters.render_letter(*args).startswith("Date: 2026-10-01\n")


def test_generation_audit_failure_raises_and_creates_no_letter(read_only_log):
    note = _note()

    with read_only_log(), pytest.raises(ToolError, match="audit trail"):
        _letter(note.note_id)

    assert not any(e["action"] == "generate_referral_letter" for e in _entries())


# --- Criterion 2: approval marks it ready to send ---------------------------


def test_approve_marks_letter_ready_to_send():
    letter = _letter(_note().note_id)

    approved = server.approve_referral_letter(
        letter_id=letter.letter_id, approved_by="Dr. A. Rivera"
    )

    assert approved.status == "ready_to_send"
    assert approved.approved_by == "Dr. A. Rivera"
    assert approved.approved_at is not None
    assert approved.letter_text == letter.letter_text


def test_approve_with_clinician_edits_keeps_the_edited_text():
    letter = _letter(_note().note_id)

    approved = server.approve_referral_letter(
        letter_id=letter.letter_id,
        approved_by="Dr. A. Rivera",
        edited_letter_text="Edited letter body.",
    )

    assert approved.letter_text == "Edited letter body."
    assert _entries()[-1]["edited"] is True
    # Re-generating the same request now returns the approved letter.
    assert _letter(letter.note_id).status == "ready_to_send"


def test_approve_is_idempotent_on_replay():
    letter = _letter(_note().note_id)
    first = server.approve_referral_letter(
        letter_id=letter.letter_id, approved_by="Dr. R"
    )

    second = server.approve_referral_letter(
        letter_id=letter.letter_id, approved_by="Dr. R"
    )

    assert second == first
    approvals = [e for e in _entries() if e["action"] == "approve_referral_letter"]
    assert len(approvals) == 1


def test_approve_with_different_text_after_approval_is_refused():
    letter = _letter(_note().note_id)
    server.approve_referral_letter(letter_id=letter.letter_id, approved_by="Dr. R")

    with pytest.raises(ToolError, match="already approved"):
        server.approve_referral_letter(
            letter_id=letter.letter_id,
            approved_by="Dr. R",
            edited_letter_text="Changed.",
        )


def test_approve_unknown_letter_raises_not_found():
    with pytest.raises(ResourceNotFoundError):
        server.approve_referral_letter(letter_id="nope", approved_by="Dr. R")


def test_approval_save_failure_leaves_letter_a_draft(read_only_log):
    letter = _letter(_note().note_id)

    with read_only_log(), pytest.raises(ToolError, match="audit trail"):
        server.approve_referral_letter(letter_id=letter.letter_id, approved_by="Dr. R")

    assert referral_letters.find_letter(_entries(), letter.letter_id).status == "draft"
    # Retrying once the log is writable again succeeds.
    retried = server.approve_referral_letter(
        letter_id=letter.letter_id, approved_by="Dr. R"
    )
    assert retried.status == "ready_to_send"


# --- Criterion 3 (trust): creation and approval are audited -----------------


def test_creation_and_approval_are_both_in_the_audit_trail():
    letter = _letter(_note().note_id)
    server.approve_referral_letter(
        letter_id=letter.letter_id, approved_by="Dr. A. Rivera"
    )

    created, approved = [
        e for e in _entries() if e.get("letter_id") == letter.letter_id
    ]

    assert created["action"] == "generate_referral_letter"
    assert created["status"] == "draft"
    assert created["referring_clinician"] == "Dr. A. Rivera"
    assert created["letter_text"] == letter.letter_text
    assert approved["action"] == "approve_referral_letter"
    assert approved["status"] == "ready_to_send"
    assert approved["approved_by"] == "Dr. A. Rivera"
    assert created["timestamp"] <= approved["timestamp"]


def test_compliance_check_passes_with_referral_letters():
    letter = _letter(_note().note_id)
    server.approve_referral_letter(letter_id=letter.letter_id, approved_by="Dr. R")

    assert server.run_compliance_check().all_passed is True


def test_compliance_check_catches_an_orphaned_letter_approval():
    _raw({"timestamp": "t", "action": "approve_referral_letter", "letter_id": "ghost"})

    result = server.run_compliance_check()

    completeness = next(
        c for c in result.controls if c.control_id == "REQ-006-audit-completeness"
    )
    assert completeness.passed is False
    assert "ghost" in completeness.detail


def test_compliance_check_catches_a_double_approved_letter():
    letter = _letter(_note().note_id)
    server.approve_referral_letter(letter_id=letter.letter_id, approved_by="Dr. R")
    _raw(
        {
            "timestamp": "t",
            "action": "approve_referral_letter",
            "letter_id": letter.letter_id,
        }
    )

    result = server.run_compliance_check()

    integrity = next(
        c
        for c in result.controls
        if c.control_id == "REQ-014-single-decision-integrity"
    )
    assert integrity.passed is False


def _raw(entry):
    with server.AUDIT_LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


# --- Ready-to-send queue and recording delivery ------------------------------


def _approved_letter(specialty="Cardiology"):
    letter = _letter(_note().note_id, specialty=specialty)
    return server.approve_referral_letter(
        letter_id=letter.letter_id, approved_by="Dr. R"
    )


def test_list_ready_to_send_returns_only_approved_unsent_letters():
    note = _note()
    draft = _letter(note.note_id, specialty="Pulmonology")
    ready = _approved_letter()
    sent = _approved_letter(specialty="Neurology")
    server.mark_referral_letter_sent(
        letter_id=sent.letter_id, sent_by="Dr. R", send_method="fax"
    )

    queue = server.list_referral_letters(status="ready_to_send")

    assert [lt.letter_id for lt in queue.letters] == [ready.letter_id]
    assert queue.count == 1
    assert {lt.letter_id for lt in server.list_referral_letters().letters} == {
        draft.letter_id,
        ready.letter_id,
        sent.letter_id,
    }


def test_list_filters_by_patient_and_is_empty_on_empty_log():
    assert server.list_referral_letters().count == 0
    _approved_letter()

    assert server.list_referral_letters(patient_id="epic-pt-10293847").count == 1
    assert server.list_referral_letters(patient_id="oh-pt-88213").count == 0


def test_list_writes_no_audit_entry():
    _approved_letter()
    before = len(_entries())

    server.list_referral_letters(status="ready_to_send")

    assert len(_entries()) == before


def test_mark_sent_records_delivery_and_leaves_the_queue():
    letter = _approved_letter()

    sent = server.mark_referral_letter_sent(
        letter_id=letter.letter_id, sent_by="Dr. R", send_method="handed_to_patient"
    )

    assert sent.status == "sent"
    assert sent.sent_by == "Dr. R"
    assert sent.send_method == "handed_to_patient"
    assert sent.sent_at is not None
    assert sent.letter_text == letter.letter_text
    assert server.list_referral_letters(status="ready_to_send").count == 0
    entry = _entries()[-1]
    assert entry["action"] == "mark_referral_letter_sent"
    assert entry["send_method"] == "handed_to_patient"


def test_mark_sent_refuses_a_draft():
    draft = _letter(_note().note_id)

    with pytest.raises(ToolError, match="still a draft"):
        server.mark_referral_letter_sent(
            letter_id=draft.letter_id, sent_by="Dr. R", send_method="mail"
        )
    assert not any(e["action"] == "mark_referral_letter_sent" for e in _entries())


def test_mark_sent_unknown_letter_raises_not_found():
    with pytest.raises(ResourceNotFoundError):
        server.mark_referral_letter_sent(
            letter_id="nope", sent_by="Dr. R", send_method="mail"
        )


def test_mark_sent_is_idempotent_and_refuses_a_conflicting_method():
    letter = _approved_letter()
    first = server.mark_referral_letter_sent(
        letter_id=letter.letter_id, sent_by="Dr. R", send_method="mail"
    )

    again = server.mark_referral_letter_sent(
        letter_id=letter.letter_id, sent_by="Dr. R", send_method="mail"
    )
    assert again == first
    with pytest.raises(ToolError, match="already marked sent"):
        server.mark_referral_letter_sent(
            letter_id=letter.letter_id, sent_by="Dr. R", send_method="fax"
        )
    sends = [e for e in _entries() if e["action"] == "mark_referral_letter_sent"]
    assert len(sends) == 1


def test_mark_sent_save_failure_leaves_letter_ready_to_send(read_only_log):
    letter = _approved_letter()

    with read_only_log(), pytest.raises(ToolError, match="audit trail"):
        server.mark_referral_letter_sent(
            letter_id=letter.letter_id, sent_by="Dr. R", send_method="mail"
        )

    assert server.list_referral_letters(status="ready_to_send").count == 1


def test_a_sent_letter_cannot_be_re_approved_with_new_text():
    letter = _approved_letter()
    server.mark_referral_letter_sent(
        letter_id=letter.letter_id, sent_by="Dr. R", send_method="mail"
    )

    with pytest.raises(ToolError, match="already approved"):
        server.approve_referral_letter(
            letter_id=letter.letter_id,
            approved_by="Dr. R",
            edited_letter_text="Changed.",
        )
    approvals = [e for e in _entries() if e["action"] == "approve_referral_letter"]
    assert len(approvals) == 1


def test_send_method_contract_rejects_unknown_methods():
    with pytest.raises(ValidationError):
        TypeAdapter(referral_letters.SendMethod).validate_python("carrier_pigeon")


def test_compliance_passes_after_send_and_catches_send_without_approval():
    letter = _approved_letter()
    server.mark_referral_letter_sent(
        letter_id=letter.letter_id, sent_by="Dr. R", send_method="mail"
    )
    assert server.run_compliance_check().all_passed is True

    draft = _letter(_note().note_id, specialty="Dermatology")
    _raw(
        {
            "timestamp": "t",
            "action": "mark_referral_letter_sent",
            "letter_id": draft.letter_id,
        }
    )
    completeness = next(
        c
        for c in server.run_compliance_check().controls
        if c.control_id == "REQ-006-audit-completeness"
    )
    assert completeness.passed is False
    assert draft.letter_id in completeness.detail


def test_compliance_catches_a_letter_marked_sent_twice():
    letter = _approved_letter()
    server.mark_referral_letter_sent(
        letter_id=letter.letter_id, sent_by="Dr. R", send_method="mail"
    )
    _raw(
        {
            "timestamp": "t",
            "action": "mark_referral_letter_sent",
            "letter_id": letter.letter_id,
        }
    )

    integrity = next(
        c
        for c in server.run_compliance_check().controls
        if c.control_id == "REQ-014-single-decision-integrity"
    )
    assert integrity.passed is False
    assert "marked sent 2 times" in integrity.detail


# --- Voiding a letter that must not go out -----------------------------------


def _void(letter_id, reason="Wrong patient."):
    return server.void_referral_letter(
        letter_id=letter_id, voided_by="Dr. R", void_reason=reason
    )


def test_void_a_ready_to_send_letter_removes_it_from_the_queue():
    letter = _approved_letter()

    voided = _void(letter.letter_id)

    assert voided.status == "voided"
    assert voided.voided_by == "Dr. R"
    assert voided.void_reason == "Wrong patient."
    assert voided.voided_at is not None
    assert server.list_referral_letters(status="ready_to_send").count == 0
    assert server.list_referral_letters(status="voided").count == 1
    entry = _entries()[-1]
    assert entry["action"] == "void_referral_letter"
    assert entry["status_before"] == "ready_to_send"


def test_void_a_draft():
    draft = _letter(_note().note_id)

    assert _void(draft.letter_id).status == "voided"
    assert _entries()[-1]["status_before"] == "draft"


def test_a_voided_letter_cannot_be_approved_or_sent():
    draft = _letter(_note().note_id)
    ready = _approved_letter(specialty="Neurology")
    _void(draft.letter_id)
    _void(ready.letter_id)

    with pytest.raises(ToolError, match="voided"):
        server.approve_referral_letter(letter_id=draft.letter_id, approved_by="Dr. R")
    with pytest.raises(ToolError, match="voided"):
        server.mark_referral_letter_sent(
            letter_id=ready.letter_id, sent_by="Dr. R", send_method="mail"
        )
    assert not any(e["action"] == "mark_referral_letter_sent" for e in _entries())


def test_a_sent_letter_cannot_be_voided():
    letter = _approved_letter()
    server.mark_referral_letter_sent(
        letter_id=letter.letter_id, sent_by="Dr. R", send_method="fax"
    )

    with pytest.raises(ToolError, match="already sent"):
        _void(letter.letter_id)
    assert not any(e["action"] == "void_referral_letter" for e in _entries())


def test_void_is_idempotent():
    letter = _approved_letter()
    first = _void(letter.letter_id)

    again = _void(letter.letter_id, reason="Different wording on retry.")

    assert again == first
    voids = [e for e in _entries() if e["action"] == "void_referral_letter"]
    assert len(voids) == 1


def test_void_unknown_letter_raises_not_found():
    with pytest.raises(ResourceNotFoundError):
        _void("nope")


def test_void_save_failure_leaves_letter_in_the_queue(read_only_log):
    letter = _approved_letter()

    with read_only_log(), pytest.raises(ToolError, match="audit trail"):
        _void(letter.letter_id)

    assert server.list_referral_letters(status="ready_to_send").count == 1


def test_same_request_can_be_prepared_again_after_a_void():
    note = _note()
    first = _letter(note.note_id)
    _void(first.letter_id, reason="Typo in reason.")

    fresh = _letter(note.note_id)

    assert fresh.letter_id != first.letter_id
    assert fresh.status == "draft"
    # And the fresh one is idempotent in turn.
    assert _letter(note.note_id).letter_id == fresh.letter_id


def test_void_reason_contract_rejects_empty_and_oversized():
    adapter = TypeAdapter(referral_letters.VoidReasonField)
    for bad in ("", "x" * 501):
        with pytest.raises(ValidationError):
            adapter.validate_python(bad)


def test_compliance_passes_after_void_and_catches_void_violations():
    letter = _approved_letter()
    _void(letter.letter_id)
    assert server.run_compliance_check().all_passed is True

    _raw(
        {
            "timestamp": "t",
            "action": "mark_referral_letter_sent",
            "letter_id": letter.letter_id,
        }
    )
    _raw({"timestamp": "t", "action": "void_referral_letter", "letter_id": "ghost"})
    controls = {c.control_id: c for c in server.run_compliance_check().controls}

    integrity = controls["REQ-014-single-decision-integrity"]
    assert integrity.passed is False
    assert "both voided and marked sent" in integrity.detail
    completeness = controls["REQ-006-audit-completeness"]
    assert completeness.passed is False
    assert "ghost" in completeness.detail
    assert "missing voided_by or void_reason" in completeness.detail
