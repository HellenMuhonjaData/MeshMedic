"""Tests for the AI draft contract: the AI, not the clinician, supplies confidence."""

import json

import pytest

from note_drafting import DraftContractError, parse_draft


def _raw(**fields):
    base = {"note_text": "Patient reports headaches.", "confidence": 0.85, "confidence_reason": "Clear history."}
    base.update(fields)
    return json.dumps(base)


def test_happy_path_returns_ai_confidence():
    draft = parse_draft(_raw())
    assert draft.note_text == "Patient reports headaches."
    assert draft.confidence == 0.85
    assert draft.low_confidence is False


def test_low_confidence_with_reason_is_flagged():
    draft = parse_draft(_raw(confidence=0.4, confidence_reason="No assessment discussed."))
    assert draft.low_confidence is True
    assert draft.confidence_reason == "No assessment discussed."


def test_threshold_boundary_is_not_low():
    assert parse_draft(_raw(confidence=0.7)).low_confidence is False
    assert parse_draft(_raw(confidence=0.69, confidence_reason="Short.")).low_confidence is True


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[1, 2]",
        _raw(note_text="   "),
        _raw(confidence="high"),
        _raw(confidence=True),
        _raw(confidence=1.5),
        _raw(confidence=-0.1),
        _raw(confidence=0.3, confidence_reason="  "),
    ],
)
def test_malformed_drafts_are_rejected(raw):
    with pytest.raises(DraftContractError):
        parse_draft(raw)


def test_parsing_is_deterministic():
    raw = _raw(confidence=0.623456, confidence_reason="Ambiguous plan.")
    assert parse_draft(raw) == parse_draft(raw)
    assert parse_draft(raw).confidence == 0.62


def _full(**fields):
    base = {
        "note_text": "Six weeks of fatigue and thirst. Random glucose 248.",
        "confidence": 0.88,
        "confidence_reason": "Clear history and plan.",
        "codes": [{"code": "R73.9", "code_system": "icd-10", "description": "Hyperglycemia, unspecified"}],
        "codes_confidence": 0.8,
        "codes_confidence_reason": "Diagnosis not yet confirmed.",
        "care_gaps": [{"description": "Overdue for screening mammogram", "confidence": 0.9, "confidence_reason": ""}],
        "test_orders": [
            {"test_name": "Hemoglobin A1c", "reason": "Confirm suspected diabetes"},
            {"test_name": "Basic metabolic panel", "reason": "Baseline"},
        ],
        "referral": {"needed": True, "specialty": "Ophthalmology", "reason": "Dilated eye exam"},
    }
    base.update(fields)
    return json.dumps(base)


def test_full_encounter_draft_parses_every_suggestion():
    d = parse_draft(_full())
    assert d.codes[0]["code"] == "R73.9" and d.codes_confidence == 0.8
    assert d.care_gaps[0]["confidence_reason"] is None  # blank reason on a confident gap
    assert [o["test_name"] for o in d.test_orders] == ["Hemoglobin A1c", "Basic metabolic panel"]
    assert d.referral == {"specialty": "Ophthalmology", "reason": "Dilated eye exam"}


def test_empty_suggestions_and_no_referral():
    d = parse_draft(_full(codes=[], care_gaps=[], test_orders=[], referral={"needed": False, "specialty": "", "reason": ""}))
    assert d.codes == [] and d.codes_confidence is None
    assert d.care_gaps == [] and d.test_orders == [] and d.referral is None


@pytest.mark.parametrize(
    "fields",
    [
        {"codes": [{"code": "", "code_system": "icd-10", "description": "x"}]},
        {"codes": [{"code": "X", "code_system": "snomed", "description": "x"}]},
        {"codes": [{"code": "X" * 21, "code_system": "icd-10", "description": "x"}]},
        {"codes_confidence": 0.4, "codes_confidence_reason": ""},
        {"care_gaps": [{"description": "gap", "confidence": 0.3, "confidence_reason": ""}]},
        {"test_orders": [{"test_name": "A1c", "reason": " "}]},
        {"test_orders": "A1c"},
        {"referral": {"needed": True, "specialty": "", "reason": "x"}},
    ],
)
def test_malformed_suggestions_are_rejected(fields):
    with pytest.raises(DraftContractError):
        parse_draft(_full(**fields))
