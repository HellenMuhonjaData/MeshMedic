import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ResourceNotFoundError, ToolError
from mcp_types import ClientCapabilities, CreateMessageResult, ImageContent, ListRootsResult, Root, SamplingCapability, TextContent

import github_client
import server


def _run(coro):
    """Run an async tool/resolver function's coroutine to completion and
    return its result -- this project doesn't add pytest-asyncio as a
    dependency just for a handful of direct-call tests."""
    return asyncio.run(coro)


def _roots_for(*paths):
    return ListRootsResult(roots=[Root(uri=Path(p).resolve().as_uri()) for p in paths])


class _FakeCtx:
    """Duck-typed stand-in for Context in direct-call tests -- exposes only
    the attributes prioritize_care_gaps, its resolver, and emit_progress
    actually read (client_capabilities, request_id, request_context.meta),
    same pattern as this file's existing FakeCtx-style helper for
    search_ehr_patient's progress reporting. A bare `meta=None` means
    emit_progress always no-ops, matching "no progress token supplied"."""

    def __init__(self, request_id="test-request-id", sampling_supported=False, with_progress_token=False):
        self.request_id = request_id
        self.client_capabilities = ClientCapabilities(sampling=SamplingCapability()) if sampling_supported else None
        meta = {"progress_token": "test-progress-token"} if with_progress_token else None
        self.request_context = SimpleNamespace(meta=meta)
        self.progress_calls = []

    async def report_progress(self, progress, total, message):
        self.progress_calls.append((progress, total, message))


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


def _fixed_datetime(times):
    """Fake stand-in for the `datetime` class server.py imports, so STORY-009
    tests can control the elapsed time between a note's generation and its
    review decision without a real 2-minute sleep. `now(tz)` returns the next
    value from `times` on each call; `fromisoformat` delegates to the real
    implementation, since _documentation_time_seconds parses stored
    audit-trail timestamps with it."""
    it = iter(times)

    class _FakeDatetime:
        @staticmethod
        def now(tz=None):
            return next(it)

        @staticmethod
        def fromisoformat(value):
            return datetime.fromisoformat(value)

    return _FakeDatetime


def test_approve_encounter_note_under_target_has_no_suggestions_and_logs_time(monkeypatch):
    t0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(server, "datetime", _fixed_datetime([t0, t0 + timedelta(seconds=30)]))

    note = _generate_note()
    reviewed = server.approve_encounter_note(note_id=note.note_id)

    assert reviewed.documentation_time_seconds == 30.0
    assert reviewed.exceeded_target is False
    assert reviewed.suggestions is None

    entries = _read_audit_entries()
    assert entries[1]["documentation_time_seconds"] == 30.0


def test_approve_encounter_note_at_target_boundary_is_not_exceeded(monkeypatch):
    t0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        server, "datetime",
        _fixed_datetime([t0, t0 + timedelta(seconds=server.REVIEW_TIME_TARGET_SECONDS)]),
    )

    note = _generate_note()
    reviewed = server.approve_encounter_note(note_id=note.note_id)

    assert reviewed.documentation_time_seconds == server.REVIEW_TIME_TARGET_SECONDS
    assert reviewed.exceeded_target is False
    assert reviewed.suggestions is None


def test_approve_encounter_note_over_target_returns_speed_up_suggestions(monkeypatch):
    t0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(server, "datetime", _fixed_datetime([t0, t0 + timedelta(seconds=150)]))

    note = _generate_note()
    reviewed = server.approve_encounter_note(note_id=note.note_id)

    assert reviewed.documentation_time_seconds == 150.0
    assert reviewed.exceeded_target is True
    assert reviewed.suggestions == server.SPEED_UP_SUGGESTIONS

    entries = _read_audit_entries()
    assert entries[1]["documentation_time_seconds"] == 150.0


def test_reject_encounter_note_over_target_returns_speed_up_suggestions(monkeypatch):
    t0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(server, "datetime", _fixed_datetime([t0, t0 + timedelta(seconds=125)]))

    note = _generate_note()
    reviewed = server.reject_encounter_note(note_id=note.note_id, feedback="Needs more detail.")

    assert reviewed.documentation_time_seconds == 125.0
    assert reviewed.exceeded_target is True
    assert reviewed.suggestions == server.SPEED_UP_SUGGESTIONS


def test_approve_encounter_note_idempotent_replay_reuses_stored_documentation_time(monkeypatch):
    t0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(server, "datetime", _fixed_datetime([t0, t0 + timedelta(seconds=150)]))

    note = _generate_note()
    first = server.approve_encounter_note(note_id=note.note_id)
    second = server.approve_encounter_note(note_id=note.note_id)

    assert first == second
    assert second.documentation_time_seconds == 150.0
    assert second.suggestions == server.SPEED_UP_SUGGESTIONS
    # The no-op replay reused the stored duration (no third `now()` call
    # queued above) and did not write a second audit entry.
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
        confidence=0.95,
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


