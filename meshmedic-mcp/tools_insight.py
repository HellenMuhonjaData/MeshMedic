"""Transparency tools: citations, source labels, activity summary, attention items.

Split out of server.py (Phase 0, no behaviour change).
"""

import re
from datetime import datetime, timezone
from typing import Annotated

from mcp.server.mcpserver.exceptions import ResourceNotFoundError
from pydantic import Field

import activity_dashboard
import attention_items
import provenance
from app_core import REVIEW_TIME_TARGET_SECONDS, _append_audit_entry, _logger, mcp
from audit_queries import _all_audit_entries, _find_note, _find_patient
from logging_utils import (
    TOOL_COMPLETED,
    TOOL_ERROR,
    TOOL_STARTED,
    log_event,
    new_correlation_id,
)
from models import CitationResult
from sample_patients import SAMPLE_PATIENTS


def _find_citation(
    transcript: str, claimed_excerpt: str
) -> tuple[int, int, str] | None:
    """Locate claimed_excerpt in transcript, tolerant of whitespace/case
    differences (line breaks, capitalization) but NOT of paraphrasing or
    punctuation changes -- a real match, not a fuzzy guess. Returns
    (start, end, matched_text) in the transcript's own coordinates -- the
    offsets a real UI would use to highlight -- or None if no match."""
    normalized_excerpt = " ".join(claimed_excerpt.split())
    if not normalized_excerpt:
        return None
    pattern = re.escape(normalized_excerpt).replace(r"\ ", r"\s+")
    match = re.search(pattern, transcript, re.IGNORECASE)
    if match is None:
        return None
    return match.start(), match.end(), transcript[match.start() : match.end()]


@mcp.tool()
def request_citation(
    note_id: Annotated[str, Field(min_length=1)],
    claimed_excerpt: Annotated[str, Field(min_length=1, max_length=1000)],
) -> CitationResult:
    """
    Verify a claimed source citation for an AI recommendation (a note, a
    code suggestion, a care gap -- anything drafted from this note's
    transcript) against the actual encounter transcript (REQ-007), logging
    every request and its outcome regardless of result (REQ-006). Call this
    only for a note_id returned by generate_encounter_note.

    You (the calling model) supply `claimed_excerpt` -- the specific text
    you believe supports a recommendation. This tool checks whether that
    text genuinely appears in the transcript (tolerant of whitespace/case
    differences but not paraphrasing or punctuation changes) and returns the
    matched text plus its character offsets in the transcript if found --
    those offsets are what a real UI would use to highlight the source
    ("highlights the relevant transcript section"). If not found, `found` is
    false with an explanation, rather than silently failing.

    Important limitation, stated plainly: this proves the excerpt is not
    fabricated -- it genuinely appears in the transcript -- not that it is
    definitively the true reason behind the recommendation. There is no
    semantic-grounding system in this build; a real-but-unrelated excerpt
    would still pass this check.
    """
    correlation_id = new_correlation_id()
    log_event(
        _logger,
        "info",
        TOOL_STARTED,
        correlation_id,
        tool="request_citation",
        note_id=note_id,
    )

    note_record = _find_note(note_id)
    if note_record is None:
        log_event(
            _logger,
            "warning",
            TOOL_ERROR,
            correlation_id,
            tool="request_citation",
            error_class="ResourceNotFoundError",
        )
        raise ResourceNotFoundError(
            f"No generated note found for note_id={note_id!r}; confirm the note "
            "exists (generate_encounter_note) before requesting a citation for it."
        )

    generated = note_record["generated"]
    transcript = generated["transcript"]
    requested_at = datetime.now(timezone.utc).isoformat()

    match = _find_citation(transcript, claimed_excerpt)
    if match is None:
        found = False
        matched_text = None
        start_offset = None
        end_offset = None
        explanation = (
            "This text does not appear verbatim in the encounter transcript "
            "(checked case- and whitespace-insensitively). It may be "
            "paraphrased, drawn from outside the transcript, or fabricated."
        )
    else:
        start_offset, end_offset, matched_text = match
        found = True
        explanation = None

    _append_audit_entry(
        {
            "timestamp": requested_at,
            "action": "request_citation",
            "note_id": note_id,
            "ehr_system": generated["ehr_system"],
            "patient_id": generated["patient_id"],
            "claimed_excerpt": claimed_excerpt,
            "found": found,
            "matched_text": matched_text,
            "start_offset": start_offset,
            "end_offset": end_offset,
        }
    )
    log_event(
        _logger,
        "info",
        TOOL_COMPLETED,
        correlation_id,
        tool="request_citation",
        note_id=note_id,
        outcome="success",
        found=found,
    )

    return CitationResult(
        note_id=note_id,
        patient_id=generated["patient_id"],
        ehr_system=generated["ehr_system"],
        claimed_excerpt=claimed_excerpt,
        found=found,
        matched_text=matched_text,
        start_offset=start_offset,
        end_offset=end_offset,
        explanation=explanation,
        requested_at=requested_at,
    )


