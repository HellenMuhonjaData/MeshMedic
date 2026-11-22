"""Encounters rebuilt from the audit trail (Phase 2, STORY-015).

An encounter is one patient visit. Everything produced for it -- the note,
code set, AI suggestions, care gaps and orders -- is filed under its
encounter_id, so work for different patients can never mix. Pure functions
over audit entries; tools_encounters.py exposes them as MCP tools.
"""

import item_status

START_ACTION = "start_encounter"
VIEW_ACTION = "view_encounter"


def find_encounter(entries: list[dict], encounter_id: str) -> dict | None:
    return next((e for e in entries if e.get("action") == START_ACTION and e.get("encounter_id") == encounter_id), None)


def find_by_idempotency_key(entries: list[dict], action: str, key: str | None) -> dict | None:
    if not key:
        return None
    return next((e for e in entries if e.get("action") == action and e.get("idempotency_key") == key), None)


def latest_note_entry(entries: list[dict], encounter_id: str) -> dict | None:
    notes = [e for e in entries if e.get("action") == "generate_note" and e.get("encounter_id") == encounter_id]
    return notes[-1] if notes else None


def _note_view(entries: list[dict], generated: dict) -> dict:
    note_id = generated["note_id"]
    edits = [e for e in entries if e.get("action") == "edit_note" and e.get("note_id") == note_id]
    decision = next((e for e in reversed(entries)
                     if e.get("action") in ("approve_note", "reject_note") and e.get("note_id") == note_id), None)
    signature = next((e for e in entries if e.get("action") == "sign_note" and e.get("note_id") == note_id), None)
    current = decision["note_text"] if decision else (edits[-1]["note_text"] if edits else generated["note_text"])
    # Status as the review queue needs it: editing is work in progress, not review.
    if signature is not None:
        status = "signed"
    elif decision is None:
        status = "in_review" if edits else "ai_suggested"
    elif decision["action"] == "reject_note":
        status = "rejected"
    elif decision.get("stage") == "reviewed":
        status = "clinician_reviewed"
    else:
        status = "approved_legacy_unsigned"
    return {
        "note_id": note_id,
        "status": status,
        "text": current,
        "original_text": generated["note_text"],
        "confidence": generated.get("confidence"),
        "flagged": bool(generated.get("flagged")),
        "confidence_reason": generated.get("confidence_reason"),
        "review": decision.get("review") if decision else None,
        "signed_by": (signature or {}).get("actor", {}).get("display_name"),
        "signed_at": (signature or {}).get("timestamp"),
    }


def bundle(entries: list[dict], encounter_id: str) -> dict | None:
    """Everything a reviewer needs for one encounter, in its current state."""
    encounter = find_encounter(entries, encounter_id)
    if encounter is None:
        return None
    statuses = item_status.derive_statuses(entries)
    generated = latest_note_entry(entries, encounter_id)
    result = {"encounter": encounter, "note": None, "code_set": None, "suggestions": [], "care_gaps": [], "orders": []}
    if generated is None:
        return result
    note_id = generated["note_id"]
    result["note"] = _note_view(entries, generated)

    code_sets = [e for e in entries if e.get("action") == "suggest_codes" and e.get("note_id") == note_id]
    if code_sets:
        latest = code_sets[-1]
        decided = next((e for e in reversed(entries) if e.get("action") in ("approve_codes", "reject_codes")
                        and e.get("suggestion_id") == latest["suggestion_id"]), None)
        result["code_set"] = {
            "suggestion_id": latest["suggestion_id"],
            "status": statuses.get(("code_set", latest["suggestion_id"])),
            "codes": (decided or latest)["codes"] if (decided or {}).get("action") != "reject_codes" else latest["codes"],
            "confidence": latest.get("confidence"),
            "flagged": bool(latest.get("flagged")),
            "confidence_reason": latest.get("confidence_reason"),
        }
    for record in (e for e in entries if e.get("action") == "record_ai_suggestions" and e.get("note_id") == note_id):
        for item in record.get("items", []):
            result["suggestions"].append({**item, "status": statuses.get(("suggestion", item["item_id"]))})
    for gap in (e for e in entries if e.get("action") == "identify_care_gap" and e.get("note_id") == note_id):
        result["care_gaps"].append({
            "gap_id": gap["gap_id"], "description": gap["description"], "confidence": gap.get("confidence"),
            "status": statuses.get(("care_gap", gap["gap_id"])), "suggestion_item_id": gap.get("suggestion_item_id"),
        })
    for order in (e for e in entries if e.get("action") == "order_test" and e.get("note_id") == note_id):
        result["orders"].append({
            "order_id": order["order_id"], "test_name": order["test_name"], "reason": order["reason"],
            "status": statuses.get(("test_order", order["order_id"])), "suggestion_item_id": order.get("suggestion_item_id"),
        })
    return result
