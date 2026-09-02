import json

import pytest
from mcp.server.mcpserver.exceptions import ResourceNotFoundError, ToolError

import server


@pytest.fixture(autouse=True)
def isolated_audit_log(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "AUDIT_LOG_PATH", tmp_path / "audit_log.jsonl")


def _read_audit_entries():
    if not server.AUDIT_LOG_PATH.exists():
        return []
    with server.AUDIT_LOG_PATH.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f]


def test_generate_encounter_note_happy_path():
    note = server.generate_encounter_note(
        ehr_system="epic",
        patient_id="epic-pt-10293847",
        transcript="Patient reports mild headache for two days, no fever.",
        note_text="Grace Whitfield presents with a two-day history of mild headache, afebrile.",
        confidence=0.95,
    )

    assert note.status == "draft"
    assert note.patient_id == "epic-pt-10293847"
    assert note.note_text.startswith("Grace Whitfield")
    assert note.note_id
    assert note.flagged is False
    assert note.warning is None

    entries = _read_audit_entries()
    assert len(entries) == 1
    assert entries[0]["action"] == "generate_note"
    assert entries[0]["note_id"] == note.note_id
    assert entries[0]["status"] == "draft"
    assert entries[0]["patient_id"] == "epic-pt-10293847"
    assert entries[0]["flagged"] is False


def test_generate_encounter_note_unknown_patient_raises_and_does_not_audit():
    with pytest.raises(ResourceNotFoundError):
        server.generate_encounter_note(
            ehr_system="epic",
            patient_id="epic-pt-does-not-exist",
            transcript="Patient reports mild headache.",
            note_text="Some draft text.",
            confidence=0.95,
        )

    assert _read_audit_entries() == []


def test_generate_encounter_note_flags_low_confidence_with_reason():
    note = server.generate_encounter_note(
        ehr_system="epic",
        patient_id="epic-pt-10293847",
        transcript="Patient mentioned some head discomfort, details unclear.",
        note_text="Grace Whitfield reports head discomfort; onset and severity unclear from transcript.",
        confidence=0.4,
        confidence_reason="Transcript doesn't specify onset, duration, or severity.",
    )

    assert note.flagged is True
    assert note.warning is not None
    assert "0.40" in note.warning
    assert "onset" in note.warning

    entries = _read_audit_entries()
    assert entries[0]["flagged"] is True
    assert entries[0]["confidence"] == 0.4
    assert entries[0]["confidence_reason"] == "Transcript doesn't specify onset, duration, or severity."


def test_generate_encounter_note_at_threshold_is_not_flagged():
    note = server.generate_encounter_note(
        ehr_system="epic",
        patient_id="epic-pt-10293847",
        transcript="Patient reports mild headache for two days, no fever.",
        note_text="Grace Whitfield presents with a two-day history of mild headache, afebrile.",
        confidence=server.CONFIDENCE_THRESHOLD,
    )

    assert note.flagged is False
    assert note.warning is None


def test_generate_encounter_note_low_confidence_without_reason_raises_and_does_not_audit():
    with pytest.raises(ToolError):
        server.generate_encounter_note(
            ehr_system="epic",
            patient_id="epic-pt-10293847",
            transcript="Patient mentioned some head discomfort, details unclear.",
            note_text="Some draft text.",
            confidence=0.4,
        )

    assert _read_audit_entries() == []


def _generate_note():
    return server.generate_encounter_note(
        ehr_system="epic",
        patient_id="epic-pt-10293847",
        transcript="Patient reports mild headache for two days, no fever.",
        note_text="Grace Whitfield presents with a two-day history of mild headache, afebrile.",
        confidence=0.95,
    )


def test_approve_encounter_note_happy_path():
    note = _generate_note()

    reviewed = server.approve_encounter_note(note_id=note.note_id)

    assert reviewed.status == "approved"
    assert reviewed.note_text == note.note_text
    assert reviewed.feedback is None

    entries = _read_audit_entries()
    assert len(entries) == 2
    assert entries[0]["action"] == "generate_note"
    assert entries[1]["action"] == "approve_note"
    assert entries[1]["note_id"] == note.note_id
    assert entries[1]["status"] == "approved"


def test_approve_encounter_note_with_edit_records_final_text():
    note = _generate_note()

    reviewed = server.approve_encounter_note(
        note_id=note.note_id,
        edited_note_text="Grace Whitfield: two-day mild headache, afebrile. No red flags.",
    )

    assert reviewed.note_text == "Grace Whitfield: two-day mild headache, afebrile. No red flags."
    entries = _read_audit_entries()
    assert entries[1]["note_text"] == reviewed.note_text
    # The AI's original draft is still visible in its own entry.
    assert entries[0]["note_text"] == note.note_text


