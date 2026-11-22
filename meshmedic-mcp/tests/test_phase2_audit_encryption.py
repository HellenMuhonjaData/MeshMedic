"""Phase 2 acceptance tests (STORY-015): audit-trail encryption that stays append-only and auditable."""

import json

import pytest

import app_core
import audit_store
import integrity_controls
import server

NOTE = "Patient reports crushing chest pressure radiating to the left arm."


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(app_core, "AUDIT_LOG_PATH", tmp_path / "audit_log.jsonl")
    monkeypatch.setenv("MESHMEDIC_ATTESTATION_KEY_DIR", str(tmp_path / "keys"))


def _path():
    return app_core.AUDIT_LOG_PATH


def _raw_lines():
    return _path().read_text(encoding="utf-8").splitlines()


def _write_lines(lines):
    _path().write_text("".join(line + "\n" for line in lines), encoding="utf-8")


def _note():
    return server.generate_encounter_note(ehr_system="epic", patient_id="epic-pt-55219",
                                          transcript="Chest pressure.", note_text=NOTE, confidence=0.9)


def _legacy_then_encrypted(monkeypatch, legacy_count=3, new_count=3):
    monkeypatch.setenv("MESHMEDIC_AUDIT_ENCRYPTION", "off")
    for i in range(legacy_count):
        audit_store.append(_path(), {"action": "legacy_event", "n": i})
    legacy_raw = _raw_lines()
    monkeypatch.setenv("MESHMEDIC_AUDIT_ENCRYPTION", "on")
    for i in range(new_count):
        audit_store.append(_path(), {"action": "new_event", "n": i})
    return legacy_raw


def test_records_written_before_encryption_stay_unchanged_and_readable(monkeypatch):
    legacy_raw = _legacy_then_encrypted(monkeypatch)
    assert _raw_lines()[: len(legacy_raw)] == legacy_raw  # byte-for-byte, never rewritten
    entries = audit_store.read_entries(_path())
    assert [e["action"] for e in entries] == ["legacy_event"] * 3 + ["new_event"] * 3


def test_new_entries_are_encrypted_with_no_clinical_text_in_the_file():
    _note()
    raw = _path().read_text(encoding="utf-8")
    assert "chest pressure" not in raw.lower() and "epic-pt-55219" not in raw
    assert all(json.loads(line)["v"] == 2 for line in _raw_lines())
    assert audit_store.read_entries(_path())[0]["note_text"] == NOTE


def test_altering_an_entry_is_detected(monkeypatch):
    _legacy_then_encrypted(monkeypatch)
    lines = _raw_lines()
    record = json.loads(lines[4])
    record["ct"] = ("A" if record["ct"][0] != "A" else "B") + record["ct"][1:]
    lines[4] = json.dumps(record, separators=(",", ":"))
    _write_lines(lines)
    assert audit_store.chain_problems(_path())  # caught without the key
    with pytest.raises(audit_store.AuditIntegrityError):
        audit_store.read_entries(_path())


def test_altering_the_last_entry_is_caught_by_the_head_and_by_decryption(monkeypatch):
    _legacy_then_encrypted(monkeypatch)
    lines = _raw_lines()
    record = json.loads(lines[-1])
    record["ct"] = record["ct"][::-1]
    lines[-1] = json.dumps(record, separators=(",", ":"))
    _write_lines(lines)
    assert any("head" in p for p in audit_store.chain_problems(_path()))


@pytest.mark.parametrize("change", ["remove_middle", "remove_last", "reorder", "insert_plain", "edit_legacy"])
def test_removing_reordering_inserting_or_editing_old_records_is_detected_without_the_key(monkeypatch, change):
    _legacy_then_encrypted(monkeypatch)
    lines = _raw_lines()
    if change == "remove_middle":
        del lines[4]
    elif change == "remove_last":
        del lines[-1]
    elif change == "reorder":
        lines[4], lines[5] = lines[5], lines[4]
    elif change == "insert_plain":
        lines.insert(5, json.dumps({"action": "approve_note", "note_id": "forged"}))
    elif change == "edit_legacy":
        lines[1] = json.dumps({"action": "legacy_event", "n": 99})
    _write_lines(lines)
    monkeypatch.setenv("MESHMEDIC_ATTESTATION_KEY_DIR", "/nonexistent-key-dir")  # no key needed to detect it
    assert audit_store.chain_problems(_path())


def test_plain_entries_cannot_be_appended_once_encryption_started(monkeypatch):
    _note()
    monkeypatch.setenv("MESHMEDIC_AUDIT_ENCRYPTION", "off")
    with pytest.raises(audit_store.AuditIntegrityError):
        audit_store.append(_path(), {"action": "plain"})


def test_a_missing_key_fails_loudly_instead_of_skipping_entries(monkeypatch, tmp_path):
    _note()
    monkeypatch.setenv("MESHMEDIC_ATTESTATION_KEY_DIR", str(tmp_path / "no-keys-here"))
    with pytest.raises(audit_store.AuditIntegrityError, match="key is missing"):
        audit_store.read_entries(_path())


def test_a_crash_between_line_and_head_write_is_tolerated():
    _note()
    _note()
    head = audit_store.head_path(_path())
    lines = _raw_lines()
    head.write_text(json.dumps({"seq": 1, "hash": audit_store.line_hash(lines[0])}), encoding="utf-8")
    assert audit_store.chain_problems(_path()) == []


def test_compliance_check_verifies_every_entry_and_fails_on_tampering():
    note = _note()
    server.approve_encounter_note(note_id=note.note_id)
    result = server.run_compliance_check()
    integrity = next(c for c in result.controls if c.control_id == "REQ-025-audit-trail-integrity")
    assert result.all_passed and integrity.passed

    lines = _raw_lines()
    del lines[1]
    _write_lines(lines)
    result = server.run_compliance_check()
    integrity = next(c for c in result.controls if c.control_id == "REQ-025-audit-trail-integrity")
    assert not result.all_passed and not integrity.passed
    assert all(not c.passed for c in result.controls if c.control_id != "REQ-011-no-phi-in-structured-logs")


def test_every_reader_returns_the_same_results_encrypted_or_plain(monkeypatch, tmp_path):
    def run_flow():
        note = _note()
        server.approve_encounter_note(note_id=note.note_id)
        server.label_information_sources(note_id=note.note_id)
        summary = server.get_activity_summary(period="day", count=1).totals.model_dump()
        found = server.audit_queries._find_note(note.note_id)
        return summary, found["decision"]["note_text"], found["decision"]["action"]

    monkeypatch.setattr(app_core, "AUDIT_LOG_PATH", tmp_path / "plain.jsonl")
    monkeypatch.setenv("MESHMEDIC_AUDIT_ENCRYPTION", "off")
    plain = run_flow()
    monkeypatch.setattr(app_core, "AUDIT_LOG_PATH", tmp_path / "encrypted.jsonl")
    monkeypatch.setenv("MESHMEDIC_AUDIT_ENCRYPTION", "on")
    encrypted = run_flow()
    assert plain == encrypted


def test_no_clinician_only_action_may_be_done_by_the_ai_or_system():
    clean = [{"action": "sign_note", "actor": {"type": "clinician", "clinician_id": "dr.x"}},
             {"action": "generate_note", "actor": {"type": "ai"}}]
    assert integrity_controls.check_no_autonomous_clinical_actions(clean).passed
    for actor in ("ai", "system"):
        bad = [*clean, {"action": "approve_note", "timestamp": "t", "actor": {"type": actor}}]
        assert not integrity_controls.check_no_autonomous_clinical_actions(bad).passed