def test_suggest_codes_flags_low_confidence_with_reason():
    note = _generate_note()

    suggestion = server.suggest_codes(
        note_id=note.note_id,
        codes=[server.CodeSuggestion(code="R51.9", code_system="icd-10", description="Headache, unspecified")],
        confidence=0.4,
        confidence_reason="Transcript doesn't clearly support a headache-only visit; may need a broader code.",
    )

    assert suggestion.flagged is True
    assert suggestion.warning is not None

    entries = _read_audit_entries()
    assert entries[1]["flagged"] is True
    assert entries[1]["confidence"] == 0.4


def test_suggest_codes_low_confidence_without_reason_raises_and_does_not_audit():
    note = _generate_note()

    with pytest.raises(ToolError):
        server.suggest_codes(
            note_id=note.note_id,
            codes=[server.CodeSuggestion(code="R51.9", code_system="icd-10", description="Headache, unspecified")],
            confidence=0.4,
        )

    # Only the generate_note entry exists -- nothing from the rejected call.
    assert len(_read_audit_entries()) == 1


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


def _identify_gaps(note_id, descriptions, confidence=0.95, confidence_reason=None):
    return server.identify_care_gaps(
        note_id=note_id,
        gaps=[
            server.CareGapInput(description=d, confidence=confidence, confidence_reason=confidence_reason)
            for d in descriptions
        ],
    )


def test_identify_care_gaps_happy_path_returns_independent_gaps():
    note = _generate_note()

    gaps = _identify_gaps(
        note.note_id,
        [
            "Patient overdue for mammogram - last screening 3+ years ago",
            "No documented flu vaccine for current season",
        ],
    )

    assert len(gaps) == 2
    assert gaps[0].status == "flagged"
    assert gaps[0].gap_id != gaps[1].gap_id
    assert gaps[0].description.startswith("Patient overdue")
    assert gaps[1].description.startswith("No documented flu")
    assert gaps[0].flagged is False
    assert gaps[0].warning is None

    entries = _read_audit_entries()
    assert len(entries) == 3
    assert entries[1]["action"] == "identify_care_gap"
    assert entries[2]["action"] == "identify_care_gap"
    assert entries[1]["gap_id"] == gaps[0].gap_id
    assert entries[2]["gap_id"] == gaps[1].gap_id


def test_identify_care_gaps_unknown_note_id_raises_and_does_not_audit():
    with pytest.raises(ResourceNotFoundError):
        _identify_gaps("does-not-exist", ["Some gap."])

    assert _read_audit_entries() == []


def test_identify_care_gaps_flags_low_confidence_with_reason():
    note = _generate_note()

    gaps = _identify_gaps(
        note.note_id,
        ["Possible missed screening, unclear from transcript"],
        confidence=0.3,
        confidence_reason="Transcript only vaguely mentions a prior test, not what kind.",
    )

    assert gaps[0].flagged is True
    assert gaps[0].warning is not None
    assert "0.30" in gaps[0].warning

    entries = _read_audit_entries()
    assert entries[1]["flagged"] is True
    assert entries[1]["confidence"] == 0.3


def test_identify_care_gaps_low_confidence_without_reason_raises_and_does_not_audit():
    note = _generate_note()

    with pytest.raises(ToolError):
        _identify_gaps(note.note_id, ["Some vague gap."], confidence=0.3)

    # Only the generate_note entry exists -- nothing from the rejected call.
    assert len(_read_audit_entries()) == 1


def test_identify_care_gaps_rejects_whole_batch_if_any_gap_fails_validation():
    note = _generate_note()

    with pytest.raises(ToolError):
        server.identify_care_gaps(
            note_id=note.note_id,
            gaps=[
                server.CareGapInput(description="High confidence gap.", confidence=0.95),
                server.CareGapInput(description="Low confidence, no reason.", confidence=0.2),
            ],
        )

    # Neither gap was written -- validated before any audit entry for this call, not partially.
    assert len(_read_audit_entries()) == 1  # generate_note only


def test_address_care_gap_happy_path():
    note = _generate_note()
    gaps = _identify_gaps(note.note_id, ["Overdue for mammogram."])

    addressed = server.address_care_gap(
        gap_id=gaps[0].gap_id,
        resolution="Ordered mammogram referral, scheduled for next week.",
    )

    assert addressed.status == "addressed"
    assert addressed.resolution == "Ordered mammogram referral, scheduled for next week."
    assert addressed.description == "Overdue for mammogram."

    entries = _read_audit_entries()
    assert len(entries) == 3  # generate_note + identify_care_gap + address_care_gap
    assert entries[2]["action"] == "address_care_gap"
    assert entries[2]["gap_id"] == gaps[0].gap_id
    assert entries[2]["resolution"] == "Ordered mammogram referral, scheduled for next week."


def test_care_gaps_from_same_call_are_addressed_independently():
    note = _generate_note()
    gaps = _identify_gaps(note.note_id, ["Overdue for mammogram.", "Missing flu vaccine."])

    server.address_care_gap(gap_id=gaps[0].gap_id, resolution="Ordered mammogram referral.")

    # The second gap from the same call is untouched -- still just flagged, not addressed.
    still_open = server._find_gap(gaps[1].gap_id)
    assert still_open["addressed"] is None

    entries = _read_audit_entries()
    assert len(entries) == 4  # generate_note + 2 identify + 1 address
    assert entries[3]["gap_id"] == gaps[0].gap_id


