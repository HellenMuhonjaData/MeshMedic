"""Clinical note sign-off and sign-in audit events (Phase 1, STORY-014).

Lifecycle: draft -> reviewed (approve_encounter_note) -> signed (this tool).
Signing is a clinician-only action: it needs an attestation the web app
issues only after the clinician re-entered their signing PIN, bound to this
note and to the exact text they reviewed (see attestation.py). An AI client
can see this tool but cannot produce that attestation, so it cannot sign.

There is deliberately no way to sign several notes in one call: one note,
one attestation, one signature.
"""

from datetime import datetime, timezone
from typing import Annotated, Literal

from mcp.server.mcpserver.exceptions import ResourceNotFoundError, ToolError
from pydantic import BaseModel, Field

import attestation
import audit_actor
import audit_queries
import clinician_actor
from app_core import _append_audit_entry, _logger, mcp
from logging_utils import (
    ACCESS_DENIED,
    TOOL_COMPLETED,
    TOOL_STARTED,
    log_event,
    new_correlation_id,
)

SIGN_ACTION = attestation.SIGN_NOTE
AUTH_EVENT_ACTION = attestation.AUTH_EVENT
BRIDGE_IDENTITY = attestation.WEB_APP_IDENTITY

AuthEventKind = Literal["login_success", "login_failure", "logout", "reauth_success", "reauth_failure", "reauth_locked"]
_SECURITY_KINDS = {"login_failure", "reauth_failure", "reauth_locked"}


class SignedNote(BaseModel):
    note_id: str
    patient_id: str
    ehr_system: str
    status: Literal["signed"]
    content_hash: str
    signed_by_clinician_id: str
    signed_by_name: str
    signed_at: str


class AuthEvent(BaseModel):
    event_id: Annotated[str, Field(min_length=8, max_length=64)]
    kind: AuthEventKind
    clinician_id: Annotated[str | None, Field(max_length=64)] = None
    detail: Annotated[str | None, Field(max_length=300)] = None


def _signed_note(entry: dict) -> SignedNote:
    actor = entry["actor"]
    return SignedNote(
        note_id=entry["note_id"],
        patient_id=entry["patient_id"],
        ehr_system=entry["ehr_system"],
        status="signed",
        content_hash=entry["content_hash"],
        signed_by_clinician_id=actor["clinician_id"],
        signed_by_name=actor.get("display_name", actor["clinician_id"]),
        signed_at=entry["timestamp"],
    )


def _refuse(correlation_id: str, note_id: str, reason: str, message: str) -> ToolError:
    log_event(
        _logger, "warning", ACCESS_DENIED, correlation_id,
        tool="sign_encounter_note", note_id=note_id, reason=reason, error_class="AccessDenied",
    )
    return ToolError(message)