def test_reject_encounter_note_happy_path_requires_and_logs_feedback():
    note = _generate_note()

    reviewed = server.reject_encounter_note(
        note_id=note.note_id,
        feedback="Missed the patient's reported fever on day two.",
    )

    assert reviewed.status == "rejected"
    assert reviewed.feedback == "Missed the patient's reported fever on day two."

    entries = _read_audit_entries()
    assert len(entries) == 2
    assert entries[1]["action"] == "reject_note"
    assert entries[1]["feedback"] == "Missed the patient's reported fever on day two."


def test_approve_encounter_note_unknown_note_id_raises_and_does_not_audit():
    with pytest.raises(ResourceNotFoundError):
        server.approve_encounter_note(note_id="does-not-exist")

    assert _read_audit_entries() == []


def test_approve_then_reject_same_note_raises_tool_error():
    note = _generate_note()
    server.approve_encounter_note(note_id=note.note_id)

    with pytest.raises(ToolError):
        server.reject_encounter_note(note_id=note.note_id, feedback="Actually, reject this.")

    # No third audit entry was written for the rejected attempt.
    assert len(_read_audit_entries()) == 2


def test_approve_encounter_note_is_idempotent_for_identical_replay():
    note = _generate_note()
    first = server.approve_encounter_note(note_id=note.note_id)
    second = server.approve_encounter_note(note_id=note.note_id)

    assert first == second
    # Replaying the identical approval did not write a second audit entry.
    assert len(_read_audit_entries()) == 2


def test_edit_encounter_note_happy_path():
    note = _generate_note()

    edited = server.edit_encounter_note(
        note_id=note.note_id,
        edited_note_text="Grace Whitfield: two-day mild headache, afebrile. No red flags.",
    )

    assert edited.status == "draft"
    assert edited.note_text == "Grace Whitfield: two-day mild headache, afebrile. No red flags."

    entries = _read_audit_entries()
    assert len(entries) == 2
    assert entries[0]["action"] == "generate_note"
    assert entries[1]["action"] == "edit_note"
    assert entries[1]["note_id"] == note.note_id
    assert entries[1]["status"] == "draft"


def test_approve_encounter_note_uses_latest_saved_edit_by_default():
    note = _generate_note()
    server.edit_encounter_note(
        note_id=note.note_id,
        edited_note_text="Grace Whitfield: two-day mild headache, afebrile. No red flags.",
    )

    reviewed = server.approve_encounter_note(note_id=note.note_id)

    assert reviewed.note_text == "Grace Whitfield: two-day mild headache, afebrile. No red flags."


def test_reject_encounter_note_uses_latest_saved_edit():
    note = _generate_note()
    server.edit_encounter_note(
        note_id=note.note_id,
        edited_note_text="Grace Whitfield: two-day mild headache, afebrile. No red flags.",
    )

    reviewed = server.reject_encounter_note(note_id=note.note_id, feedback="Still not detailed enough.")

    assert reviewed.note_text == "Grace Whitfield: two-day mild headache, afebrile. No red flags."


def test_edit_encounter_note_unknown_note_id_raises_and_does_not_audit():
    with pytest.raises(ResourceNotFoundError):
        server.edit_encounter_note(note_id="does-not-exist", edited_note_text="Some edit.")

    assert _read_audit_entries() == []


def test_edit_encounter_note_after_decision_raises_tool_error():
    note = _generate_note()
    server.approve_encounter_note(note_id=note.note_id)

    with pytest.raises(ToolError):
        server.edit_encounter_note(note_id=note.note_id, edited_note_text="Too late now.")

    # No third audit entry was written for the rejected edit attempt.
    assert len(_read_audit_entries()) == 2


def test_edit_encounter_note_identical_edit_is_idempotent():
    note = _generate_note()
    first = server.edit_encounter_note(
        note_id=note.note_id,
        edited_note_text="Grace Whitfield: two-day mild headache, afebrile. No red flags.",
    )
    second = server.edit_encounter_note(
        note_id=note.note_id,
        edited_note_text="Grace Whitfield: two-day mild headache, afebrile. No red flags.",
    )

    assert first == second
    # Replaying the identical edit did not write a second audit entry.
    assert len(_read_audit_entries()) == 2