def test_address_care_gap_unknown_gap_id_raises_and_does_not_audit():
    with pytest.raises(ResourceNotFoundError):
        server.address_care_gap(gap_id="does-not-exist", resolution="Some resolution.")

    assert _read_audit_entries() == []


def test_address_care_gap_conflicting_resolution_raises_tool_error():
    note = _generate_note()
    gaps = _identify_gaps(note.note_id, ["Overdue for mammogram."])
    server.address_care_gap(gap_id=gaps[0].gap_id, resolution="Ordered mammogram referral.")

    with pytest.raises(ToolError):
        server.address_care_gap(gap_id=gaps[0].gap_id, resolution="Actually, patient declined.")

    # No entry was written for the conflicting attempt (generate_note + identify + first address only).
    assert len(_read_audit_entries()) == 3


def test_address_care_gap_is_idempotent_for_identical_replay():
    note = _generate_note()
    gaps = _identify_gaps(note.note_id, ["Overdue for mammogram."])
    first = server.address_care_gap(gap_id=gaps[0].gap_id, resolution="Ordered mammogram referral.")
    second = server.address_care_gap(gap_id=gaps[0].gap_id, resolution="Ordered mammogram referral.")

    assert first == second
    # Replaying the identical resolution did not write a second audit entry
    # (generate_note + identify + one address only).
    assert len(_read_audit_entries()) == 3


def test_fetch_patient_from_ehr_epic_happy_path(monkeypatch):
    fake_patient = {"resourceType": "Patient", "id": "e63wRTbPfr1p8UW81d8Seiw3", "name": [{"family": "Lopez"}]}
    monkeypatch.setattr(server, "fetch_patient", lambda fhir_patient_id, correlation_id: fake_patient)

    result = _run(
        server.fetch_patient_from_ehr(ehr_system="epic", fhir_patient_id="e63wRTbPfr1p8UW81d8Seiw3", ctx=_FakeCtx())
    )

    assert result == fake_patient

    entries = _read_audit_entries()
    assert len(entries) == 1
    assert entries[0]["action"] == "fetch_ehr_patient"
    assert entries[0]["ehr_system"] == "epic"
    assert entries[0]["outcome"] == "success"


def test_fetch_patient_from_ehr_oracle_health_raises_and_logs_failure():
    with pytest.raises(ToolError):
        _run(server.fetch_patient_from_ehr(ehr_system="oracle_health", fhir_patient_id="some-id", ctx=_FakeCtx()))

    entries = _read_audit_entries()
    assert len(entries) == 1
    assert entries[0]["action"] == "fetch_ehr_patient"
    assert entries[0]["ehr_system"] == "oracle_health"
    assert entries[0]["outcome"] == "failure"
    assert entries[0]["error_class"] == "UpstreamUnavailable"


def test_fetch_patient_from_ehr_not_found_raises_and_logs_failure(monkeypatch):
    monkeypatch.setattr(server, "fetch_patient", lambda fhir_patient_id, correlation_id: None)

    with pytest.raises(ResourceNotFoundError):
        _run(server.fetch_patient_from_ehr(ehr_system="epic", fhir_patient_id="does-not-exist", ctx=_FakeCtx()))

    entries = _read_audit_entries()
    assert len(entries) == 1
    assert entries[0]["outcome"] == "failure"
    assert entries[0]["error_class"] == "ResourceNotFoundError"


def test_fetch_patient_from_ehr_upstream_error_raises_and_logs_failure(monkeypatch):
    def _raise(fhir_patient_id, correlation_id):
        raise server.EpicFHIRError("simulated network failure")

    monkeypatch.setattr(server, "fetch_patient", _raise)

    with pytest.raises(ToolError):
        _run(
            server.fetch_patient_from_ehr(
                ehr_system="epic", fhir_patient_id="e63wRTbPfr1p8UW81d8Seiw3", ctx=_FakeCtx()
            )
        )

    entries = _read_audit_entries()
    assert len(entries) == 1
    assert entries[0]["outcome"] == "failure"
    assert entries[0]["error_class"] == "UpstreamUnavailable"


def test_search_ehr_patient_emits_progress_with_a_real_total_when_a_token_is_present():
    ctx = _FakeCtx(with_progress_token=True)

    result = _run(server.search_ehr_patient(ehr_system="epic", mrn="E10293847", ctx=ctx))

    assert result.count == 1
    epic_candidate_count = sum(1 for p in server.SAMPLE_PATIENTS if p["ehr_system"] == "epic")
    assert len(ctx.progress_calls) == epic_candidate_count
    for position, (progress, total, message) in enumerate(ctx.progress_calls, start=1):
        assert progress == position
        assert total == epic_candidate_count  # REAL total, not invented
        assert f"{position} of {epic_candidate_count}" in message


