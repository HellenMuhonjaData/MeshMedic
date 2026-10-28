"""Clinician activity dashboard: day / week / month activity and pending work."""

import json
from datetime import date, datetime, timezone

import pytest
from pydantic import TypeAdapter, ValidationError

import activity_dashboard as dash
import server

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)  # a Wednesday


@pytest.fixture(autouse=True)
def isolated_audit_log(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "AUDIT_LOG_PATH", tmp_path / "audit_log.jsonl")


def _e(ts, action, **extra):
    return {"timestamp": ts, "action": action, **extra}


def _summarize(entries, period="day", count=3, offset=0):
    return dash.summarize(
        entries,
        period,
        count,
        NOW,
        review_target_seconds=120.0,
        utc_offset_minutes=offset,
    )


# --- Bucketing -----------------------------------------------------------------


def test_days_count_activity_into_the_right_day_and_keep_empty_days():
    entries = [
        _e("2026-10-28T09:00:00+00:00", "generate_note", note_id="n1", patient_id="p1"),
        _e("2026-10-28T10:00:00+00:00", "generate_note", note_id="n2", patient_id="p1"),
        _e("2026-10-26T10:00:00+00:00", "generate_note", note_id="n3", patient_id="p2"),
    ]
    s = _summarize(entries)
    assert [b.start for b in s.buckets] == ["2026-10-26", "2026-10-27", "2026-10-28"]
    assert [b.metrics.notes_drafted for b in s.buckets] == [1, 0, 2]
    assert s.buckets[-1].metrics.patients_seen == 1  # distinct patients
    assert s.totals.notes_drafted == 3
    assert s.totals.patients_seen == 2


def test_weeks_start_on_monday():
    s = _summarize([], period="week", count=2)
    assert [b.start for b in s.buckets] == ["2026-10-19", "2026-10-26"]
    assert s.buckets[-1].end == "2026-11-02"
    assert s.buckets[-1].label == "Week of 26 Oct"


def test_months_cross_a_year_boundary():
    starts = dash.bucket_starts(date(2026, 2, 10), "month", 4)
    assert [d.isoformat() for d in starts] == [
        "2025-11-01",
        "2025-12-01",
        "2026-01-01",
        "2026-02-01",
    ]
    s = _summarize([], period="month", count=1)
    assert s.buckets[0].label == "Oct 2026"
    assert s.buckets[0].end == "2026-11-01"


def test_local_offset_moves_late_evening_utc_into_the_next_local_day():
    # 22:30 UTC on the 27th is 01:30 on the 28th in UTC+3.
    entries = [_e("2026-10-27T22:30:00+00:00", "approve_note", note_id="n1")]
    utc = _summarize(entries)
    local = _summarize(entries, offset=180)
    assert [b.metrics.notes_approved for b in utc.buckets] == [0, 1, 0]
    assert [b.metrics.notes_approved for b in local.buckets] == [0, 0, 1]


def test_entries_older_than_the_window_are_left_out_of_totals():
    entries = [
        _e("2026-01-01T09:00:00+00:00", "generate_note", note_id="n", patient_id="p")
    ]
    s = _summarize(entries)
    assert s.totals.notes_drafted == 0


# --- Metrics -------------------------------------------------------------------


def test_documentation_time_average_and_over_target_count():
    entries = [
        _e(
            "2026-10-28T09:00:00+00:00",
            "approve_note",
            note_id="a",
            documentation_time_seconds=60,
        ),
        _e(
            "2026-10-28T09:05:00+00:00",
            "reject_note",
            note_id="b",
            documentation_time_seconds=180,
        ),
        _e(
            "2026-10-28T09:06:00+00:00",
            "generate_note",
            note_id="c",
            patient_id="p",
            flagged=True,
        ),
    ]
    m = _summarize(entries).buckets[-1].metrics
    assert m.avg_documentation_seconds == 120.0
    assert m.notes_over_time_target == 1
    assert (m.notes_approved, m.notes_rejected) == (1, 1)
    assert m.notes_flagged_low_confidence == 1


def test_no_decisions_means_no_average_rather_than_zero():
    assert _summarize([]).buckets[-1].metrics.avg_documentation_seconds is None


def test_every_tracked_action_increments_its_counter():
    acc = dash._Accumulator(review_target_seconds=120.0)
    for action in dash._ACTION_COUNTERS:
        acc.add(_e("2026-10-28T09:00:00+00:00", action))
    m = acc.metrics().model_dump()
    for counter in dash._ACTION_COUNTERS.values():
        assert m[counter] == 1, counter


