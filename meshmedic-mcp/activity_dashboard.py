"""
Clinician activity dashboard: what happened per day, week or month, and
what is waiting on the clinician right now.

Pure functions only -- server.py's get_activity_summary tool reads the
audit trail and calls summarize(). Like referral_letters.py and
provenance.py, everything is reconstructed by replaying audit entries, so
the dashboard can never disagree with the audit trail.

Counts only: no note text, patient names or other PHI leave this module,
just numbers per period.

Scope note: this build has one clinician per local audit log (see
docs/TRANSPORT_DECISION.md) and most entries carry no clinician name, so
"activity" means everything recorded in this log, not a per-person split.

Failure modes handled: entries with a missing or unparseable timestamp are
skipped and counted in `skipped_entries` (never silently dropped); periods
with no activity still appear, as zeros, so gaps in work stay visible.
"""

from datetime import date, datetime, timedelta, timezone
from typing import Literal

from pydantic import BaseModel

import referral_letters

Period = Literal["day", "week", "month"]
DEFAULT_COUNTS = {"day": 14, "week": 12, "month": 12}
MAX_COUNT = 90

# audit action -> counter it increments
_ACTION_COUNTERS = {
    "search_ehr_patient": "patient_searches",
    "generate_note": "notes_drafted",
    "approve_note": "notes_approved",
    "reject_note": "notes_rejected",
    "suggest_codes": "code_sets_suggested",
    "approve_codes": "code_sets_approved",
    "reject_codes": "code_sets_rejected",
    "identify_care_gap": "care_gaps_flagged",
    "address_care_gap": "care_gaps_addressed",
    "order_test": "tests_ordered",
    "record_test_result": "tests_resulted",
    referral_letters.GENERATE_ACTION: "letters_prepared",
    referral_letters.APPROVE_ACTION: "letters_approved",
    referral_letters.SEND_ACTION: "letters_sent",
    referral_letters.VOID_ACTION: "letters_voided",
}


class ActivityMetrics(BaseModel):
    patients_seen: int = 0
    patient_searches: int = 0
    notes_drafted: int = 0
    notes_flagged_low_confidence: int = 0
    notes_approved: int = 0
    notes_rejected: int = 0
    avg_documentation_seconds: float | None = None
    notes_over_time_target: int = 0
    code_sets_suggested: int = 0
    code_sets_approved: int = 0
    code_sets_rejected: int = 0
    care_gaps_flagged: int = 0
    care_gaps_addressed: int = 0
    tests_ordered: int = 0
    tests_resulted: int = 0
    letters_prepared: int = 0
    letters_approved: int = 0
    letters_sent: int = 0
    letters_voided: int = 0


class ActivityBucket(BaseModel):
    label: str
    start: str  # local date, inclusive
    end: str  # local date, exclusive
    metrics: ActivityMetrics


class PendingWork(BaseModel):
    notes_awaiting_review: int
    open_care_gaps: int
    tests_awaiting_results: int
    letters_awaiting_approval: int
    letters_ready_to_send: int


class ActivitySummary(BaseModel):
    period: Period
    utc_offset_minutes: int
    buckets: list[ActivityBucket]  # oldest first; the last is the current period
    totals: ActivityMetrics
    pending: PendingWork
    skipped_entries: int
    generated_at: str


def _local_date(timestamp: str, offset: timedelta) -> date | None:
    try:
        moment = datetime.fromisoformat(timestamp)
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (moment.astimezone(timezone.utc) + offset).date()


def period_start(day: date, period: Period) -> date:
    if period == "day":
        return day
    if period == "week":
        return day - timedelta(days=day.weekday())  # Monday
    return day.replace(day=1)


def _next_start(start: date, period: Period) -> date:
    if period == "day":
        return start + timedelta(days=1)
    if period == "week":
        return start + timedelta(days=7)
    year, month = (
        (start.year + 1, 1) if start.month == 12 else (start.year, start.month + 1)
    )
    return date(year, month, 1)


def _previous_start(start: date, period: Period) -> date:
    if period == "day":
        return start - timedelta(days=1)
    if period == "week":
        return start - timedelta(days=7)
    year, month = (
        (start.year - 1, 12) if start.month == 1 else (start.year, start.month - 1)
    )
    return date(year, month, 1)


