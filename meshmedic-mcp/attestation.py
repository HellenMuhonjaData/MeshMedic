"""Clinician attestations: proof that an authenticated clinician authorized one exact action.

Clinician-only actions (signing a note, and from later phases authorizing
orders, final code decisions and approving referrals for sending) are MCP
tools, so any connected AI client can *see* them. What it cannot do is
produce an attestation:

* The web app (ui_bridge.py) holds an Ed25519 private key. It issues an
  attestation only after the clinician has signed in and re-entered their
  signing PIN in the last few minutes.
* The MCP server holds only the public key. It verifies the signature and
  that the attestation names this exact action, record and content, has not
  expired, and that its one-time number (nonce) has never been used. Used
  nonces are recorded in the audit trail, so replays are refused even after
  a restart.

Token format: base64url(JSON claims) "." base64url(signature).

Threat model limits: anything that can read the private key file can mint
attestations, so the key lives outside the repo (.secrets/, git-ignored) and
outside any directory an MCP client is allowed to read (roots_guard.py).
"""

import base64
import hashlib
import json
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

DEFAULT_KEY_DIR = Path(__file__).parent / ".secrets"
PRIVATE_KEY_NAME = "attestation_ed25519_private.pem"
PUBLIC_KEY_NAME = "attestation_ed25519_public.pem"
TTL_SECONDS = 120

# Actions an attestation can authorize. Shared by the web app (issuer) and
# the MCP server (verifier); this module has no server dependencies.
ACT_AS = "act_as"  # identity for a review action; not bound to content
SIGN_NOTE = "sign_note"  # clinician-only: sign one reviewed note
AUTH_EVENT = "auth_event"  # the web app recording a sign-in event
AUDIO_DELETED = "audio_deleted"  # the web app recording a retention deletion
WEB_APP_IDENTITY = "meshmedic-web-app"


class AttestationError(Exception):
    """The attestation is missing, forged, expired, replayed or for something else."""


@dataclass(frozen=True)
class Claims:
    clinician_id: str
    display_name: str
    action: str
    record_id: str
    content_hash: str
    nonce: str
    issued_at: int
    expires_at: int


def content_hash(text: str) -> str:
    """SHA-256 of the exact content being attested (the browser computes the same)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def key_dir() -> Path:
    return Path(os.environ.get("MESHMEDIC_ATTESTATION_KEY_DIR") or DEFAULT_KEY_DIR)


def load_or_create_private_key(directory: Path | None = None) -> Ed25519PrivateKey:
    """Web app only. Creates the key pair on first use."""
    directory = directory or key_dir()
    private_path = directory / PRIVATE_KEY_NAME
    if private_path.exists():
        key = serialization.load_pem_private_key(private_path.read_bytes(), password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise AttestationError("attestation private key is not an Ed25519 key")
        return key
    directory.mkdir(parents=True, exist_ok=True)
    key = Ed25519PrivateKey.generate()
    private_path.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    try:
        os.chmod(private_path, 0o600)
    except OSError:
        pass  # Windows ignores POSIX modes; the directory is git-ignored and outside MCP roots.
    (directory / PUBLIC_KEY_NAME).write_bytes(
        key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    )
    return key


def load_public_key(directory: Path | None = None) -> Ed25519PublicKey:
    """MCP server only. Fails closed: no key means no clinician-only action can succeed."""
    path = (directory or key_dir()) / PUBLIC_KEY_NAME
    if not path.exists():
        raise AttestationError("attestation public key is not configured; clinician-only actions are disabled")
    key = serialization.load_pem_public_key(path.read_bytes())
    if not isinstance(key, Ed25519PublicKey):
        raise AttestationError("attestation public key is not an Ed25519 key")
    return key


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def issue(
    private_key: Ed25519PrivateKey,
    clinician_id: str,
    display_name: str,
    action: str,
    record_id: str,
    content_sha256: str,
    now: float | None = None,
) -> str:
    issued = int(now if now is not None else time.time())
    claims = {
        "clinician_id": clinician_id,
        "display_name": display_name,
        "action": action,
        "record_id": record_id,
        "content_hash": content_sha256,
        "nonce": secrets.token_hex(16),
        "issued_at": issued,
        "expires_at": issued + TTL_SECONDS,
    }
    payload = json.dumps(claims, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"{_b64(payload)}.{_b64(private_key.sign(payload))}"


def verify(
    token: str,
    public_key: Ed25519PublicKey,
    *,
    action: str,
    record_id: str,
    content_sha256: str,
    nonce_used,
    now: float | None = None,
) -> Claims:
    """Return the claims, or raise AttestationError naming what is wrong."""
    if not token or token.count(".") != 1:
        raise AttestationError("attestation is missing or malformed")
    payload_b64, sig_b64 = token.split(".")
    try:
        payload, signature = _unb64(payload_b64), _unb64(sig_b64)
    except (ValueError, TypeError) as exc:
        raise AttestationError("attestation is malformed") from exc
    try:
        public_key.verify(signature, payload)
    except InvalidSignature as exc:
        raise AttestationError("attestation signature is invalid") from exc
    try:
        data = json.loads(payload)
        claims = Claims(**{f: data[f] for f in Claims.__dataclass_fields__})
    except (ValueError, KeyError, TypeError) as exc:
        raise AttestationError("attestation claims are malformed") from exc
    current = now if now is not None else time.time()
    if current > claims.expires_at:
        raise AttestationError("attestation has expired")
    if claims.action != action:
        raise AttestationError(f"attestation is for {claims.action!r}, not {action!r}")
    if claims.record_id != record_id:
        raise AttestationError("attestation is for a different record")
    if claims.content_hash != content_sha256:
        raise AttestationError("attestation is for different content")
    if nonce_used(claims.nonce):
        raise AttestationError("attestation has already been used")
    return claims


ACT_AS_CONTENT = hashlib.sha256(b"act_as").hexdigest()


def event_hash(event: dict) -> str:
    """Content hash for a sign-in event the web app asks the server to record."""
    return content_hash(json.dumps(event, sort_keys=True, separators=(",", ":")))
