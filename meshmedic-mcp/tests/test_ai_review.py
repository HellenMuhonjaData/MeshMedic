"""AI suggestions -> clinician review: was it approved as-is, edited, or entered by hand?"""

import json
from datetime import datetime, timedelta

import pytest
from mcp.server.mcpserver.exceptions import ResourceNotFoundError, ToolError

import ai_suggestions
import review_tracking
import app_core
import server
from ai_suggestions import ReferralSuggestion, TestOrderSuggestion


@pytest.fixture(autouse=True)
def isolated_audit_log(tmp_path, monkeypatch):
    monkeypatch.setattr(app_core, "AUDIT_LOG_PATH", tmp_path / "audit_log.jsonl")


def _entries():
    if not app_core.AUDIT_LOG_PATH.exists():
        return []
    with app_core.AUDIT_LOG_PATH.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f]


def _note(source="ai"):
    return server.generate_encounter_note(
        ehr_system="epic",
        patient_id="epic-pt-55219",
        transcript="Fatigue and thirst for six weeks. Glucose 248. Draw an A1c.",
        note_text="Patient reports six weeks of fatigue and thirst. Random glucose 248. A1c ordered.",
        confidence=0.88,
        source=source,
    )


def _suggest(note_id):
    return server.record_ai_suggestions(
        note_id=note_id,
        care_gaps=[server.CareGapInput(description="Overdue for screening mammogram", confidence=0.9)],
        test_orders=[
            TestOrderSuggestion(test_name="Hemoglobin A1c", reason="Confirm suspected diabetes"),
            TestOrderSuggestion(test_name="Basic metabolic panel", reason="Baseline kidney function"),
        ],
        referral=ReferralSuggestion(specialty="Ophthalmology", reason="Dilated eye exam"),
    )


def _item(recorded, kind, n=0):
    return [i for i in recorded.items if i.kind == kind][n]


# ---------- pure comparison logic ----------

T0 = "2026-11-12T10:00:00+00:00"


def _later(seconds):
    return (datetime.fromisoformat(T0) + timedelta(seconds=seconds)).isoformat()


def test_review_text_unchanged_is_as_is_and_whitespace_ignored():
    r = review_tracking.review_text("A  b c.", "A b\nc.", T0, _later(60))
    assert r.outcome == "approved_as_is" and r.edited is False and r.changes == []
    assert r.review_seconds == 60.0


def test_review_text_edit_is_counted():
    r = review_tracking.review_text("one two three", "one two four five", T0, _later(60))
    assert r.outcome == "approved_with_edits" and r.edited is True
    assert "2 word(s) added, 1 removed" in r.changes[0]


def test_fast_unedited_approval_is_possibly_unread():
    long_text = " ".join(["word"] * 400)  # 400 words needs >= 50 s at skim speed
    assert review_tracking.review_text(long_text, long_text, T0, _later(10)).possibly_unread is True
    assert review_tracking.review_text(long_text, long_text, T0, _later(60)).possibly_unread is False


def test_edited_approval_is_never_possibly_unread():
    r = review_tracking.review_text("a b c", "a b d", T0, _later(1))
    assert r.possibly_unread is False


def test_minimum_review_floor_applies_to_short_items():
    r = review_tracking.review_fields({"x": "short"}, {"x": "short"}, T0, _later(2))
    assert r.possibly_unread is True  # under MIN_REVIEW_SECONDS


def test_bad_timestamp_gives_no_timing_and_no_flag():
    r = review_tracking.review_text("a", "a", "not-a-time", _later(1))
    assert r.review_seconds is None and r.possibly_unread is False


def test_review_codes_lists_added_removed_and_changed():
    orig = [
        {"code": "R73.9", "code_system": "icd-10", "description": "Hyperglycemia"},
        {"code": "99214", "code_system": "cpt", "description": "Office visit"},
    ]
    final = [
        {"code": "r73.9", "code_system": "icd-10", "description": "Hyperglycemia, unspecified"},
        {"code": "Z12.31", "code_system": "icd-10", "description": "Screening mammogram"},
    ]
    r = review_tracking.review_codes(orig, final, T0, _later(30))
    assert r.changes == [
        "added icd-10 Z12.31",
        "description changed for icd-10 R73.9",
        "removed cpt 99214",
    ]


# ---------- note and code approvals ----------


def test_note_approved_without_edits_is_recorded_as_is():
    note = _note()
    reviewed = server.approve_encounter_note(note_id=note.note_id)
    assert reviewed.review.outcome == "approved_as_is"
    assert _entries()[-1]["review"]["outcome"] == "approved_as_is"


def test_note_approved_with_edits_is_recorded_with_changes():
    note = _note()
    reviewed = server.approve_encounter_note(
        note_id=note.note_id, edited_note_text=note.note_text + " Follow up in two weeks."
    )
    assert reviewed.review.outcome == "approved_with_edits"
    assert reviewed.review.changes
    # Approving again with the same text is a no-op that returns the same review.
    again = server.approve_encounter_note(
        note_id=note.note_id, edited_note_text=note.note_text + " Follow up in two weeks."
    )
    assert again.review == reviewed.review
    assert [e["action"] for e in _entries()].count("approve_note") == 1


