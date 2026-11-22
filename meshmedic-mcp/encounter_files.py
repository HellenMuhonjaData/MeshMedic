"""Encrypted per-encounter files: raw audio and the background job's working copies (Phase 2).

Layout: encounter_data/<encounter_id>/<kind>.enc (git-ignored, protected path).
Kinds: "context" (which patient), "audio" (raw recording), "transcript", "draft" (the AI draft JSON).
Every file is AES-256-GCM encrypted with the encounter id and kind bound as
associated data, so a file copied to another encounter or renamed to
another kind fails to decrypt. The key (files_aes256gcm.key) lives in the
git-ignored secrets folder and is separate from the audit-trail key.

Only the web app process (the background worker and the signed-in
clinician's authenticated routes) uses this module; the MCP server never
reads audio.

Retention: raw audio is deleted MESHMEDIC_AUDIO_RETENTION_DAYS after its
note is signed (purge_expired). Working copies are deleted with it. Clinical
notes live in the audit trail and are never touched here.
"""

import json
import os
import shutil
import time
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

import attestation
import encounters

KINDS = ("context", "audio", "transcript", "draft")
KEY_NAME = "files_aes256gcm.key"
MAX_AUDIO_BYTES = 50 * 1024 * 1024


class EncounterFileError(Exception):
    """Missing, unreadable or tampered encounter file, or an invalid request."""


def data_root() -> Path:
    return Path(os.environ.get("MESHMEDIC_ENCOUNTER_DATA_DIR") or Path(__file__).parent / "encounter_data")


def _key() -> bytes:
    path = attestation.key_dir() / KEY_NAME
    if path.exists():
        return path.read_bytes()
    path.parent.mkdir(parents=True, exist_ok=True)
    key = AESGCM.generate_key(bit_length=256)
    path.write_bytes(key)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass  # Windows ignores POSIX modes; the folder is git-ignored and protected.
    return key


def _path(encounter_id: str, kind: str) -> Path:
    if not encounters.is_encounter_id(encounter_id):
        raise EncounterFileError("invalid encounter id")
    if kind not in KINDS:
        raise EncounterFileError(f"unknown file kind {kind!r}")
    return data_root() / encounter_id / f"{kind}.enc"


def _aad(encounter_id: str, kind: str) -> bytes:
    return f"{encounter_id}:{kind}".encode()


def write(encounter_id: str, kind: str, data: bytes, meta: dict | None = None) -> None:
    """Write (or replace) one file atomically. `meta` must hold no patient information."""
    if kind == "audio" and len(data) > MAX_AUDIO_BYTES:
        raise EncounterFileError("audio file is too large")
    path = _path(encounter_id, kind)
    path.parent.mkdir(parents=True, exist_ok=True)
    nonce = os.urandom(12)
    blob = nonce + AESGCM(_key()).encrypt(nonce, data, _aad(encounter_id, kind))
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(blob)
    tmp.replace(path)
    if meta is not None:
        path.with_suffix(".meta.json").write_text(json.dumps({**meta, "stored_at": time.time()}), encoding="utf-8")


def read(encounter_id: str, kind: str) -> bytes:
    path = _path(encounter_id, kind)
    if not path.exists():
        raise EncounterFileError(f"no {kind} stored for this encounter")
    blob = path.read_bytes()
    try:
        return AESGCM(_key()).decrypt(blob[:12], blob[12:], _aad(encounter_id, kind))
    except (InvalidTag, ValueError) as exc:
        raise EncounterFileError(f"{kind} file failed authenticated decryption") from exc


def exists(encounter_id: str, kind: str) -> bool:
    return _path(encounter_id, kind).exists()


def meta(encounter_id: str, kind: str) -> dict:
    path = _path(encounter_id, kind).with_suffix(".meta.json")
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def delete_encounter_files(encounter_id: str) -> bool:
    """Delete audio and working copies for one encounter. True if anything was deleted."""
    folder = _path(encounter_id, "audio").parent
    if not folder.exists():
        return False
    shutil.rmtree(folder)
    return True


def stored_encounters() -> list[str]:
    root = data_root()
    if not root.exists():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir() and encounters.is_encounter_id(p.name))
