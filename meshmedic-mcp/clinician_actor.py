"""Turning a clinician attestation into a verified audit `actor`, server side.

Two strengths of attestation, both issued only by the web app:

* "act_as"  -- the signed-in clinician is doing a review action (editing,
  marking reviewed, rejecting). Proves *who*, from their session; no PIN.
* clinician-only actions ("sign_note", later "authorize_orders", ...) --
  issued only after the clinician re-entered their signing PIN, and bound
  to the exact content being signed or authorized.

A tool called with no attestation (an AI client, or older callers) records
the actor as "unverified" -- never as a clinician. An attestation that is
present but invalid is refused outright rather than downgraded.
"""

import attestation
import audit_actor
import audit_queries
import clinician_registry

ACT_AS = attestation.ACT_AS
ACT_AS_CONTENT = attestation.ACT_AS_CONTENT


def nonce_used(nonce: str) -> bool:
    return nonce in audit_queries.used_attestation_nonces()


def verify(token: str, *, action: str, record_id: str, content_sha256: str) -> attestation.Claims:
    """Verify an attestation and that its clinician is still an active account."""
    claims = attestation.verify(
        token,
        attestation.load_public_key(),
        action=action,
        record_id=record_id,
        content_sha256=content_sha256,
        nonce_used=nonce_used,
    )
    if clinician_registry.get_active(claims.clinician_id) is None:
        raise attestation.AttestationError("attestation names a clinician who is not an active account")
    return claims


def actor_from_claims(claims: attestation.Claims) -> dict:
    return {
        **audit_actor.clinician(claims.clinician_id),
        "display_name": claims.display_name,
        "attestation_action": claims.action,
        "attestation_nonce": claims.nonce,
    }


def resolve_review_actor(actor_attestation: str | None, record_id: str) -> dict:
    """Actor for a review action: verified clinician, or unverified when no attestation was given."""
    if not actor_attestation:
        return dict(audit_actor.UNVERIFIED)
    claims = verify(actor_attestation, action=ACT_AS, record_id=record_id, content_sha256=ACT_AS_CONTENT)
    return actor_from_claims(claims)


event_hash = attestation.event_hash
