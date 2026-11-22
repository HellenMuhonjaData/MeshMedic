"""One status vocabulary for every reviewable item, derived from the audit trail.

Notes, code sets, care gaps, AI suggestions, test orders and referral
letters all report their state with the same words, so the workspace can
show one consistent badge. Status is never stored: it is recomputed by
replaying audit entries, so it cannot drift from what actually happened.

Lifecycle (not every item uses every step):
    drafted / ai_suggested -> clinician_reviewed -> approved | rejected | dismissed
    -> signed | authorized -> completed            (voided: a letter withdrawn)

Phase 0 maps today's actions onto this vocabulary. Later phases add the
steps they introduce ("signed" in Phase 1, "authorized" in Phase 5,
"dismissed" when dismissals are audited).
"""

from typing import Literal

import referral_letters

Status = Literal[
    "drafted",
    "ai_suggested",
    "clinician_reviewed",
    "approved",
    "rejected",
    "dismissed",
    "signed",
    "authorized",
    "completed",
    "voided",
]
ItemKind = Literal["note", "code_set", "care_gap", "suggestion", "test_order", "referral_letter"]

# audit action -> (item kind, id field, resulting status)
_TRANSITIONS: dict[str, tuple[ItemKind, str, Status]] = {
    "generate_note": ("note", "note_id", "ai_suggested"),
    "edit_note": ("note", "note_id", "clinician_reviewed"),
    "approve_note": ("note", "note_id", "approved"),  # legacy; Phase 1 reviews carry stage=reviewed
    "sign_note": ("note", "note_id", "signed"),
    "reject_note": ("note", "note_id", "rejected"),
    "suggest_codes": ("code_set", "suggestion_id", "ai_suggested"),
    "approve_codes": ("code_set", "suggestion_id", "approved"),
    "reject_codes": ("code_set", "suggestion_id", "rejected"),
    "identify_care_gap": ("care_gap", "gap_id", "approved"),
    "address_care_gap": ("care_gap", "gap_id", "completed"),
    "order_test": ("test_order", "order_id", "approved"),
    "record_test_result": ("test_order", "order_id", "completed"),
    referral_letters.GENERATE_ACTION: ("referral_letter", "letter_id", "drafted"),
    referral_letters.APPROVE_ACTION: ("referral_letter", "letter_id", "approved"),
    referral_letters.SEND_ACTION: ("referral_letter", "letter_id", "completed"),
    referral_letters.VOID_ACTION: ("referral_letter", "letter_id", "voided"),
}


def derive_statuses(entries: list[dict]) -> dict[tuple[str, str], Status]:
    """Current status of every item, keyed by (kind, id). Later entries win."""
    statuses: dict[tuple[str, str], Status] = {}
    for entry in entries:
        action = entry.get("action")
        if action == "record_ai_suggestions":
            for item in entry.get("items", []):
                statuses.setdefault(("suggestion", item["item_id"]), "ai_suggested")
            continue
        transition = _TRANSITIONS.get(action)
        if transition is not None:
            kind, id_field, status = transition
            item_id = entry.get(id_field)
            if item_id:
                if action == "approve_note" and entry.get("stage") == "reviewed":
                    status = "clinician_reviewed"
                statuses[(kind, item_id)] = status
        # Accepting an AI suggestion moves that suggestion to approved.
        if action in ("identify_care_gap", "order_test") and entry.get("suggestion_item_id"):
            statuses[("suggestion", entry["suggestion_item_id"])] = "approved"
    return statuses
