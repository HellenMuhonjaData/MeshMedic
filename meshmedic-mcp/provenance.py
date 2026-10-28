"""
Source labeling for STORY-012 / REQ-013: tell the clinician which
information came from the encounter and which the AI generated.

Pure functions only -- no file I/O, no logging, no MCP wiring. server.py's
label_information_sources tool reads the audit trail, calls into this
module, and writes the "presented" audit entry. Same split as
referral_letters.py, for the same reasons (testable in isolation; the audit
trail stays the single source of truth).

Three labels, applied per sentence of the note and per AI-proposed item:

- encounter_supported: the sentence is grounded in what was actually said or
  recorded -- most of its content words appear in the transcript or the
  patient's chart. The best-matching transcript line comes back as evidence
  so the clinician can check it.
- ai_generated: the AI added it with no support in the encounter -- an
  inference ("Assessment: likely uncontrolled hypertension"), or a code /
  care gap the AI proposed. Approval doesn't change where it came from, so
  an approved code is still ai_generated (its review_status says approved).
- clinician_entered: a clinician added the sentence while editing (it isn't
  in the AI's original draft) and the encounter doesn't support it. Calling
  a clinician's own words AI-generated would itself be a mislabel.

The check is deterministic word overlap, not a judgement of clinical truth:
"encounter_supported" means "the encounter says this", not "this is right".
It is tuned to err towards ai_generated (the label that prompts a closer
look) except for the case the story names as a failure path: a sentence the
transcript contains word-for-word is always encounter_supported.
"""

import re
from typing import Literal

from pydantic import BaseModel

PRESENT_ACTION = "present_information_sources"

ENCOUNTER = "encounter_supported"
AI = "ai_generated"
CLINICIAN = "clinician_entered"
SourceLabel = Literal["encounter_supported", "ai_generated", "clinician_entered"]
VALID_SOURCES = frozenset({ENCOUNTER, AI, CLINICIAN})

# Share of a sentence's content words that must appear in the encounter for
# the sentence to count as encounter-supported.
SUPPORT_THRESHOLD = 0.5

_STOPWORDS = frozenset(
    (  # noqa: SIM905 -- one readable line beats 80 one-word lines
        "a an and are as at be been but by for from had has have he her his i if in "
        "into is it its me my no not of on or our she so that the their them then "
        "there they this to was we were what when which who will with you your "
        "about any some just also very"
    ).split()
)


class LabeledSentence(BaseModel):
    index: int
    text: str
    source: SourceLabel
    support_ratio: float
    evidence: str | None


class LabeledItem(BaseModel):
    item_type: Literal["code_suggestion", "care_gap"]
    item_id: str
    text: str
    source: SourceLabel
    review_status: str


class SourceLabels(BaseModel):
    note_id: str
    patient_id: str
    ehr_system: str
    note_status: Literal["draft", "approved", "rejected"]
    sentences: list[LabeledSentence]
    items: list[LabeledItem]
    counts: dict[str, int]
    labeled_at: str


def split_sentences(text: str) -> list[str]:
    """Sentences and lines of `text`, in order, none empty. Joining them back
    with spaces gives the note's words in their original order -- nothing is
    dropped, so nothing in the note can go unlabeled."""
    parts: list[str] = []
    for line in text.splitlines():
        parts.extend(re.split(r"(?<=[.!?])\s+", line.strip()))
    return [p for p in (s.strip() for s in parts) if p]


def _stem(word: str) -> str:
    return word[:-1] if len(word) > 3 and word.endswith("s") else word


def content_words(text: str) -> set[str]:
    return {
        _stem(w)
        for w in re.findall(r"[a-z0-9]+", text.lower())
        if len(w) > 1 and w not in _STOPWORDS
    }


def _normalize(text: str) -> str:
    return " ".join(text.lower().split())


def _best_evidence(sentence_words: set[str], transcript: str) -> str | None:
    """The transcript line sharing the most content words with the sentence."""
    best, best_hits = None, 0
    for line in transcript.splitlines():
        hits = len(sentence_words & content_words(line))
        if hits > best_hits:
            best, best_hits = line.strip(), hits
    return best


def chart_text(patient: dict | None) -> str:
    if not patient:
        return ""
    return " ".join(
        str(patient.get(k, ""))
        for k in ("first_name", "last_name", "date_of_birth", "mrn")
    )


