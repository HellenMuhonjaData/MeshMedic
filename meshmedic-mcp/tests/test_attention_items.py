"""Dashboard drill-down: the open items behind each "waiting on you now" count."""

import json
import os
import stat
import typing
from datetime import datetime, timedelta, timezone

import pytest
from mcp.server.mcpserver.exceptions import ToolError

import attention_items
import server

GRACE = ("epic", "epic-pt-10293847")


@pytest.fixture(autouse=True)
def isolated_audit_log(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "AUDIT_LOG_PATH", tmp_path / "audit_log.jsonl")


def _entries():
    if not server.AUDIT_LOG_PATH.exists():
        return []
    with server.AUDIT_LOG_PATH.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f]


def _note(confidence=0.9, reason=None):
    return server.generate_encounter_note(
        ehr_system=GRACE[0],
        patient_id=GRACE[1],
        transcript="Chest tightness on exertion for three weeks.",
        note_text="Exertional chest tightness for three weeks.",
        confidence=confidence,
        confidence_reason=reason,
    )


def _gap(note_id, description="Overdue A1c"):
    return server.identify_care_gaps(
        note_id=note_id,
        gaps=[server.CareGapInput(description=description, confidence=0.9)],
    )[0]


# --- Each category lists exactly the open items ---------------------------------


def test_notes_awaiting_review_lists_undecided_notes_with_patient():
    open_note = _note(confidence=0.5, reason="Short transcript.")
    decided = _note()
    server.approve_encounter_note(note_id=decided.note_id)
    listing = server.get_attention_items(category="notes_awaiting_review")
    assert [i.item_id for i in listing.items] == [open_note.note_id]
    item = listing.items[0]
    assert item.kind == "note"
    assert item.patient_name == "Grace Whitfield"
    assert item.mrn == "E10293847"
    assert item.low_confidence is True
    assert "low confidence (0.50)" in item.summary
    assert "approve or reject" in item.needs


def test_open_care_gaps_excludes_addressed_ones_and_names_the_gap():
    note = _note()
    open_gap = _gap(note.note_id, "Overdue A1c")
    done = _gap(note.note_id, "Flu shot")
    server.address_care_gap(gap_id=done.gap_id, resolution="Given today.")
    listing = server.get_attention_items(category="open_care_gaps")
    assert [i.item_id for i in listing.items] == [open_gap.gap_id]
    assert listing.items[0].summary == "Care gap: Overdue A1c"
    assert listing.items[0].note_id == note.note_id


def test_tests_awaiting_results_excludes_resulted_orders():
    note = _note()
    pending = server.order_test(note_id=note.note_id, test_name="A1c", reason="Annual")
    resulted = server.order_test(
        note_id=note.note_id, test_name="Lipids", reason="Annual"
    )
    server.record_test_result(order_id=resulted.order_id, result_text="Normal")
    listing = server.get_attention_items(category="tests_awaiting_results")
    assert [i.item_id for i in listing.items] == [pending.order_id]
    assert listing.items[0].summary == "Test ordered: A1c"


def test_letter_categories_split_drafts_from_ready_to_send():
    note = _note()
    server.approve_encounter_note(note_id=note.note_id)
    draft = server.generate_referral_letter(
        note_id=note.note_id,
        specialty="Cardiology",
        referral_reason="Chest tightness.",
        referring_clinician="Dr. A",
    )
    ready = server.generate_referral_letter(
        note_id=note.note_id,
        specialty="Neurology",
        referral_reason="Headaches.",
        referring_clinician="Dr. A",
    )
    server.approve_referral_letter(letter_id=ready.letter_id, approved_by="Dr. A")
    drafts = server.get_attention_items(category="letters_awaiting_approval")
    queue = server.get_attention_items(category="letters_ready_to_send")
    assert [i.item_id for i in drafts.items] == [draft.letter_id]
    assert [i.item_id for i in queue.items] == [ready.letter_id]
    assert queue.items[0].summary == "Referral letter to Neurology"
    assert "mark it sent" in queue.items[0].needs


