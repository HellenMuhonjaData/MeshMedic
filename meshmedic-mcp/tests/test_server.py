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
    )

    assert note.status == "draft"
    assert note.patient_id == "epic-pt-10293847"
    assert note.note_text.startswith("Grace Whitfield")
    assert note.note_id

    entries = _read_audit_entries()
    assert len(entries) == 1
    assert entries[0]["action"] == "generate_note"
    assert entries[0]["note_id"] == note.note_id
    assert entries[0]["status"] == "draft"
    assert entries[0]["patient_id"] == "epic-pt-10293847"


def test_generate_encounter_note_unknown_patient_raises_and_does_not_audit():
    with pytest.raises(ResourceNotFoundError):
        server.generate_encounter_note(
            ehr_system="epic",
            patient_id="epic-pt-does-not-exist",
            transcript="Patient reports mild headache.",
            note_text="Some draft text.",
        )

    assert _read_audit_entries() == []


def _generate_note():
    return server.generate_encounter_note(
        ehr_system="epic",
        patient_id="epic-pt-10293847",
        transcript="Patient reports mild headache for two days, no fever.",
        note_text="Grace Whitfield presents with a two-day history of mild headache, afebrile.",
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