def test_search_ehr_patient_emits_no_progress_without_a_token():
    ctx = _FakeCtx(with_progress_token=False)

    result = _run(server.search_ehr_patient(ehr_system="epic", mrn="E10293847", ctx=ctx))

    assert result.count == 1  # behaves identically either way
    assert ctx.progress_calls == []


def test_fetch_patient_from_ehr_emits_progress_without_a_total_when_a_token_is_present(monkeypatch):
    fake_patient = {"resourceType": "Patient", "id": "e63wRTbPfr1p8UW81d8Seiw3"}
    monkeypatch.setattr(server, "fetch_patient", lambda fhir_patient_id, correlation_id: fake_patient)
    ctx = _FakeCtx(with_progress_token=True)

    result = _run(
        server.fetch_patient_from_ehr(ehr_system="epic", fhir_patient_id="e63wRTbPfr1p8UW81d8Seiw3", ctx=ctx)
    )

    assert result == fake_patient
    assert len(ctx.progress_calls) == 1
    progress, total, message = ctx.progress_calls[0]
    assert total is None  # genuinely unknown, not invented
    assert "no fixed step count" in message


def test_fetch_patient_from_ehr_emits_no_progress_without_a_token(monkeypatch):
    fake_patient = {"resourceType": "Patient", "id": "e63wRTbPfr1p8UW81d8Seiw3"}
    monkeypatch.setattr(server, "fetch_patient", lambda fhir_patient_id, correlation_id: fake_patient)
    ctx = _FakeCtx(with_progress_token=False)

    result = _run(
        server.fetch_patient_from_ehr(ehr_system="epic", fhir_patient_id="e63wRTbPfr1p8UW81d8Seiw3", ctx=ctx)
    )

    assert result == fake_patient  # behaves identically either way
    assert ctx.progress_calls == []


def test_request_citation_happy_path_exact_match():
    note = _generate_note()

    result = server.request_citation(
        note_id=note.note_id,
        claimed_excerpt="Patient reports mild headache for two days, no fever.",
    )

    assert result.found is True
    assert result.matched_text == "Patient reports mild headache for two days, no fever."
    assert result.start_offset == 0
    assert result.end_offset == len("Patient reports mild headache for two days, no fever.")
    assert result.explanation is None

    entries = _read_audit_entries()
    assert len(entries) == 2
    assert entries[1]["action"] == "request_citation"
    assert entries[1]["found"] is True


def test_request_citation_tolerant_of_whitespace_and_case():
    note = _generate_note()

    result = server.request_citation(
        note_id=note.note_id,
        claimed_excerpt="  patient   reports MILD headache\nfor two days, no fever.  ",
    )

    assert result.found is True
    assert result.matched_text == "Patient reports mild headache for two days, no fever."


def test_request_citation_not_found_for_fabricated_text_returns_explanation():
    note = _generate_note()

    result = server.request_citation(
        note_id=note.note_id,
        claimed_excerpt="Patient reports chest pain radiating to the left arm.",
    )

    assert result.found is False
    assert result.matched_text is None
    assert result.start_offset is None
    assert result.explanation is not None

    entries = _read_audit_entries()
    assert len(entries) == 2
    assert entries[1]["action"] == "request_citation"
    assert entries[1]["found"] is False


def test_request_citation_does_not_match_paraphrase():
    note = _generate_note()

    # A reasonable paraphrase of the transcript, not a verbatim quote --
    # must NOT be reported as found, proving this is a real match, not fuzzy.
    result = server.request_citation(
        note_id=note.note_id,
        claimed_excerpt="the patient has had a headache for a couple of days",
    )

    assert result.found is False


def test_request_citation_unknown_note_id_raises_and_does_not_audit():
    with pytest.raises(ResourceNotFoundError):
        server.request_citation(note_id="does-not-exist", claimed_excerpt="Some text.")

    assert _read_audit_entries() == []