def test_counts_match_the_dashboard_for_every_category():
    note = _note()
    _gap(note.note_id)
    server.order_test(note_id=note.note_id, test_name="A1c", reason="Annual")
    pending = server.get_activity_summary(period="day", count=1).pending.model_dump()
    for category in typing.get_args(attention_items.Category):
        assert server.get_attention_items(category=category).count == pending[category]


# --- Ordering and detail ----------------------------------------------------------


def test_oldest_item_comes_first_with_hours_waiting():
    now = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)

    def note(nid, hours_ago):
        ts = (now - timedelta(hours=hours_ago)).isoformat()
        return {
            "timestamp": ts,
            "action": "generate_note",
            "note_id": nid,
            "patient_id": GRACE[1],
            "ehr_system": GRACE[0],
        }

    entries = [note("new", 2), note("old", 50), note("mid", 10)]
    patients = {(p["ehr_system"], p["patient_id"]): p for p in server.SAMPLE_PATIENTS}
    listing = attention_items.attention_list(
        entries, "notes_awaiting_review", patients, now
    )
    assert [i.item_id for i in listing.items] == ["old", "mid", "new"]
    assert listing.items[0].waiting_hours == 50.0


def test_unknown_patient_is_shown_not_dropped():
    entry = {
        "timestamp": "2026-10-28T09:00:00+00:00",
        "action": "identify_care_gap",
        "gap_id": "g",
        "note_id": "n",
        "patient_id": "ghost",
        "ehr_system": "epic",
        "description": "X",
    }
    listing = attention_items.attention_list(
        [entry], "open_care_gaps", {}, datetime(2026, 10, 28, tzinfo=timezone.utc)
    )
    assert listing.items[0].patient_name == "Unknown patient (ghost)"
    assert listing.items[0].mrn is None


def test_unreadable_timestamp_waits_zero_hours_rather_than_crashing():
    entry = {
        "timestamp": "garbage",
        "action": "order_test",
        "order_id": "o",
        "note_id": "n",
        "patient_id": GRACE[1],
        "ehr_system": GRACE[0],
        "test_name": "A1c",
    }
    listing = attention_items.attention_list(
        [entry], "tests_awaiting_results", {}, datetime.now(timezone.utc)
    )
    assert listing.items[0].waiting_hours == 0.0


def test_empty_log_gives_an_empty_list():
    listing = server.get_attention_items(category="open_care_gaps")
    assert listing.items == [] and listing.count == 0


# --- Audit -------------------------------------------------------------------------


def test_each_view_is_audited_with_ids_only():
    note = _note()
    gap = _gap(note.note_id, "Secret gap words")
    server.get_attention_items(category="open_care_gaps")
    [view] = [e for e in _entries() if e["action"] == attention_items.VIEW_ACTION]
    assert view["category"] == "open_care_gaps"
    assert view["item_ids"] == [gap.gap_id]
    assert view["patient_ids"] == [GRACE[1]]
    assert "Secret" not in json.dumps(view)


def test_no_note_text_is_returned():
    _note()
    flat = json.dumps(
        server.get_attention_items(category="notes_awaiting_review").model_dump()
    )
    assert "chest tightness" not in flat.lower()


def test_audit_write_failure_returns_nothing():
    _note()
    os.chmod(server.AUDIT_LOG_PATH, stat.S_IREAD)
    try:
        with pytest.raises(ToolError, match="audit trail"):
            server.get_attention_items(category="notes_awaiting_review")
    finally:
        os.chmod(server.AUDIT_LOG_PATH, stat.S_IREAD | stat.S_IWRITE)
    assert not [e for e in _entries() if e["action"] == attention_items.VIEW_ACTION]


def test_compliance_check_still_passes_with_views_in_the_log():
    _note()
    server.get_attention_items(category="notes_awaiting_review")
    assert server.run_compliance_check().all_passed
