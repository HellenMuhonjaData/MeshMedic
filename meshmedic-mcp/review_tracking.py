"""How a clinician handled an AI suggestion: approved as-is, edited, or entered it themselves.

Every approval of an AI-drafted note, code set, care gap or test order
carries a `review` record in its audit entry, built here. It answers the
traceability questions the audit trail must be able to answer later:

- Did the clinician change what the AI proposed, and what changed?
- How long did they spend before approving?
- Was an unedited approval fast enough that the clinician may not have read
  it (`possibly_unread`)? That is a prompt for follow-up, not proof: a
  clinician can legitimately agree with a short item quickly.

Pure functions only; server.py writes the result into the audit entry.
Timing comes from server-side audit timestamps (AI suggestion recorded ->
decision recorded), never from the browser.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

ReviewOutcome = Literal["approved_as_is", "approved_with_edits", "clinician_entered"]

# Reading-speed floor for the "possibly unread" heuristic. ~8 words/second is
# fast skimming (about 480 wpm); an unedited approval faster than that, or
# under the minimum, gets flagged for follow-up.
SKIM_WORDS_PER_SECOND = 8.0
MIN_REVIEW_SECONDS = 3.0


class ReviewRecord(BaseModel):
    source: Literal["ai", "clinician"]
    outcome: ReviewOutcome
    edited: bool
    changes: list[str]  # short, human-readable; empty when unedited
    review_seconds: float | None  # None when there was no AI suggestion to time from
    possibly_unread: bool


def _seconds_between(start_iso: str, end_iso: str) -> float | None:
    try:
        start = datetime.fromisoformat(start_iso)
        end = datetime.fromisoformat(end_iso)
    except (TypeError, ValueError):
        return None
    return max(0.0, round((end - start).total_seconds(), 1))


def min_read_seconds(word_count: int) -> float:
    return max(MIN_REVIEW_SECONDS, word_count / SKIM_WORDS_PER_SECOND)


def _normalise(text: str) -> str:
    return " ".join(text.split())


def _finish(
    changes: list[str], word_count: int, suggested_at: str, decided_at: str
) -> ReviewRecord:
    edited = bool(changes)
    seconds = _seconds_between(suggested_at, decided_at)
    possibly_unread = (
        not edited and seconds is not None and seconds < min_read_seconds(word_count)
    )
    return ReviewRecord(
        source="ai",
        outcome="approved_with_edits" if edited else "approved_as_is",
        edited=edited,
        changes=changes,
        review_seconds=seconds,
        possibly_unread=possibly_unread,
    )


def clinician_entered() -> ReviewRecord:
    """The clinician wrote this item themselves; there is no AI version to compare."""
    return ReviewRecord(
        source="clinician",
        outcome="clinician_entered",
        edited=False,
        changes=[],
        review_seconds=None,
        possibly_unread=False,
    )


def review_text(original: str, final: str, suggested_at: str, decided_at: str) -> ReviewRecord:
    """Compare an AI-drafted text (e.g. a note) with what was approved.

    Whitespace-only differences don't count as edits.
    """
    orig_words = _normalise(original).split()
    final_words = _normalise(final).split()
    changes: list[str] = []
    if orig_words != final_words:
        orig_set, final_set = set(orig_words), set(final_words)
        added = sum(1 for w in final_words if w not in orig_set)
        removed = sum(1 for w in orig_words if w not in final_set)
        changes.append(
            f"text edited: {added} word(s) added, {removed} removed "
            f"({len(orig_words)} -> {len(final_words)} words)"
        )
    return _finish(changes, len(orig_words), suggested_at, decided_at)


def review_fields(
    original: dict[str, str], final: dict[str, str], suggested_at: str, decided_at: str
) -> ReviewRecord:
    """Compare a small AI-suggested record (care gap, test order) field by field."""
    changes = [
        f"{field} changed"
        for field in original
        if _normalise(str(original.get(field) or "")) != _normalise(str(final.get(field) or ""))
    ]
    words = sum(len(str(v).split()) for v in original.values())
    return _finish(changes, words, suggested_at, decided_at)


def _code_key(code: dict) -> tuple[str, str]:
    return (code.get("code_system", ""), _normalise(code.get("code", "")).upper())


def review_codes(
    original: list[dict], final: list[dict], suggested_at: str, decided_at: str
) -> ReviewRecord:
    """Compare an AI-suggested code set with the approved one."""
    orig = {_code_key(c): c for c in original}
    fin = {_code_key(c): c for c in final}
    changes: list[str] = []
    for key in fin.keys() - orig.keys():
        changes.append(f"added {key[0]} {key[1]}")
    for key in orig.keys() - fin.keys():
        changes.append(f"removed {key[0]} {key[1]}")
    for key in orig.keys() & fin.keys():
        if _normalise(orig[key].get("description", "")) != _normalise(fin[key].get("description", "")):
            changes.append(f"description changed for {key[0]} {key[1]}")
    words = sum(len(str(c.get("description", "")).split()) + 1 for c in original)
    return _finish(sorted(changes), words, suggested_at, decided_at)
