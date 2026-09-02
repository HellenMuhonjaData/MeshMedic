import json

import pytest
from mcp.server.mcpserver.exceptions import ResourceNotFoundError

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
