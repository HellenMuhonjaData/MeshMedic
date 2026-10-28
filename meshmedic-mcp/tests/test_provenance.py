"""STORY-012 / REQ-013: encounter-supported vs AI-generated information."""

import json
import os
import stat

import pytest
from mcp.server.mcpserver.exceptions import ResourceNotFoundError, ToolError

import provenance
import server

TRANSCRIPT = (
    "Clinician: What brings you in today?\n"
    "Patient: I've been getting headaches most afternoons for about two weeks, and my "
    "home blood pressure readings have been running high, around 150 over 95.\n"
    "Clinician: Any chest pain, vision changes, or shortness of breath?\n"
    "Patient: No, none of that. Just the headaches.\n"
    "Clinician: I'm going to start you on lisinopril 10mg daily and have you recheck "
    "your blood pressure in two weeks."
)
NOTE = (
    "Patient presents with two-week history of afternoon headaches and elevated home "
    "blood pressure readings (avg 150/95). Denies chest pain, vision changes, dyspnea. "
    "Assessment: likely uncontrolled hypertension. "
    "Plan: start lisinopril 10mg daily, recheck BP in 2 weeks."
)


@pytest.fixture(autouse=True)
def isolated_audit_log(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "AUDIT_LOG_PATH", tmp_path / "audit_log.jsonl")


def _entries():
    if not server.AUDIT_LOG_PATH.exists():
        return []
    with server.AUDIT_LOG_PATH.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f]


def _presented():
    return [e for e in _entries() if e["action"] == provenance.PRESENT_ACTION]


def _note(note_text=NOTE, transcript=TRANSCRIPT):
    return server.generate_encounter_note(
        ehr_system="epic",
        patient_id="epic-pt-10293847",
        transcript=transcript,
        note_text=note_text,
        confidence=0.9,
    )


def _sources(labels):
    return [s.source for s in labels.sentences]


# --- Criterion 1: encounter-supported data is labeled ----------------------


def test_sentences_grounded_in_transcript_are_encounter_supported():
    labels = server.label_information_sources(note_id=_note().note_id)
    assert _sources(labels) == [
        provenance.ENCOUNTER,
        provenance.ENCOUNTER,
        provenance.AI,
        provenance.ENCOUNTER,
    ]
    first = labels.sentences[0]
    assert "headaches most afternoons" in first.evidence
    assert first.support_ratio >= provenance.SUPPORT_THRESHOLD


def test_verbatim_transcript_sentence_is_never_labeled_ai_generated():
    # All stopwords: zero content words, so the overlap ratio alone would say
    # ai_generated. The failure path "encounter-supported data mislabeled as
    # AI-generated" must not happen for a sentence the patient actually said.
    note = _note(note_text="It is what it is.", transcript="Patient: It is what it is.")
    labels = server.label_information_sources(note_id=note.note_id)
    assert _sources(labels) == [provenance.ENCOUNTER]
    assert labels.sentences[0].support_ratio == 1.0


def test_chart_facts_count_as_encounter_supported():
    note = _note(note_text="Grace Whitfield, born 1968-03-14.", transcript="Hello.")
    labels = server.label_information_sources(note_id=note.note_id)
    assert _sources(labels) == [provenance.ENCOUNTER]
    assert labels.sentences[0].evidence == "Patient chart"


def test_every_word_of_the_note_is_covered_by_a_labeled_sentence():
    text = "Line one has headaches.\nSecond line?  Third!\n\nFourth sentence here."
    labels = server.label_information_sources(note_id=_note(note_text=text).note_id)
    joined = " ".join(s.text for s in labels.sentences)
    assert joined.split() == text.split()
    assert [s.index for s in labels.sentences] == list(range(len(labels.sentences)))


# --- Criterion 2: AI-generated information is labeled ----------------------


def test_unsupported_inference_is_labeled_ai_generated():
    labels = server.label_information_sources(note_id=_note().note_id)
    assessment = labels.sentences[2]
    assert assessment.text.startswith("Assessment")
    assert assessment.source == provenance.AI
    assert assessment.evidence is None


def test_codes_and_care_gaps_are_ai_generated_whatever_their_review_status():
    note = _note()
    approved = server.suggest_codes(
        note_id=note.note_id,
        codes=[
            server.CodeSuggestion(
                code="I10", code_system="icd-10", description="Essential hypertension"
            )
        ],
        confidence=0.9,
    )
    server.approve_codes(suggestion_id=approved.suggestion_id)
    rejected = server.suggest_codes(
        note_id=note.note_id,
        codes=[
            server.CodeSuggestion(
                code="R51", code_system="icd-10", description="Headache"
            )
        ],
        confidence=0.9,
    )
    server.reject_codes(suggestion_id=rejected.suggestion_id, feedback="Not needed.")
    gap = server.identify_care_gaps(
        note_id=note.note_id,
        gaps=[server.CareGapInput(description="Overdue A1c", confidence=0.9)],
    )[0]
    server.address_care_gap(gap_id=gap.gap_id, resolution="Drawn today.")

    labels = server.label_information_sources(note_id=note.note_id)
    by_id = {i.item_id: i for i in labels.items}
    assert {i.source for i in labels.items} == {provenance.AI}
    assert by_id[approved.suggestion_id].review_status == "approved"
    assert by_id[rejected.suggestion_id].review_status == "rejected"
    assert by_id[gap.gap_id].review_status == "addressed"
    assert "I10 (icd-10) Essential hypertension" in by_id[approved.suggestion_id].text


