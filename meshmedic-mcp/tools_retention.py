"""Audit record of raw-audio deletion under the retention policy (Phase 2, REQ-025).

The web app's retention purge calls this before deleting an encounter's
audio. Only the web app can record a deletion (attested as the web app
itself); AI clients cannot. Recording twice for one encounter writes once,
so a purge that is retried never duplicates the entry. The clinical note in
the audit trail is not affected by audio deletion.
"""

from datetime import datetime, timezone
from typing import Annotated

from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field

import attestation
import audit_actor
import clinician_actor
import encounter_records
import encounters
from app_core import _append_audit_entry, mcp
from audit_queries import _all_audit_entries

DELETE_ACTION = "audio_deleted"


class AudioDeletion(BaseModel):
    encounter_id: str
    deleted_at: str
    retention_days: int
    policy_ref: str
    recorded: bool


def deletion_event(encounter_id: str, retention_days: int, policy_ref: str) -> dict:
    """The exact content the web app attests for a deletion."""
    return {"encounter_id": encounter_id, "retention_days": retention_days, "policy_ref": policy_ref}


@mcp.tool()
def record_audio_deletion(
    encounter_id: Annotated[str, Field(min_length=1, max_length=64)],
    retention_days: Annotated[int, Field(ge=0, le=36500)],
    policy_ref: Annotated[str, Field(min_length=1, max_length=200)],
    attestation_token: Annotated[str, Field(min_length=1, max_length=4000)],
) -> AudioDeletion:
    """Record that an encounter's raw audio is being deleted under the retention policy. Web app only."""
    if not encounters.is_encounter_id(encounter_id):
        raise ToolError("encounter_id is not a valid encounter identifier.")
    content = attestation.event_hash(deletion_event(encounter_id, retention_days, policy_ref))
    entries = _all_audit_entries()
    existing = next((e for e in entries if e.get("action") == DELETE_ACTION and e.get("encounter_id") == encounter_id), None)
    if existing is not None:
        return AudioDeletion(encounter_id=encounter_id, deleted_at=existing["timestamp"],
                             retention_days=existing["retention_days"], policy_ref=existing["policy_ref"],
                             recorded=False)
    try:
        claims = attestation.verify(attestation_token, attestation.load_public_key(), action=attestation.AUDIO_DELETED,
                                    record_id=encounter_id, content_sha256=content,
                                    nonce_used=clinician_actor.nonce_used)
    except attestation.AttestationError as exc:
        raise ToolError(f"Audio deletion refused: {exc}.") from exc
    if claims.clinician_id != attestation.WEB_APP_IDENTITY:
        raise ToolError("Audio deletions can only be recorded by the MeshMedic web app.")
    start = encounter_records.find_encounter(entries, encounter_id)
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "action": DELETE_ACTION,
        "encounter_id": encounter_id,
        "patient_id": (start or {}).get("patient_id"),
        "retention_days": retention_days,
        "policy_ref": policy_ref,
        "actor": {**audit_actor.SYSTEM, "attestation_nonce": claims.nonce},
    }
    _append_audit_entry(entry)
    return AudioDeletion(encounter_id=encounter_id, deleted_at=entry["timestamp"], retention_days=retention_days,
                         policy_ref=policy_ref, recorded=True)