def test_read_transcript_file_allows_path_inside_declared_root(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    transcript_file = allowed / "transcript.txt"
    transcript_file.write_text("Patient reports mild cough.", encoding="utf-8")

    result = server.read_transcript_file(file_path=str(transcript_file), roots=_roots_for(allowed))

    assert result.denied is False
    assert result.transcript == "Patient reports mild cough."
    assert result.error is None

    entries = _read_audit_entries()
    assert len(entries) == 1
    assert entries[0]["action"] == "read_transcript_file"
    assert entries[0]["outcome"] == "success"


def test_read_transcript_file_denies_dot_dot_traversal_out_of_root(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    secret = tmp_path / "secret"
    secret.mkdir()
    (secret / "passwd.txt").write_text("TOP SECRET", encoding="utf-8")

    traversal_path = str(allowed / ".." / "secret" / "passwd.txt")
    result = server.read_transcript_file(file_path=traversal_path, roots=_roots_for(allowed))

    assert result.denied is True
    assert result.transcript is None
    assert "outside" in result.error.lower()

    entries = _read_audit_entries()
    assert len(entries) == 1
    assert entries[0]["action"] == "read_transcript_file"
    assert entries[0]["outcome"] == "failure"
    assert entries[0]["error_class"] == "AccessDenied"


def test_read_transcript_file_denies_symlink_escape_out_of_root(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    secret = tmp_path / "secret"
    secret.mkdir()
    (secret / "passwd.txt").write_text("TOP SECRET", encoding="utf-8")
    link = allowed / "escape"

    try:
        os.symlink(secret, link, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is not permitted in this environment (needs Developer Mode or admin on Windows)")

    result = server.read_transcript_file(file_path=str(link / "passwd.txt"), roots=_roots_for(allowed))

    assert result.denied is True
    assert result.transcript is None


def test_read_transcript_file_denies_when_client_declares_no_roots(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    transcript_file = allowed / "transcript.txt"
    transcript_file.write_text("Patient reports mild cough.", encoding="utf-8")

    result = server.read_transcript_file(file_path=str(transcript_file), roots=ListRootsResult(roots=[]))

    assert result.denied is True


def test_read_transcript_file_unknown_file_raises_resource_not_found(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()

    with pytest.raises(ResourceNotFoundError):
        server.read_transcript_file(file_path=str(allowed / "does_not_exist.txt"), roots=_roots_for(allowed))

    assert _read_audit_entries() == []


def test_read_transcript_file_rejects_oversized_file(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "MAX_TRANSCRIPT_FILE_BYTES", 10)
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    big_file = allowed / "big.txt"
    big_file.write_text("this is definitely more than ten bytes", encoding="utf-8")

    with pytest.raises(ToolError):
        server.read_transcript_file(file_path=str(big_file), roots=_roots_for(allowed))


def _note_with_gaps(*descriptions):
    note = server.generate_encounter_note(
        ehr_system="epic",
        patient_id="epic-pt-10293847",
        transcript="Transcript covering several care gaps.",
        note_text="Note text.",
        confidence=0.9,
    )
    gaps = server.identify_care_gaps(
        note_id=note.note_id,
        gaps=[server.CareGapInput(description=d, confidence=0.9) for d in descriptions],
    )
    return note, gaps


def test_open_care_gaps_for_patient_excludes_addressed_gaps():
    _note, gaps = _note_with_gaps("Overdue flu shot", "Overdue mammogram")
    server.address_care_gap(gap_id=gaps[0].gap_id, resolution="Flu shot administered.")

    open_gaps = server._open_care_gaps_for_patient("epic-pt-10293847")

    assert [g["gap_id"] for g in open_gaps] == [gaps[1].gap_id]


def test_list_open_gaps_resolver_returns_none_when_no_open_gaps():
    outcome = _run(
        server._list_open_gaps_resolver(patient_id="epic-pt-10293847", ctx=_FakeCtx(sampling_supported=True))
    )

    assert outcome is None


def test_list_open_gaps_resolver_returns_none_without_sampling_capability():
    _note_with_gaps("Overdue colonoscopy")

    outcome = _run(
        server._list_open_gaps_resolver(patient_id="epic-pt-10293847", ctx=_FakeCtx(sampling_supported=False))
    )

    assert outcome is None


def test_list_open_gaps_resolver_returns_sample_marker_with_capability():
    _note, gaps = _note_with_gaps("Overdue colonoscopy")

    outcome = _run(
        server._list_open_gaps_resolver(patient_id="epic-pt-10293847", ctx=_FakeCtx(sampling_supported=True))
    )

    assert isinstance(outcome, server.Sample)
    assert outcome.params.max_tokens == server.PRIORITIZE_GAPS_MAX_TOKENS
    assert outcome.params.system_prompt == server.PRIORITIZE_GAPS_SYSTEM_PROMPT
    assert gaps[0].gap_id in outcome.params.messages[0].content.text
    assert "Overdue colonoscopy" in outcome.params.messages[0].content.text


def test_prioritize_care_gaps_degrades_when_no_open_gaps():
    result = _run(
        server.prioritize_care_gaps(patient_id="epic-pt-10293847", completion=None, ctx=_FakeCtx(sampling_supported=True))
    )

    assert result.degraded is True
    assert result.degraded_reason == "no_open_care_gaps"
    assert result.gap_count == 0
    assert result.ranking_text is None


def test_prioritize_care_gaps_degrades_when_client_lacks_sampling_capability():
    _note_with_gaps("Overdue flu shot")

    result = _run(
        server.prioritize_care_gaps(patient_id="epic-pt-10293847", completion=None, ctx=_FakeCtx(sampling_supported=False))
    )

    assert result.degraded is True
    assert result.degraded_reason == "sampling_not_supported_by_client"
    assert result.gap_count == 1
    assert result.ranking_text is None


def test_prioritize_care_gaps_returns_ranking_text_from_real_completion():
    _note_with_gaps("Overdue flu shot", "Overdue mammogram")
    completion = CreateMessageResult(
        role="assistant",
        content=TextContent(type="text", text="1. Flu shot is most urgent. 2. Mammogram can wait."),
        model="client-chosen-model",
    )

    result = _run(
        server.prioritize_care_gaps(
            patient_id="epic-pt-10293847", completion=completion, ctx=_FakeCtx(sampling_supported=True)
        )
    )

    assert result.degraded is False
    assert result.degraded_reason is None
    assert result.gap_count == 2
    assert result.ranking_text == "1. Flu shot is most urgent. 2. Mammogram can wait."


def test_prioritize_care_gaps_duration_is_measured_when_resolver_and_body_share_a_request():
    _note_with_gaps("Overdue colonoscopy")
    ctx = _FakeCtx(request_id="shared-id", sampling_supported=True)

    marker = _run(server._list_open_gaps_resolver(patient_id="epic-pt-10293847", ctx=ctx))
    assert marker is not None

    completion = CreateMessageResult(
        role="assistant", content=TextContent(type="text", text="Colonoscopy is overdue."), model="client-model"
    )
    result = _run(server.prioritize_care_gaps(patient_id="epic-pt-10293847", completion=completion, ctx=ctx))

    assert result.degraded is False
    assert "shared-id" not in server._sampling_calls  # popped, not leaked


def test_extract_ranking_text_falls_back_cleanly_on_non_text_content():
    image = ImageContent(type="image", data="base64data", mime_type="image/png")

    text = server._extract_ranking_text(image)

    assert text  # never empty
    assert "image" in text


def test_prioritize_care_gaps_emits_progress_at_start_and_finish_without_a_total():
    _note_with_gaps("Overdue colonoscopy")
    ctx = _FakeCtx(sampling_supported=True, with_progress_token=True)

    marker = _run(server._list_open_gaps_resolver(patient_id="epic-pt-10293847", ctx=ctx))
    assert marker is not None
    assert len(ctx.progress_calls) == 1
    assert ctx.progress_calls[0][1] is None  # total: genuinely unknown for a single round-trip

    completion = CreateMessageResult(
        role="assistant", content=TextContent(type="text", text="Colonoscopy is overdue."), model="client-model"
    )
    _run(server.prioritize_care_gaps(patient_id="epic-pt-10293847", completion=completion, ctx=ctx))

    assert len(ctx.progress_calls) == 2
    assert ctx.progress_calls[1][1] is None  # total


def test_prioritize_care_gaps_emits_no_progress_without_a_token():
    _note_with_gaps("Overdue colonoscopy")
    ctx = _FakeCtx(sampling_supported=True, with_progress_token=False)

    marker = _run(server._list_open_gaps_resolver(patient_id="epic-pt-10293847", ctx=ctx))
    assert marker is not None

    completion = CreateMessageResult(
        role="assistant", content=TextContent(type="text", text="Colonoscopy is overdue."), model="client-model"
    )
    _run(server.prioritize_care_gaps(patient_id="epic-pt-10293847", completion=completion, ctx=ctx))

    assert ctx.progress_calls == []


def test_check_github_issue_status_found_open_issue(monkeypatch):
    monkeypatch.setattr(
        server, "get_issue_or_pr",
        lambda issue_number, correlation_id: {
            "found": True,
            "data": {"state": "open", "title": "Something worth fixing", "number": issue_number},
        },
    )

    result = _run(server.check_github_issue_status(issue_number=42, ctx=_FakeCtx()))

    assert result.ok is True
    assert result.found is True
    assert result.state == "open"
    assert result.title == "Something worth fixing"
    assert result.is_pull_request is False
    assert result.error is None


def test_check_github_issue_status_found_pull_request(monkeypatch):
    monkeypatch.setattr(
        server, "get_issue_or_pr",
        lambda issue_number, correlation_id: {
            "found": True,
            "data": {"state": "closed", "title": "A merged PR", "pull_request": {"url": "..."}},
        },
    )

    result = _run(server.check_github_issue_status(issue_number=7, ctx=_FakeCtx()))

    assert result.ok is True
    assert result.state == "closed"
    assert result.is_pull_request is True


def test_check_github_issue_status_not_found(monkeypatch):
    monkeypatch.setattr(server, "get_issue_or_pr", lambda issue_number, correlation_id: {"found": False})

    result = _run(server.check_github_issue_status(issue_number=999999, ctx=_FakeCtx()))

    assert result.ok is True
    assert result.found is False
    assert result.title is None
    assert result.error is None


def test_check_github_issue_status_times_out_returns_result_not_raise(monkeypatch):
    def _raise_timeout(issue_number, correlation_id):
        raise httpx.TimeoutException("simulated timeout")

    monkeypatch.setattr(server, "get_issue_or_pr", _raise_timeout)

    result = _run(server.check_github_issue_status(issue_number=1, ctx=_FakeCtx()))

    assert result.ok is False
    assert "Timed out" in result.error
    entries = _read_audit_entries()
    assert entries[-1]["error_class"] == "TimeoutError"


def test_check_github_issue_status_upstream_error_returns_result_not_raise(monkeypatch):
    def _raise_upstream(issue_number, correlation_id):
        raise server.GitHubAPIError("GitHub API rejected the request (status 500).")

    monkeypatch.setattr(server, "get_issue_or_pr", _raise_upstream)

    result = _run(server.check_github_issue_status(issue_number=1, ctx=_FakeCtx()))

    assert result.ok is False
    assert result.error  # never empty
    assert "500" not in result.error  # no upstream detail leaked into the caller-facing message


def test_check_github_issue_status_unexpected_error_returns_result_not_raise(monkeypatch):
    def _raise_unexpected(issue_number, correlation_id):
        raise RuntimeError("something nobody named ahead of time")

    monkeypatch.setattr(server, "get_issue_or_pr", _raise_unexpected)

    result = _run(server.check_github_issue_status(issue_number=1, ctx=_FakeCtx()))

    assert result.ok is False
    assert result.error
    entries = _read_audit_entries()
    assert entries[-1]["error_class"] == "UnexpectedError"


def test_check_github_issue_status_never_leaks_a_credential_or_host_in_the_error(monkeypatch):
    def _raise_upstream(issue_number, correlation_id):
        raise server.GitHubAPIError(
            "GitHub API rejected the request (status 401): bad credentials for token ghp_fake123"
        )

    monkeypatch.setattr(server, "get_issue_or_pr", _raise_upstream)

    result = _run(server.check_github_issue_status(issue_number=1, ctx=_FakeCtx()))

    assert "ghp_fake123" not in result.error
    assert "api.github.com" not in result.error


def test_github_client_reads_token_from_environment_not_hardcoded(monkeypatch):
    captured_headers = {}

    class _FakeResponse:
        status_code = 200

        def json(self):
            return {"state": "open", "title": "x"}

        def close(self):
            pass

    def _fake_get(url, headers=None, **kwargs):
        captured_headers.update(headers or {})
        return _FakeResponse()

    monkeypatch.setattr(github_client._http_client, "get", _fake_get)
    monkeypatch.setenv("GITHUB_TOKEN", "test-token-value")

    github_client.get_issue_or_pr(1, correlation_id="test-correlation")

    assert captured_headers.get("Authorization") == "Bearer test-token-value"


def test_github_client_omits_auth_header_when_no_token_in_environment(monkeypatch):
    captured_headers = {}

    class _FakeResponse:
        status_code = 404

        def close(self):
            pass

    def _fake_get(url, headers=None, **kwargs):
        captured_headers.update(headers or {})
        return _FakeResponse()

    monkeypatch.setattr(github_client._http_client, "get", _fake_get)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)

    github_client.get_issue_or_pr(1, correlation_id="test-correlation")

    assert "Authorization" not in captured_headers


def test_github_client_releases_response_in_finally_even_on_error_status(monkeypatch):
    closed = []

    class _FakeResponse:
        status_code = 500

        def close(self):
            closed.append(True)

    monkeypatch.setattr(github_client._http_client, "get", lambda url, headers=None, **kwargs: _FakeResponse())

    with pytest.raises(github_client.GitHubAPIError):
        github_client.get_issue_or_pr(1, correlation_id="test-correlation")

    assert closed == [True]


def test_append_audit_entry_write_failure_raises_tool_error_not_swallowed(tmp_path, monkeypatch):
    # Point the audit log at a directory instead of a file: opening it in
    # append mode fails with OSError on both Windows and POSIX, simulating a
    # real write failure (disk full, permission denied) without needing a
    # platform-specific chmod trick.
    monkeypatch.setattr(server, "AUDIT_LOG_PATH", tmp_path)

    with pytest.raises(ToolError, match="audit trail"):
        server.generate_encounter_note(
            ehr_system="epic",
            patient_id="epic-pt-10293847",
            transcript="Patient reports mild headache for two days, no fever.",
            note_text="Grace Whitfield presents with a two-day history of mild headache, afebrile.",
            confidence=0.95,
        )


def _raw_append_audit_entry(entry: dict):
    """Write a line straight to the isolated audit log, bypassing every tool
    in server.py -- used only to simulate a corrupted/hand-edited audit
    trail for the compliance-check failure-path tests below. No tool in this
    server can produce these entries itself; that's the point."""
    with server.AUDIT_LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def test_log_event_violations_detects_a_forbidden_key_in_a_synthetic_snippet():
    snippet = 'log_event(_logger, "info", TOOL_STARTED, correlation_id, tool="x", note_text="secret")'

    violations = server._log_event_violations(snippet)

    assert len(violations) == 1
    assert "note_text" in violations[0]
    assert "line 1" in violations[0]


def test_log_event_violations_finds_every_bad_call_site_not_just_the_first():
    snippet = (
        'log_event(_logger, "info", TOOL_STARTED, correlation_id, transcript="a")\n'
        'log_event(_logger, "info", TOOL_STARTED, correlation_id, tool="ok")\n'
        'log_event(_logger, "info", TOOL_STARTED, correlation_id, feedback="b")\n'
    )

    violations = server._log_event_violations(snippet)

    assert len(violations) == 2
    assert any("transcript" in v for v in violations)
    assert any("feedback" in v for v in violations)


def test_log_event_violations_is_not_fooled_by_a_forbidden_name_as_a_value_or_in_a_comment():
    # "note_text" and "mrn" appear here, but never as a log_event keyword
    # name -- an AST-based check must not flag either.
    snippet = (
        '# do not log note_text or mrn here\n'
        'log_event(_logger, "info", TOOL_STARTED, correlation_id, tool="note_text", reason="mrn")\n'
    )

    violations = server._log_event_violations(snippet)

    assert violations == []


def test_run_compliance_check_includes_a_passing_no_phi_in_logs_control_on_the_real_file():
    result = server.run_compliance_check()

    phi_control = next(c for c in result.controls if c.control_id == "REQ-011-no-phi-in-structured-logs")
    assert phi_control.passed is True
    assert phi_control.detail == "No forbidden keys found in any log_event(...) call."


def test_run_compliance_check_all_pass_on_a_clean_normal_audit_trail():
    note = _generate_note()
    server.approve_encounter_note(note_id=note.note_id)

    result = server.run_compliance_check()

    assert result.all_passed is True
    assert len(result.controls) == 4
    assert all(control.passed for control in result.controls)
    assert {c.control_id for c in result.controls} == {
        "REQ-006-audit-completeness",
        "REQ-014-single-decision-integrity",
        "REQ-011-security-incident-logging",
        "REQ-011-no-phi-in-structured-logs",
    }

    entries = _read_audit_entries()
    assert entries[-1]["action"] == "compliance_check"
    assert entries[-1]["all_passed"] is True


def test_run_compliance_check_on_empty_audit_trail_all_pass_vacuously():
    result = server.run_compliance_check()

    assert result.all_passed is True
    assert all(control.passed for control in result.controls)


def test_run_compliance_check_catches_an_orphaned_decision():
    _raw_append_audit_entry(
        {
            "timestamp": "2026-01-01T00:00:00+00:00",
            "action": "approve_note",
            "note_id": "no-such-note",
            "ehr_system": "epic",
            "patient_id": "epic-pt-10293847",
            "note_text": "fabricated",
            "status": "approved",
        }
    )

    result = server.run_compliance_check()

    completeness = next(c for c in result.controls if c.control_id == "REQ-006-audit-completeness")
    assert completeness.passed is False
    assert "no-such-note" in completeness.detail
    assert result.all_passed is False


def test_run_compliance_check_catches_two_decisions_on_one_note():
    note = _generate_note()
    server.approve_encounter_note(note_id=note.note_id)
    _raw_append_audit_entry(
        {
            "timestamp": "2026-01-01T00:00:00+00:00",
            "action": "reject_note",
            "note_id": note.note_id,
            "ehr_system": note.ehr_system,
            "patient_id": note.patient_id,
            "note_text": note.note_text,
            "feedback": "fabricated second decision",
            "status": "rejected",
        }
    )

    result = server.run_compliance_check()

    integrity = next(c for c in result.controls if c.control_id == "REQ-014-single-decision-integrity")
    assert integrity.passed is False
    assert note.note_id in integrity.detail
    assert result.all_passed is False


def test_run_compliance_check_catches_an_incomplete_security_incident_record():
    _raw_append_audit_entry(
        {
            "timestamp": "2026-01-01T00:00:00+00:00",
            "action": "read_transcript_file",
            "error_class": "AccessDenied",
            "security_incident": True,
            # No "outcome" field -- an incomplete security-incident record.
        }
    )

    result = server.run_compliance_check()

    logging_control = next(c for c in result.controls if c.control_id == "REQ-011-security-incident-logging")
    assert logging_control.passed is False
    assert result.all_passed is False


def test_run_compliance_check_ignores_an_access_denied_entry_not_tagged_as_an_incident():
    # error_class alone is no longer what this control keys off -- an
    # AccessDenied entry without the explicit security_incident tag isn't
    # counted at all (0 incidents observed is a legitimate pass), not
    # flagged incomplete. Confirms the tag, not the error-class string, is
    # now authoritative for what counts as a security incident.
    _raw_append_audit_entry(
        {
            "timestamp": "2026-01-01T00:00:00+00:00",
            "action": "some_future_tool",
            "error_class": "AccessDenied",
            "outcome": "failure",
        }
    )

    result = server.run_compliance_check()

    logging_control = next(c for c in result.controls if c.control_id == "REQ-011-security-incident-logging")
    assert logging_control.passed is True
    assert "0 security-incident event" in logging_control.detail


def test_run_compliance_check_recognizes_a_real_security_incident_as_complete(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    secret = tmp_path / "secret"
    secret.mkdir()
    (secret / "passwd.txt").write_text("TOP SECRET", encoding="utf-8")

    traversal_path = str(allowed / ".." / "secret" / "passwd.txt")
    denied = server.read_transcript_file(file_path=traversal_path, roots=_roots_for(allowed))
    assert denied.denied is True

    entries = _read_audit_entries()
    assert entries[0]["security_incident"] is True

    result = server.run_compliance_check()

    logging_control = next(c for c in result.controls if c.control_id == "REQ-011-security-incident-logging")
    assert logging_control.passed is True
    assert "1 security-incident event" in logging_control.detail
