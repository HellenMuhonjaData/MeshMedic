"""AI suggestions for an encounter that a clinician accepts one at a time.

When the AI drafts an encounter it also proposes care gaps, test orders and
a referral. None of these become clinical records on their own: they are
written to the audit trail as one `record_ai_suggestions` entry, each item
with its own `item_id`. When the clinician accepts an item (possibly after
editing it), identify_care_gaps / order_test receive that `item_id`, and the
server compares the accepted version with the original here, so the audit
trail shows exactly what the AI proposed and what the clinician changed.

Pure functions only; server.py does the audit I/O.

Idempotency: recording the same suggestions for the same note twice returns
the first entry instead of writing a duplicate (keyed on note_id + a hash of
the suggestion content).
"""

import hashlib
import json
import uuid
from typing import Annotated, Literal

from pydantic import BaseModel, Field

RECORD_ACTION = "record_ai_suggestions"

ItemKind = Literal["care_gap", "test_order", "referral"]


class TestOrderSuggestion(BaseModel):
    __test__ = False  # not a pytest test class despite the name

    test_name: Annotated[str, Field(min_length=1, max_length=200)]
    reason: Annotated[str, Field(min_length=1, max_length=500)]


class ReferralSuggestion(BaseModel):
    specialty: Annotated[str, Field(min_length=1, max_length=100)]
    reason: Annotated[str, Field(min_length=1, max_length=1000)]


class SuggestedItem(BaseModel):
    item_id: str
    kind: ItemKind
    fields: dict[str, str | float | None]


class RecordedSuggestions(BaseModel):
    note_id: str
    patient_id: str
    recorded_at: str
    items: list[SuggestedItem]


def content_key(note_id: str, care_gaps: list[dict], test_orders: list[dict], referral: dict | None) -> str:
    payload = json.dumps(
        {"note_id": note_id, "care_gaps": care_gaps, "test_orders": test_orders, "referral": referral},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_items(care_gaps: list[dict], test_orders: list[dict], referral: dict | None) -> list[dict]:
    items = [{"item_id": str(uuid.uuid4()), "kind": "care_gap", "fields": g} for g in care_gaps]
    items += [{"item_id": str(uuid.uuid4()), "kind": "test_order", "fields": o} for o in test_orders]
    if referral:
        items.append({"item_id": str(uuid.uuid4()), "kind": "referral", "fields": referral})
    return items


def find_existing(entries: list[dict], key: str) -> dict | None:
    for entry in entries:
        if entry.get("action") == RECORD_ACTION and entry.get("content_key") == key:
            return entry
    return None


def find_item(entries: list[dict], item_id: str) -> tuple[dict, dict] | None:
    """Return (record entry, item) for an item_id, or None if it was never recorded."""
    for entry in entries:
        if entry.get("action") != RECORD_ACTION:
            continue
        for item in entry.get("items", []):
            if item.get("item_id") == item_id:
                return entry, item
    return None


def already_accepted(entries: list[dict], item_id: str) -> bool:
    """True if an item was already turned into a care gap or test order."""
    return any(
        e.get("action") in ("identify_care_gap", "order_test") and e.get("suggestion_item_id") == item_id
        for e in entries
    )


def orphan_references(entries: list[dict]) -> list[str]:
    """Audit-completeness check: every accepted item must point at a recorded suggestion."""
    recorded = {
        item.get("item_id")
        for e in entries
        if e.get("action") == RECORD_ACTION
        for item in e.get("items", [])
    }
    return [
        f"{e['action']} references suggestion_item_id={e['suggestion_item_id']!r} with no {RECORD_ACTION} entry"
        for e in entries
        if e.get("suggestion_item_id") and e["suggestion_item_id"] not in recorded
    ]