@mcp.tool()
def sign_encounter_note(
    note_id: Annotated[str, Field(min_length=1)],
    content_hash: Annotated[str, Field(min_length=64, max_length=64)],
    attestation_token: Annotated[str, Field(min_length=1, max_length=4000)],
) -> SignedNote:
    """
    Sign (finalize) one reviewed clinical note. Clinician-only: requires an
    attestation issued by the MeshMedic web app after the clinician
    re-entered their signing PIN. AI clients cannot sign notes.

    `content_hash` is the SHA-256 of the exact note text the clinician
    reviewed; signing is refused if it does not match the reviewed text.
    Signing an already-signed note with the same content returns the
    existing signature without writing anything.
    """
    correlation_id = new_correlation_id()
    log_event(_logger, "info", TOOL_STARTED, correlation_id, tool="sign_encounter_note", note_id=note_id)

    record = audit_queries._find_note(note_id)
    if record is None:
        raise ResourceNotFoundError(f"No generated note found for note_id={note_id!r}.")

    existing = audit_queries.find_note_signature(note_id)
    if existing is not None:
        if existing["content_hash"] != content_hash:
            raise _refuse(correlation_id, note_id, "already_signed", f"note_id={note_id!r} is already signed and locked.")
        # A repeated request is a no-op only when it carries the very attestation
        # that made this signature; anyone else gets a refusal, not the signature.
        original_nonce = existing["actor"].get("attestation_nonce")
        try:
            attestation.verify(
                attestation_token, attestation.load_public_key(), action=SIGN_ACTION, record_id=note_id,
                content_sha256=content_hash, nonce_used=lambda nonce: nonce != original_nonce,
            )
        except attestation.AttestationError as exc:
            raise _refuse(correlation_id, note_id, "already_signed",
                          f"note_id={note_id!r} is already signed and locked.") from exc
        log_event(_logger, "info", TOOL_COMPLETED, correlation_id, tool="sign_encounter_note",
                  note_id=note_id, outcome="success", no_op=True)
        return _signed_note(existing)

    decision = record["decision"]
    if decision is None or decision["action"] != "approve_note":
        raise _refuse(correlation_id, note_id, "not_reviewed",
                      f"note_id={note_id!r} must be reviewed (approved) before it can be signed.")
    if attestation.content_hash(decision["note_text"]) != content_hash:
        raise _refuse(correlation_id, note_id, "content_mismatch",
                      "The note text has changed since it was reviewed; review it again before signing.")

    try:
        claims = clinician_actor.verify(
            attestation_token, action=SIGN_ACTION, record_id=note_id, content_sha256=content_hash
        )
    except attestation.AttestationError as exc:
        raise _refuse(correlation_id, note_id, "attestation_rejected",
                      f"Signing refused: {exc}. Only a signed-in clinician can sign notes.") from exc

    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "action": SIGN_ACTION,
        "note_id": note_id,
        "ehr_system": decision["ehr_system"],
        "patient_id": decision["patient_id"],
        "content_hash": content_hash,
        "status": "signed",
        "actor": clinician_actor.actor_from_claims(claims),
    }
    _append_audit_entry(entry)
    log_event(_logger, "info", TOOL_COMPLETED, correlation_id, tool="sign_encounter_note",
              note_id=note_id, outcome="success", no_op=False)
    return _signed_note(entry)


class RecordedAuthEvent(BaseModel):
    event_id: str
    kind: str
    recorded: bool


@mcp.tool()
def record_auth_event(
    event: AuthEvent,
    attestation_token: Annotated[str, Field(min_length=1, max_length=4000)],
) -> RecordedAuthEvent:
    """
    Record a sign-in event from the MeshMedic web app (successful and failed
    sign-ins, PIN re-entry, lockouts) in the audit trail. Accepted only with
    an attestation from the web app itself; not for AI clients.
    Recording the same event_id twice writes it once.
    """
    correlation_id = new_correlation_id()
    event_data = event.model_dump()
    try:
        claims = attestation.verify(
            attestation_token,
            attestation.load_public_key(),
            action=AUTH_EVENT_ACTION,
            record_id=event.event_id,
            content_sha256=clinician_actor.event_hash(event_data),
            nonce_used=clinician_actor.nonce_used,
        )
    except attestation.AttestationError as exc:
        log_event(_logger, "warning", ACCESS_DENIED, correlation_id, tool="record_auth_event",
                  reason="attestation_rejected", error_class="AccessDenied")
        raise ToolError(f"Auth event refused: {exc}.") from exc
    if claims.clinician_id != BRIDGE_IDENTITY:
        raise ToolError("Auth events can only be recorded by the MeshMedic web app.")

    if any(e.get("action") == AUTH_EVENT_ACTION and e.get("event_id") == event.event_id
           for e in audit_queries._all_audit_entries()):
        return RecordedAuthEvent(event_id=event.event_id, kind=event.kind, recorded=False)

    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "action": AUTH_EVENT_ACTION,
        **event_data,
        "actor": {**audit_actor.SYSTEM, "attestation_nonce": claims.nonce},
    }
    if event.kind in _SECURITY_KINDS:
        entry.update({"security_incident": True, "outcome": "failure", "error_class": "AuthenticationFailure"})
    _append_audit_entry(entry)
    return RecordedAuthEvent(event_id=event.event_id, kind=event.kind, recorded=True)