def test_flagged_note_manual_correction_is_saved_and_logged():
    note = server.generate_encounter_note(
        ehr_system="epic",
        patient_id="epic-pt-10293847",
        transcript="Patient mentioned some head discomfort, details unclear.",
        note_text="Grace Whitfield reports head discomfort; onset and severity unclear from transcript.",
        confidence=0.4,
        confidence_reason="Transcript doesn't specify onset, duration, or severity.",
    )
    assert note.flagged is True

    corrected = server.edit_encounter_note(
        note_id=note.note_id,
        edited_note_text="Grace Whitfield: two-day mild headache per follow-up call, afebrile.",
    )

    assert corrected.note_text == "Grace Whitfield: two-day mild headache per follow-up call, afebrile."

    entries = _read_audit_entries()
    assert len(entries) == 2
    assert entries[0]["action"] == "generate_note" and entries[0]["flagged"] is True
    assert entries[1]["action"] == "edit_note"
    assert entries[1]["note_text"] == corrected.note_text


def _suggest_codes(note_id):
    return server.suggest_codes(
        note_id=note_id,
        codes=[
            server.CodeSuggestion(code="R51.9", code_system="icd-10", description="Headache, unspecified"),
            server.CodeSuggestion(
                code="99213", code_system="cpt", description="Established patient office visit, low complexity"
            ),
        ],
    )


def test_suggest_codes_happy_path():
    note = _generate_note()

    suggestion = _suggest_codes(note.note_id)

    assert suggestion.status == "draft"
    assert suggestion.note_id == note.note_id
    assert len(suggestion.codes) == 2
    assert suggestion.codes[0].code == "R51.9"

    entries = _read_audit_entries()
    assert len(entries) == 2
    assert entries[1]["action"] == "suggest_codes"
    assert entries[1]["suggestion_id"] == suggestion.suggestion_id
    assert entries[1]["note_id"] == note.note_id
    assert entries[1]["status"] == "draft"


def test_suggest_codes_unknown_note_id_raises_and_does_not_audit():
    with pytest.raises(ResourceNotFoundError):
        _suggest_codes("does-not-exist")

    assert _read_audit_entries() == []


def test_approve_codes_happy_path():
    note = _generate_note()
    suggestion = _suggest_codes(note.note_id)

    reviewed = server.approve_codes(suggestion_id=suggestion.suggestion_id)

    assert reviewed.status == "approved"
    assert reviewed.codes == suggestion.codes
    assert reviewed.feedback is None

    entries = _read_audit_entries()
    assert len(entries) == 3
    assert entries[2]["action"] == "approve_codes"
    assert entries[2]["suggestion_id"] == suggestion.suggestion_id
    assert entries[2]["status"] == "approved"


def test_approve_codes_with_edit_records_final_codes():
    note = _generate_note()
    suggestion = _suggest_codes(note.note_id)

    reviewed = server.approve_codes(
        suggestion_id=suggestion.suggestion_id,
        edited_codes=[server.CodeSuggestion(code="R51.9", code_system="icd-10", description="Headache, unspecified")],
    )

    assert len(reviewed.codes) == 1
    entries = _read_audit_entries()
    assert len(entries[2]["codes"]) == 1
    # The AI's original two-code suggestion is still visible in its own entry.
    assert len(entries[1]["codes"]) == 2


def test_reject_codes_happy_path_requires_and_logs_feedback():
    note = _generate_note()
    suggestion = _suggest_codes(note.note_id)

    reviewed = server.reject_codes(
        suggestion_id=suggestion.suggestion_id,
        feedback="99213 is too low a complexity level for this visit; should be 99214.",
    )

    assert reviewed.status == "rejected"
    assert reviewed.feedback == "99213 is too low a complexity level for this visit; should be 99214."

    entries = _read_audit_entries()
    assert entries[2]["action"] == "reject_codes"
    assert entries[2]["feedback"] == "99213 is too low a complexity level for this visit; should be 99214."


def test_approve_codes_unknown_suggestion_id_raises_and_does_not_audit():
    with pytest.raises(ResourceNotFoundError):
        server.approve_codes(suggestion_id="does-not-exist")

    assert _read_audit_entries() == []


def test_approve_then_reject_same_suggestion_raises_tool_error():
    note = _generate_note()
    suggestion = _suggest_codes(note.note_id)
    server.approve_codes(suggestion_id=suggestion.suggestion_id)

    with pytest.raises(ToolError):
        server.reject_codes(suggestion_id=suggestion.suggestion_id, feedback="Actually, reject this.")

    # No fourth audit entry was written for the rejected attempt.
    assert len(_read_audit_entries()) == 3


def test_approve_codes_is_idempotent_for_identical_replay():
    note = _generate_note()
    suggestion = _suggest_codes(note.note_id)
    first = server.approve_codes(suggestion_id=suggestion.suggestion_id)
    second = server.approve_codes(suggestion_id=suggestion.suggestion_id)

    assert first == second
    # Replaying the identical approval did not write a second audit entry.
    assert len(_read_audit_entries()) == 3
