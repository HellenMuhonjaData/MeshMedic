"""Read-side helpers that rebuild notes, code sets, gaps and orders by replaying the audit trail.

Split out of server.py (Phase 0, no behaviour change).
"""

import app_core
import audit_store
from sample_patients import SAMPLE_PATIENTS


def _find_patient(ehr_system: str, patient_id: str) -> dict | None:
    for p in SAMPLE_PATIENTS:
        if p["ehr_system"] == ehr_system and p["patient_id"] == patient_id:
            return p
    return None


def _find_note(note_id: str) -> dict | None:
    """Reconstruct a note's current state by replaying the audit log --
    the audit trail is the single source of truth for note text/status
    (REQ-006), so no separate in-memory store can drift from what it says
    happened."""
    if not app_core.AUDIT_LOG_PATH.exists():
        return None
    generated = None
    latest_edit = None
    decision = None
    for entry in audit_store.read_entries(app_core.AUDIT_LOG_PATH):
        if entry.get("note_id") != note_id:
            continue
        if entry["action"] == "generate_note":
            generated = entry
        elif entry["action"] == "edit_note":
            latest_edit = entry
        elif entry["action"] in ("approve_note", "reject_note"):
            decision = entry
    if generated is None:
        return None
    return {"generated": generated, "latest_edit": latest_edit, "decision": decision}


def _current_note_text(record: dict) -> str:
    """The note's text as a clinician would see it right now: the latest
    saved edit if one exists, otherwise the AI's original draft."""
    if record["latest_edit"] is not None:
        return record["latest_edit"]["note_text"]
    return record["generated"]["note_text"]


def _find_suggestion(suggestion_id: str) -> dict | None:
    """Reconstruct a code suggestion's current state by replaying the audit
    log, same pattern as _find_note -- the audit trail is the single source
    of truth (REQ-006)."""
    if not app_core.AUDIT_LOG_PATH.exists():
        return None
    suggested = None
    decision = None
    for entry in audit_store.read_entries(app_core.AUDIT_LOG_PATH):
        if entry.get("suggestion_id") != suggestion_id:
            continue
        if entry["action"] == "suggest_codes":
            suggested = entry
        elif entry["action"] in ("approve_codes", "reject_codes"):
            decision = entry
    if suggested is None:
        return None
    return {"suggested": suggested, "decision": decision}


def _find_gap(gap_id: str) -> dict | None:
    """Reconstruct a single care gap's current state by replaying the audit
    log, same pattern as _find_note/_find_suggestion -- the audit trail is
    the single source of truth (REQ-006)."""
    if not app_core.AUDIT_LOG_PATH.exists():
        return None
    identified = None
    addressed = None
    for entry in audit_store.read_entries(app_core.AUDIT_LOG_PATH):
        if entry.get("gap_id") != gap_id:
            continue
        if entry["action"] == "identify_care_gap":
            identified = entry
        elif entry["action"] == "address_care_gap":
            addressed = entry
    if identified is None:
        return None
    return {"identified": identified, "addressed": addressed}


def _open_care_gaps_for_patient(patient_id: str) -> list[dict]:
    """Every currently-open (identified but not yet addressed) care gap for
    a patient, across every note/encounter, reconstructed by replaying the
    audit log -- same source-of-truth pattern as _find_gap, aggregated
    across every gap_id for this patient instead of looking up just one.
    Plain local file I/O, no model call -- REQ pattern for
    prioritize_care_gaps's "fetch the real data itself" requirement."""
    if not app_core.AUDIT_LOG_PATH.exists():
        return []
    identified: dict[str, dict] = {}
    addressed_ids: set[str] = set()
    for entry in audit_store.read_entries(app_core.AUDIT_LOG_PATH):
        if entry.get("patient_id") != patient_id:
            continue
        if entry["action"] == "identify_care_gap":
            identified[entry["gap_id"]] = entry
        elif entry["action"] == "address_care_gap":
            addressed_ids.add(entry["gap_id"])
    return [gap for gap_id, gap in identified.items() if gap_id not in addressed_ids]


def _find_order(order_id: str) -> dict | None:
    """Reconstruct a single test order's current state by replaying the
    audit log, same pattern as _find_gap/_find_suggestion/_find_note."""
    if not app_core.AUDIT_LOG_PATH.exists():
        return None
    ordered = None
    resulted = None
    for entry in audit_store.read_entries(app_core.AUDIT_LOG_PATH):
        if entry.get("order_id") != order_id:
            continue
        if entry["action"] == "order_test":
            ordered = entry
        elif entry["action"] == "record_test_result":
            resulted = entry
    if ordered is None:
        return None
    return {"ordered": ordered, "resulted": resulted}


def _orders_for_patient(patient_id: str) -> tuple[list[dict], list[dict]]:
    """Every test order for a patient, split into still-pending and
    resulted, reconstructed by replaying the audit log -- same
    source-of-truth pattern as _open_care_gaps_for_patient, generalized to
    orders so a new encounter can see what a prior one set in motion."""
    if not app_core.AUDIT_LOG_PATH.exists():
        return [], []
    ordered: dict[str, dict] = {}
    resulted: dict[str, dict] = {}
    for entry in audit_store.read_entries(app_core.AUDIT_LOG_PATH):
        if entry.get("patient_id") != patient_id:
            continue
        if entry["action"] == "order_test":
            ordered[entry["order_id"]] = entry
        elif entry["action"] == "record_test_result":
            resulted[entry["order_id"]] = entry
    pending = [o for oid, o in ordered.items() if oid not in resulted]
    done = [
        {"ordered": ordered[oid], "resulted": r}
        for oid, r in resulted.items()
        if oid in ordered
    ]
    return pending, done


def _all_audit_entries() -> list[dict]:
    """Every entry currently in the audit trail, oldest first. Unlike
    _find_note/_find_suggestion/_find_gap (each keyed to one id), the
    compliance checks below need the whole trail at once -- this is their
    shared read, so each control does its own single pass over it rather than
    re-opening the file per control."""
    if not app_core.AUDIT_LOG_PATH.exists():
        return []
    return audit_store.read_entries(app_core.AUDIT_LOG_PATH)


def used_attestation_nonces() -> set[str]:
    """Every attestation nonce already recorded, so a replayed attestation is refused (Phase 1)."""
    nonces = set()
    for entry in _all_audit_entries():
        actor = entry.get("actor")
        if isinstance(actor, dict) and actor.get("attestation_nonce"):
            nonces.add(actor["attestation_nonce"])
    return nonces


def find_note_signature(note_id: str) -> dict | None:
    """The sign_note entry for a note, if it has been signed."""
    for entry in _all_audit_entries():
        if entry.get("action") == "sign_note" and entry.get("note_id") == note_id:
            return entry
    return None