def label_sentence(
    index: int,
    sentence: str,
    transcript: str,
    chart: str,
    ai_draft_sentences: set[str],
) -> LabeledSentence:
    words = content_words(sentence)
    support = words & (content_words(transcript) | content_words(chart))
    ratio = round(len(support) / len(words), 2) if words else 0.0
    verbatim = _normalize(sentence) in _normalize(transcript)
    if verbatim or ratio >= SUPPORT_THRESHOLD:
        source = ENCOUNTER
        evidence = _best_evidence(words, transcript) or "Patient chart"
        if verbatim:
            ratio = 1.0
    elif _normalize(sentence) not in ai_draft_sentences:
        source, evidence = CLINICIAN, None
    else:
        source, evidence = AI, None
    return LabeledSentence(
        index=index,
        text=sentence,
        source=source,
        support_ratio=ratio,
        evidence=evidence,
    )


def note_status_and_text(note_record: dict) -> tuple[str, str]:
    """The note as the clinician sees it now (server._find_note's shape):
    the approved text once approved, else the latest edit, else the draft."""
    decision = note_record["decision"]
    if decision is not None:
        status = "approved" if decision["action"] == "approve_note" else "rejected"
        if status == "approved":
            return status, decision["note_text"]
    else:
        status = "draft"
    if note_record["latest_edit"] is not None:
        return status, note_record["latest_edit"]["note_text"]
    return status, note_record["generated"]["note_text"]


def ai_items(entries: list[dict], note_id: str) -> list[LabeledItem]:
    """Code suggestions and care gaps the AI proposed for this note."""
    decisions: dict[str, str] = {}
    for e in entries:
        if e.get("action") == "approve_codes":
            decisions[e["suggestion_id"]] = "approved"
        elif e.get("action") == "reject_codes":
            decisions[e["suggestion_id"]] = "rejected"
        elif e.get("action") == "address_care_gap":
            decisions[e["gap_id"]] = "addressed"
    items: list[LabeledItem] = []
    for e in entries:
        if e.get("note_id") != note_id:
            continue
        if e.get("action") == "suggest_codes":
            text = "; ".join(
                f"{c['code']} ({c['code_system']}) {c['description']}"
                for c in e["codes"]
            )
            item_id, item_type = e["suggestion_id"], "code_suggestion"
        elif e.get("action") == "identify_care_gap":
            text, item_id, item_type = e["description"], e["gap_id"], "care_gap"
        else:
            continue
        items.append(
            LabeledItem(
                item_type=item_type,
                item_id=item_id,
                text=text,
                source=AI,
                review_status=decisions.get(item_id, e["status"]),
            )
        )
    return items


def label_encounter(
    note_record: dict, patient: dict | None, entries: list[dict], labeled_at: str
) -> SourceLabels:
    generated = note_record["generated"]
    status, text = note_status_and_text(note_record)
    transcript = generated["transcript"]
    chart = chart_text(patient)
    ai_draft = {_normalize(s) for s in split_sentences(generated["note_text"])}
    sentences = [
        label_sentence(i, s, transcript, chart, ai_draft)
        for i, s in enumerate(split_sentences(text))
    ]
    items = ai_items(entries, generated["note_id"])
    counts = {label: 0 for label in (ENCOUNTER, AI, CLINICIAN)}
    for labeled in [*sentences, *items]:
        counts[labeled.source] += 1
    return SourceLabels(
        note_id=generated["note_id"],
        patient_id=generated["patient_id"],
        ehr_system=generated["ehr_system"],
        note_status=status,
        sentences=sentences,
        items=items,
        counts=counts,
        labeled_at=labeled_at,
    )


def present_entry(labels: SourceLabels) -> dict:
    """Audit entry recording the source of every piece of information shown.
    Labels and ids only -- the text itself is already in the audit trail."""
    sources = [
        {"item_type": "note_sentence", "index": s.index, "source": s.source}
        for s in labels.sentences
    ] + [
        {"item_type": i.item_type, "item_id": i.item_id, "source": i.source}
        for i in labels.items
    ]
    return {
        "timestamp": labels.labeled_at,
        "action": PRESENT_ACTION,
        "note_id": labels.note_id,
        "ehr_system": labels.ehr_system,
        "patient_id": labels.patient_id,
        "note_status": labels.note_status,
        "sources": sources,
        "counts": labels.counts,
    }


def present_entry_problems(entry: dict) -> list[str]:
    """Why a presented-sources audit entry is incomplete, if it is."""
    sources = entry.get("sources")
    if not sources:
        return ["records no sources"]
    bad = [s for s in sources if s.get("source") not in VALID_SOURCES]
    return [f"has {len(bad)} item(s) with no valid source label"] if bad else []