def test_untracked_actions_are_ignored():
    entries = [_e("2026-10-28T09:00:00+00:00", "compliance_check")]
    m = _summarize(entries).buckets[-1].metrics
    assert m == dash.ActivityMetrics()


# --- Pending work --------------------------------------------------------------


def test_pending_counts_only_items_still_open():
    ts = "2026-10-20T09:00:00+00:00"  # outside the day window: pending ignores periods
    entries = [
        _e(ts, "generate_note", note_id="n1", patient_id="p"),
        _e(ts, "generate_note", note_id="n2", patient_id="p"),
        _e(ts, "approve_note", note_id="n2"),
        _e(ts, "identify_care_gap", gap_id="g1"),
        _e(ts, "identify_care_gap", gap_id="g2"),
        _e(ts, "address_care_gap", gap_id="g2"),
        _e(ts, "order_test", order_id="o1"),
    ]
    p = _summarize(entries).pending
    assert p.notes_awaiting_review == 1
    assert p.open_care_gaps == 1
    assert p.tests_awaiting_results == 1
    assert p.letters_ready_to_send == 0


def test_pending_letters_through_the_real_tools():
    note = server.generate_encounter_note(
        ehr_system="epic",
        patient_id="epic-pt-10293847",
        transcript="Chest tightness on exertion.",
        note_text="Exertional chest tightness.",
        confidence=0.9,
    )
    server.approve_encounter_note(note_id=note.note_id)
    draft = server.generate_referral_letter(
        note_id=note.note_id,
        specialty="Cardiology",
        referral_reason="Evaluate chest tightness.",
        referring_clinician="Dr. A",
    )
    ready = server.generate_referral_letter(
        note_id=note.note_id,
        specialty="Neurology",
        referral_reason="Headaches.",
        referring_clinician="Dr. A",
    )
    server.approve_referral_letter(letter_id=ready.letter_id, approved_by="Dr. A")
    s = server.get_activity_summary(period="day", count=1)
    assert s.pending.letters_awaiting_approval == 1
    assert s.pending.letters_ready_to_send == 1
    assert draft.status == "draft"
    assert s.buckets[-1].metrics.letters_prepared == 2
    assert s.buckets[-1].metrics.notes_approved == 1


# --- Failure paths and boundaries ----------------------------------------------


def test_bad_timestamps_are_skipped_and_reported():
    entries = [
        _e("not a date", "generate_note", note_id="x", patient_id="p"),
        {"action": "generate_note", "note_id": "y", "patient_id": "p"},
        _e("2026-10-28T09:00:00+00:00", "generate_note", note_id="z", patient_id="p"),
    ]
    s = _summarize(entries)
    assert s.skipped_entries == 2
    assert s.totals.notes_drafted == 1


def test_naive_timestamps_are_treated_as_utc():
    entries = [_e("2026-10-28T09:00:00", "generate_note", note_id="z", patient_id="p")]
    assert _summarize(entries).buckets[-1].metrics.notes_drafted == 1


def test_empty_audit_log_gives_zeros_not_an_error():
    s = server.get_activity_summary(period="month")
    assert len(s.buckets) == dash.DEFAULT_COUNTS["month"]
    assert s.totals == dash.ActivityMetrics()
    assert s.skipped_entries == 0


def test_count_and_offset_contracts():
    adapter = TypeAdapter(dash.Period)
    with pytest.raises(ValidationError):
        adapter.validate_python("year")
    assert (
        len(server.get_activity_summary(period="day", count=dash.MAX_COUNT).buckets)
        == 90
    )


def test_dashboard_is_read_only_and_returns_no_text():
    server.generate_encounter_note(
        ehr_system="epic",
        patient_id="epic-pt-10293847",
        transcript="Secret transcript words.",
        note_text="Secret note words.",
        confidence=0.9,
    )
    before = server.AUDIT_LOG_PATH.read_text(encoding="utf-8")
    first = server.get_activity_summary(period="week")
    second = server.get_activity_summary(period="week")
    assert server.AUDIT_LOG_PATH.read_text(encoding="utf-8") == before
    flat = json.dumps(first.model_dump())
    assert "Secret" not in flat and "epic-pt-10293847" not in flat
    assert first.model_dump(exclude={"generated_at"}) == second.model_dump(
        exclude={"generated_at"}
    )
