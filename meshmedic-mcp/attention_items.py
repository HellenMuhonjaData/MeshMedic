"""
Drill-down behind the dashboard's "Waiting on you now" tiles: the actual
open items in one category, who they belong to, what each one needs, and
how long it has been waiting -- oldest first, so the most overdue is on top.

Pure functions only -- server.py's get_attention_items tool reads the audit
trail, calls attention_list(), and writes a view_attention_items audit
entry. Every item is replayed from the audit trail, so this list always
agrees with the dashboard's counts (activity_dashboard.pending_work) and
with the workspace.

Unlike the dashboard's counts, these rows identify patients (name and MRN),
because the clinician needs to know whose item it is to act on it. That is
why each view is audited (ids only, no text). Rows name the item briefly
(the care gap's short description, the test's name, the letter's specialty)
so it can be triaged, but carry no note or result text: the clinician opens
the item in the workspace to see the clinical detail.
"""

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel

import referral_letters

VIEW_ACTION = "view_attention_items"

Category = Literal[
    "notes_awaiting_review",
    "open_care_gaps",
    "tests_awaiting_results",
    "letters_awaiting_approval",
    "letters_ready_to_send",
]
ItemKind = Literal["note", "gap", "order", "letter"]

NEEDS = {
    "notes_awaiting_review": "Review the draft note, then approve or reject it.",
    "open_care_gaps": "Record what was done about this care gap.",
    "tests_awaiting_results": "Record the result when it comes back.",
    "letters_awaiting_approval": "Check the letter (patient line first), then approve it.",
    "letters_ready_to_send": "Print or download it, deliver it, then mark it sent.",
}


class AttentionItem(BaseModel):
    kind: ItemKind
    item_id: str
    note_id: str
    patient_id: str
    ehr_system: str
    patient_name: str
    mrn: str | None
    summary: str
    needs: str
    waiting_since: str
    waiting_hours: float
    low_confidence: bool = False


class AttentionList(BaseModel):
    category: Category
    items: list[AttentionItem]
    count: int
    listed_at: str


def _hours_since(timestamp: str, now: datetime) -> float:
    try:
        moment = datetime.fromisoformat(timestamp)
    except (TypeError, ValueError):
        return 0.0
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return round(max(0.0, (now - moment).total_seconds() / 3600), 1)


def _open(
    entries: list[dict], opened: str, closed: tuple[str, ...], key: str
) -> list[dict]:
    """Entries of action `opened` whose `key` never got a `closed` action."""
    done = {e.get(key) for e in entries if e.get("action") in closed}
    return [e for e in entries if e.get("action") == opened and e.get(key) not in done]


def _note_items(entries: list[dict]) -> list[tuple[str, dict, str, str, bool]]:
    rows = []
    for e in _open(
        entries, "generate_note", ("approve_note", "reject_note"), "note_id"
    ):
        summary = "Draft encounter note"
        if e.get("flagged"):
            summary += f" -- low confidence ({e.get('confidence', 0):.2f})"
        rows.append(("note", e, e["note_id"], summary, bool(e.get("flagged"))))
    return rows


def _gap_items(entries: list[dict]) -> list[tuple[str, dict, str, str, bool]]:
    return [
        (
            "gap",
            e,
            e["gap_id"],
            f"Care gap: {e.get('description', '')}",
            bool(e.get("flagged")),
        )
        for e in _open(entries, "identify_care_gap", ("address_care_gap",), "gap_id")
    ]


def _order_items(entries: list[dict]) -> list[tuple[str, dict, str, str, bool]]:
    return [
        (
            "order",
            e,
            e["order_id"],
            f"Test ordered: {e.get('test_name', 'unnamed')}",
            False,
        )
        for e in _open(entries, "order_test", ("record_test_result",), "order_id")
    ]


def _letter_items(
    entries: list[dict], status: str
) -> list[tuple[str, dict, str, str, bool]]:
    rows = []
    for letter in referral_letters.list_letters(entries, status=status):
        since = letter.approved_at if status == "ready_to_send" else letter.generated_at
        source = {
            "timestamp": since,
            "note_id": letter.note_id,
            "patient_id": letter.patient_id,
            "ehr_system": letter.ehr_system,
        }
        rows.append(
            (
                "letter",
                source,
                letter.letter_id,
                f"Referral letter to {letter.specialty}",
                False,
            )
        )
    return rows


def attention_list(
    entries: list[dict],
    category: Category,
    patients: dict[tuple[str, str], dict],
    now: datetime,
) -> AttentionList:
    """`patients` maps (ehr_system, patient_id) -> sample-patient record."""
    if category == "notes_awaiting_review":
        rows = _note_items(entries)
    elif category == "open_care_gaps":
        rows = _gap_items(entries)
    elif category == "tests_awaiting_results":
        rows = _order_items(entries)
    elif category == "letters_awaiting_approval":
        rows = _letter_items(entries, "draft")
    else:
        rows = _letter_items(entries, "ready_to_send")

    items = []
    for kind, source, item_id, summary, low_confidence in rows:
        patient = patients.get((source.get("ehr_system"), source.get("patient_id")))
        name = (
            f"{patient['first_name']} {patient['last_name']}"
            if patient
            else f"Unknown patient ({source.get('patient_id')})"
        )
        items.append(
            AttentionItem(
                kind=kind,
                item_id=item_id,
                note_id=source.get("note_id") or "",
                patient_id=source.get("patient_id") or "",
                ehr_system=source.get("ehr_system") or "",
                patient_name=name,
                mrn=patient["mrn"] if patient else None,
                summary=summary,
                needs=NEEDS[category],
                waiting_since=source.get("timestamp") or "",
                waiting_hours=_hours_since(source.get("timestamp"), now),
                low_confidence=low_confidence,
            )
        )
    items.sort(key=lambda i: i.waiting_hours, reverse=True)
    return AttentionList(
        category=category,
        items=items,
        count=len(items),
        listed_at=now.astimezone(timezone.utc).isoformat(),
    )


def view_entry(listing: AttentionList) -> dict:
    """Audit entry for one drill-down view: which items were shown. Ids only."""
    return {
        "timestamp": listing.listed_at,
        "action": VIEW_ACTION,
        "category": listing.category,
        "item_ids": [i.item_id for i in listing.items],
        "patient_ids": sorted({i.patient_id for i in listing.items}),
    }