def test_items_from_another_note_are_not_included():
    other = _note()
    server.identify_care_gaps(
        note_id=other.note_id,
        gaps=[server.CareGapInput(description="X", confidence=0.9)],
    )
    labels = server.label_information_sources(note_id=_note().note_id)
    assert labels.items == []


def test_clinician_added_sentence_is_not_called_ai_generated():
    note = _note()
    server.approve_encounter_note(
        note_id=note.note_id,
        edited_note_text=NOTE + " Patient prefers morning appointments.",
    )
    labels = server.label_information_sources(note_id=note.note_id)
    assert labels.note_status == "approved"
    assert labels.sentences[-1].source == provenance.CLINICIAN
    # The AI's own inference stays AI-generated after approval.
    assert labels.sentences[2].source == provenance.AI


def test_labels_follow_the_latest_draft_edit():
    note = _note()
    server.edit_encounter_note(
        note_id=note.note_id, edited_note_text="Follow up in clinic."
    )
    labels = server.label_information_sources(note_id=note.note_id)
    assert labels.note_status == "draft"
    assert [s.text for s in labels.sentences] == ["Follow up in clinic."]
    assert _sources(labels) == [provenance.CLINICIAN]


# --- Criterion 3 (Trust): the source of each piece is logged ---------------


def test_presentation_logs_a_source_for_every_item_without_text():
    note = _note()
    server.identify_care_gaps(
        note_id=note.note_id,
        gaps=[server.CareGapInput(description="A1c", confidence=0.9)],
    )
    labels = server.label_information_sources(note_id=note.note_id)
    [entry] = _presented()
    assert entry["note_id"] == note.note_id
    assert len(entry["sources"]) == len(labels.sentences) + len(labels.items)
    assert [s["source"] for s in entry["sources"]] == _sources(labels) + [provenance.AI]
    assert entry["counts"] == labels.counts
    flat = json.dumps(entry)
    assert "lisinopril" not in flat and "A1c" not in flat


def test_each_presentation_is_logged_and_labels_are_deterministic():
    note = _note()
    first = server.label_information_sources(note_id=note.note_id)
    second = server.label_information_sources(note_id=note.note_id)
    assert first.model_dump(exclude={"labeled_at"}) == second.model_dump(
        exclude={"labeled_at"}
    )
    assert len(_presented()) == 2


def test_compliance_check_accepts_presentations():
    server.label_information_sources(note_id=_note().note_id)
    assert server.run_compliance_check().all_passed


def test_compliance_flags_a_presentation_with_an_invalid_label():
    note = _note()
    server.label_information_sources(note_id=note.note_id)
    corrupted = _entries()
    for e in corrupted:
        if e["action"] == provenance.PRESENT_ACTION:
            e["sources"][0]["source"] = "unknown"
    server.AUDIT_LOG_PATH.write_text(
        "".join(json.dumps(e) + "\n" for e in corrupted), encoding="utf-8"
    )
    result = server.run_compliance_check()
    control = next(
        c for c in result.controls if c.control_id == "REQ-006-audit-completeness"
    )
    assert not control.passed
    assert "no valid source label" in control.detail


def test_compliance_flags_a_presentation_with_no_note():
    entry = {
        "timestamp": "2026-10-28T00:00:00+00:00",
        "action": provenance.PRESENT_ACTION,
        "note_id": "ghost",
        "sources": [],
    }
    server.AUDIT_LOG_PATH.write_text(json.dumps(entry) + "\n", encoding="utf-8")
    control = next(
        c
        for c in server.run_compliance_check().controls
        if c.control_id == "REQ-006-audit-completeness"
    )
    assert "has no generate_note entry" in control.detail
    assert "records no sources" in control.detail


# --- Failure paths ----------------------------------------------------------


def test_unknown_note_raises_and_logs_nothing():
    with pytest.raises(ResourceNotFoundError):
        server.label_information_sources(note_id="no-such-note")
    assert _presented() == []


def test_audit_write_failure_presents_nothing():
    note = _note()
    os.chmod(server.AUDIT_LOG_PATH, stat.S_IREAD)
    try:
        with pytest.raises(ToolError, match="audit trail"):
            server.label_information_sources(note_id=note.note_id)
    finally:
        os.chmod(server.AUDIT_LOG_PATH, stat.S_IREAD | stat.S_IWRITE)
    assert _presented() == []


def test_labeling_tool_logs_no_note_text(caplog):
    caplog.set_level("INFO")
    server.label_information_sources(note_id=_note().note_id)
    err = caplog.text
    assert "label_information_sources" in err
    assert "lisinopril" not in err


# --- Pure helpers -----------------------------------------------------------


def test_content_words_drop_stopwords_and_plural_s():
    assert provenance.content_words("The headaches were in two weeks") == {
        "headache",
        "two",
        "week",
    }


def test_split_sentences_ignores_blank_lines():
    assert provenance.split_sentences("A.\n\n  B?  C!") == ["A.", "B?", "C!"]