@mcp.tool()
def label_information_sources(
    note_id: Annotated[str, Field(min_length=1)],
) -> provenance.SourceLabels:
    """
    Label where each piece of an encounter's information came from, so the
    clinician can tell what the encounter supports from what the AI added
    (REQ-013 / STORY-012). Call this whenever a note, its codes or its care
    gaps are shown to the clinician, and show the labels alongside them.

    Each sentence of the note (as it stands now: approved text, latest edit,
    or the AI draft) is labeled encounter_supported (most of its content
    words appear in the transcript or chart; the best-matching transcript
    line is returned as `evidence`), ai_generated (no such support -- e.g.
    an assessment the AI inferred), or clinician_entered (added by a
    clinician while editing, not in the AI's draft). Code suggestions and
    care gaps for the note are always ai_generated -- approval is reported
    in `review_status`, it does not change where an item came from.

    Every call writes one audit entry recording the source label of every
    item presented (labels and ids only, no text). Each call is its own
    presentation, so two calls write two entries, like an access log. If
    that write fails, this raises ToolError and returns no labels: nothing
    is presented without its source being logged.
    """
    correlation_id = new_correlation_id()
    log_event(
        _logger,
        "info",
        TOOL_STARTED,
        correlation_id,
        tool="label_information_sources",
        note_id=note_id,
    )
    record = _find_note(note_id)
    if record is None:
        log_event(
            _logger,
            "warning",
            TOOL_ERROR,
            correlation_id,
            tool="label_information_sources",
            error_class="ResourceNotFoundError",
        )
        raise ResourceNotFoundError(f"No encounter note found for note_id={note_id!r}")
    generated = record["generated"]
    labels = provenance.label_encounter(
        record,
        _find_patient(generated["ehr_system"], generated["patient_id"]),
        _all_audit_entries(),
        datetime.now(timezone.utc).isoformat(),
    )
    _append_audit_entry(provenance.present_entry(labels))
    log_event(
        _logger,
        "info",
        TOOL_COMPLETED,
        correlation_id,
        tool="label_information_sources",
        outcome="success",
        note_id=note_id,
        encounter_supported=labels.counts[provenance.ENCOUNTER],
        ai_generated=labels.counts[provenance.AI],
        clinician_entered=labels.counts[provenance.CLINICIAN],
    )
    return labels


@mcp.tool()
def get_activity_summary(
    period: activity_dashboard.Period = "day",
    count: Annotated[int | None, Field(ge=1, le=activity_dashboard.MAX_COUNT)] = None,
    utc_offset_minutes: Annotated[int, Field(ge=-840, le=840)] = 0,
) -> activity_dashboard.ActivitySummary:
    """
    Clinician activity dashboard: counts of what was done per day, week
    (Monday start) or month, plus what is waiting on the clinician right now
    (notes awaiting review, open care gaps, tests awaiting results, letters
    awaiting approval or ready to send).

    `count` is how many periods to return, ending with the current one
    (defaults: 14 days, 12 weeks, 12 months). `utc_offset_minutes` is the
    clinician's local offset from UTC (e.g. 180 for UTC+3) so "today" and
    "this week" match their clock -- the audit trail is stored in UTC.

    Read-only and counts only: it writes no audit entry and returns no note
    text or patient identities. Everything is replayed from the audit trail,
    so the dashboard always agrees with it. Entries with an unreadable
    timestamp are skipped and reported in `skipped_entries`.
    """
    correlation_id = new_correlation_id()
    log_event(
        _logger,
        "info",
        TOOL_STARTED,
        correlation_id,
        tool="get_activity_summary",
        period=period,
    )
    summary = activity_dashboard.summarize(
        _all_audit_entries(),
        period,
        count or activity_dashboard.DEFAULT_COUNTS[period],
        datetime.now(timezone.utc),
        review_target_seconds=REVIEW_TIME_TARGET_SECONDS,
        utc_offset_minutes=utc_offset_minutes,
    )
    log_event(
        _logger,
        "info",
        TOOL_COMPLETED,
        correlation_id,
        tool="get_activity_summary",
        outcome="success",
        bucket_count=len(summary.buckets),
        skipped_entries=summary.skipped_entries,
    )
    return summary


@mcp.tool()
def get_attention_items(
    category: attention_items.Category,
) -> attention_items.AttentionList:
    """
    Drill-down for one "waiting on you now" count on the activity dashboard:
    the open items in `category`, oldest first, each with the patient's name
    and MRN, what the item is, what the clinician needs to do, and how long
    it has been waiting. Open each item in the workspace (by patient_id plus
    item_id / note_id) to see its clinical detail and act on it -- rows name
    the item briefly (gap description, test name, letter specialty) but
    carry no note or result text.

    Because the rows identify patients, each call writes a
    view_attention_items audit entry (category and ids only). If that write
    fails, this raises ToolError and returns nothing.
    """
    correlation_id = new_correlation_id()
    log_event(
        _logger,
        "info",
        TOOL_STARTED,
        correlation_id,
        tool="get_attention_items",
        category=category,
    )
    listing = attention_items.attention_list(
        _all_audit_entries(),
        category,
        {(p["ehr_system"], p["patient_id"]): p for p in SAMPLE_PATIENTS},
        datetime.now(timezone.utc),
    )
    _append_audit_entry(attention_items.view_entry(listing))
    log_event(
        _logger,
        "info",
        TOOL_COMPLETED,
        correlation_id,
        tool="get_attention_items",
        outcome="success",
        count=listing.count,
    )
    return listing