def test_clinician_written_note_is_marked_clinician_entered():
    note = _note(source="clinician")
    assert server.approve_encounter_note(note_id=note.note_id).review.outcome == "clinician_entered"


def test_code_set_edits_are_tracked():
    note = _note()
    codes = [server.CodeSuggestion(code="R73.9", code_system="icd-10", description="Hyperglycemia, unspecified")]
    s = server.suggest_codes(note_id=note.note_id, codes=codes, confidence=0.85)
    edited = codes + [server.CodeSuggestion(code="Z12.31", code_system="icd-10", description="Screening mammogram")]
    reviewed = server.approve_codes(suggestion_id=s.suggestion_id, edited_codes=edited)
    assert reviewed.review.outcome == "approved_with_edits"
    assert reviewed.review.changes == ["added icd-10 Z12.31"]


# ---------- AI suggestions: gaps and multiple orders ----------


def test_record_ai_suggestions_happy_path_and_idempotent():
    note = _note()
    first = _suggest(note.note_id)
    assert [i.kind for i in first.items] == ["care_gap", "test_order", "test_order", "referral"]
    second = _suggest(note.note_id)
    assert second.items == first.items
    assert [e["action"] for e in _entries()].count(ai_suggestions.RECORD_ACTION) == 1


def test_record_ai_suggestions_unknown_note_raises():
    with pytest.raises(ResourceNotFoundError):
        server.record_ai_suggestions(note_id="nope", test_orders=[])


def test_record_ai_suggestions_low_confidence_gap_needs_reason():
    note = _note()
    with pytest.raises(ToolError):
        server.record_ai_suggestions(
            note_id=note.note_id,
            care_gaps=[server.CareGapInput(description="Maybe a gap", confidence=0.3)],
        )


def test_multiple_ai_suggested_orders_track_as_is_and_edited():
    note = _note()
    rec = _suggest(note.note_id)
    a1c, bmp = _item(rec, "test_order", 0), _item(rec, "test_order", 1)
    o1 = server.order_test(
        note_id=note.note_id, test_name="Hemoglobin A1c", reason="Confirm suspected diabetes",
        suggestion_item_id=a1c.item_id,
    )
    o2 = server.order_test(
        note_id=note.note_id, test_name="Comprehensive metabolic panel", reason="Baseline kidney function",
        suggestion_item_id=bmp.item_id,
    )
    o3 = server.order_test(note_id=note.note_id, test_name="Lipid panel", reason="Cardiovascular risk")
    assert o1.review.outcome == "approved_as_is"
    assert o2.review.outcome == "approved_with_edits" and o2.review.changes == ["test_name changed"]
    assert o3.review.outcome == "clinician_entered"
    assert len({o1.order_id, o2.order_id, o3.order_id}) == 3


def test_accepting_the_same_suggested_order_twice_does_not_duplicate():
    note = _note()
    item = _item(_suggest(note.note_id), "test_order")
    first = server.order_test(
        note_id=note.note_id, test_name="Hemoglobin A1c", reason="Confirm suspected diabetes",
        suggestion_item_id=item.item_id,
    )
    second = server.order_test(
        note_id=note.note_id, test_name="Hemoglobin A1c", reason="Confirm suspected diabetes",
        suggestion_item_id=item.item_id,
    )
    assert second.order_id == first.order_id
    assert [e["action"] for e in _entries()].count("order_test") == 1


def test_suggestion_item_from_another_note_is_rejected():
    note_a, note_b = _note(), _note()
    item = _item(_suggest(note_a.note_id), "test_order")
    with pytest.raises(ToolError, match="not a recorded AI test_order"):
        server.order_test(
            note_id=note_b.note_id, test_name="Hemoglobin A1c", reason="x", suggestion_item_id=item.item_id
        )


def test_wrong_kind_of_item_is_rejected():
    note = _note()
    gap_item = _item(_suggest(note.note_id), "care_gap")
    with pytest.raises(ToolError):
        server.order_test(note_id=note.note_id, test_name="A1c", reason="x", suggestion_item_id=gap_item.item_id)


def test_accepted_gap_edit_is_tracked_and_idempotent():
    note = _note()
    item = _item(_suggest(note.note_id), "care_gap")
    gap_input = server.CareGapInput(
        description="Overdue for screening mammogram (last done 2022)", confidence=0.9,
        suggestion_item_id=item.item_id,
    )
    [gap] = server.identify_care_gaps(note_id=note.note_id, gaps=[gap_input])
    assert gap.review.outcome == "approved_with_edits"
    [again] = server.identify_care_gaps(note_id=note.note_id, gaps=[gap_input])
    assert again.gap_id == gap.gap_id
    assert [e["action"] for e in _entries()].count("identify_care_gap") == 1


def test_compliance_flags_an_order_pointing_at_an_unrecorded_suggestion():
    entries = [{"action": "order_test", "order_id": "o1", "suggestion_item_id": "ghost"}]
    assert ai_suggestions.orphan_references(entries)
    note = _note()
    item = _item(_suggest(note.note_id), "test_order")
    server.order_test(note_id=note.note_id, test_name="A1c", reason="x", suggestion_item_id=item.item_id)
    assert ai_suggestions.orphan_references(_entries()) == []