def bucket_label(start: date, period: Period) -> str:
    if period == "day":
        return start.strftime("%a %d %b")
    if period == "week":
        return "Week of " + start.strftime("%d %b")
    return start.strftime("%b %Y")


def bucket_starts(today: date, period: Period, count: int) -> list[date]:
    """The `count` most recent period starts ending with today's, oldest first."""
    starts = [period_start(today, period)]
    while len(starts) < count:
        starts.append(_previous_start(starts[-1], period))
    return starts[::-1]


class _Accumulator:
    def __init__(self, review_target_seconds: float) -> None:
        self.review_target_seconds = review_target_seconds
        self.counts: dict[str, int] = {}
        self.patients: set[str] = set()
        self.doc_times: list[float] = []

    def add(self, entry: dict) -> None:
        action = entry.get("action")
        counter = _ACTION_COUNTERS.get(action)
        if counter:
            self.counts[counter] = self.counts.get(counter, 0) + 1
        if action == "generate_note":
            if entry.get("patient_id"):
                self.patients.add(entry["patient_id"])
            if entry.get("flagged"):
                self._bump("notes_flagged_low_confidence")
        if action in ("approve_note", "reject_note"):
            seconds = entry.get("documentation_time_seconds")
            if isinstance(seconds, (int, float)):
                self.doc_times.append(float(seconds))
                if seconds > self.review_target_seconds:
                    self._bump("notes_over_time_target")

    def _bump(self, key: str) -> None:
        self.counts[key] = self.counts.get(key, 0) + 1

    def metrics(self) -> ActivityMetrics:
        avg = (
            round(sum(self.doc_times) / len(self.doc_times), 1)
            if self.doc_times
            else None
        )
        return ActivityMetrics(
            patients_seen=len(self.patients),
            avg_documentation_seconds=avg,
            **self.counts,
        )


def pending_work(entries: list[dict]) -> PendingWork:
    """What is open right now, regardless of period."""
    notes, decided = set(), set()
    gaps, addressed = set(), set()
    orders, resulted = set(), set()
    for e in entries:
        action = e.get("action")
        if action == "generate_note":
            notes.add(e.get("note_id"))
        elif action in ("approve_note", "reject_note"):
            decided.add(e.get("note_id"))
        elif action == "identify_care_gap":
            gaps.add(e.get("gap_id"))
        elif action == "address_care_gap":
            addressed.add(e.get("gap_id"))
        elif action == "order_test":
            orders.add(e.get("order_id"))
        elif action == "record_test_result":
            resulted.add(e.get("order_id"))
    for ids in (notes, decided, gaps, addressed, orders, resulted):
        ids.discard(None)
    return PendingWork(
        notes_awaiting_review=len(notes - decided),
        open_care_gaps=len(gaps - addressed),
        tests_awaiting_results=len(orders - resulted),
        letters_awaiting_approval=len(
            referral_letters.list_letters(entries, status="draft")
        ),
        letters_ready_to_send=len(
            referral_letters.list_letters(entries, status="ready_to_send")
        ),
    )


def summarize(
    entries: list[dict],
    period: Period,
    count: int,
    now: datetime,
    review_target_seconds: float,
    utc_offset_minutes: int = 0,
) -> ActivitySummary:
    offset = timedelta(minutes=utc_offset_minutes)
    today = (now.astimezone(timezone.utc) + offset).date()
    starts = bucket_starts(today, period, count)
    accumulators = {start: _Accumulator(review_target_seconds) for start in starts}
    totals = _Accumulator(review_target_seconds)
    skipped = 0
    for entry in entries:
        day = _local_date(entry.get("timestamp"), offset)
        if day is None:
            skipped += 1
            continue
        acc = accumulators.get(period_start(day, period))
        if acc is not None:
            acc.add(entry)
            totals.add(entry)
    buckets = [
        ActivityBucket(
            label=bucket_label(start, period),
            start=start.isoformat(),
            end=_next_start(start, period).isoformat(),
            metrics=accumulators[start].metrics(),
        )
        for start in starts
    ]
    return ActivitySummary(
        period=period,
        utc_offset_minutes=utc_offset_minutes,
        buckets=buckets,
        totals=totals.metrics(),
        pending=pending_work(entries),
        skipped_entries=skipped,
        generated_at=now.astimezone(timezone.utc).isoformat(),
    )
