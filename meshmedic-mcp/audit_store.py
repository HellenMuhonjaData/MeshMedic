"""Encrypted, append-only, tamper-evident storage for the audit trail (Phase 2, REQ-025).

File format: one JSON object per line.

* Legacy lines (written before encryption) are plain audit entries. They are
  never rewritten and stay readable.
* Encrypted lines look like
      {"v": 2, "seq": 7, "prev": "<sha256 of previous line>", "anchor": ..., "nonce": "...", "ct": "..."}
  - `ct` is the entry encrypted with AES-256-GCM. The header fields (v, seq,
    prev, anchor) are bound to it as associated data, so changing a header
    or the ciphertext makes decryption fail.
  - `prev` chains each line to the one before it and `seq` counts up from 1,
    so altering, removing, inserting or reordering lines is detected. This
    chain check needs no key: anyone can verify the trail is intact.
  - The first encrypted line carries `anchor`, a hash of every legacy line
    before it, so the pre-encryption records are protected from then on too.
  - Once encryption has started, plain lines are refused (no forged inserts).
  - A small head file next to the trail (<name>.head) records the latest
    sequence number and line hash, so deleting entries from the *end* of
    the trail -- which a chain alone cannot see -- is detected too. A head
    that is one step behind (a crash between the two writes) is accepted.

Reads are strict: a broken chain, a failed decryption or a missing key
raises AuditIntegrityError -- the trail is never silently skipped or
partially read. The key lives in the git-ignored secrets folder
(audit_aes256gcm.key) and must be backed up with the audit trail: losing it
makes encrypted entries unreadable (see docs/PHI_READINESS.md).

MESHMEDIC_AUDIT_ENCRYPTION=off writes plain lines (development only); it is
refused once a trail contains encrypted lines.
"""

import base64
import hashlib
import json
import os
import threading
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

import attestation

FORMAT_VERSION = 2
KEY_NAME = "audit_aes256gcm.key"

# The MCP server runs tools on worker threads, so two tools can write at the
# same moment (two background jobs recording notes, say). Every append is a
# read-then-write of the chain tail, and a read must never see a half-written
# line, so all access goes through one lock. One process writes the trail
# (docs/TRANSPORT_DECISION.md), so a process-local lock is sufficient.
_LOCK = threading.RLock()


class AuditIntegrityError(Exception):
    """The audit trail cannot be trusted or read: tampering, a broken chain or a missing key."""


def encryption_enabled() -> bool:
    return (os.environ.get("MESHMEDIC_AUDIT_ENCRYPTION") or "on").strip().lower() != "off"


def key_path() -> Path:
    return attestation.key_dir() / KEY_NAME


def _load_key(create: bool) -> bytes:
    path = key_path()
    if path.exists():
        key = path.read_bytes()
        if len(key) != 32:
            raise AuditIntegrityError("audit encryption key is not a 256-bit key")
        return key
    if not create:
        raise AuditIntegrityError(
            "the audit trail has encrypted entries but the audit encryption key is missing; "
            "restore it from backup -- entries are never skipped"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    key = AESGCM.generate_key(bit_length=256)
    path.write_bytes(key)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass  # Windows ignores POSIX modes; the folder is git-ignored and a protected path.
    return key


def line_hash(raw_line: str) -> str:
    return hashlib.sha256(raw_line.encode("utf-8")).hexdigest()


def _raw_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        return [line.rstrip("\n") for line in f if line.strip()]


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _aad(header: dict) -> bytes:
    return json.dumps(header, sort_keys=True, separators=(",", ":")).encode("utf-8")


def head_path(path: Path) -> Path:
    return path.with_name(path.name + ".head")


def _write_head(path: Path, seq: int, raw_line: str) -> None:
    head_path(path).write_text(json.dumps({"seq": seq, "hash": line_hash(raw_line)}), encoding="utf-8")


def _is_encrypted(obj: dict) -> bool:
    return obj.get("v") == FORMAT_VERSION and "ct" in obj


def _append_unlocked(path: Path, entry: dict) -> None:
    """Append one entry. Raises OSError if the file can't be written, AuditIntegrityError if the trail is unsafe."""
    lines = _raw_lines(path)
    parsed = [json.loads(line) for line in lines]
    encrypted_started = any(_is_encrypted(obj) for obj in parsed)

    if not encryption_enabled():
        if encrypted_started:
            raise AuditIntegrityError("this audit trail is encrypted; plain entries can no longer be appended")
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        return

    key = _load_key(create=not encrypted_started)
    last_seq = max((obj["seq"] for obj in parsed if _is_encrypted(obj)), default=0)
    header = {"v": FORMAT_VERSION, "seq": last_seq + 1, "prev": line_hash(lines[-1]) if lines else None}
    if not encrypted_started:
        header["anchor"] = hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()
    nonce = os.urandom(12)
    ciphertext = AESGCM(key).encrypt(nonce, json.dumps(entry).encode("utf-8"), _aad(header))
    record = {**header, "nonce": _b64(nonce), "ct": _b64(ciphertext)}
    raw = json.dumps(record, separators=(",", ":"))
    with path.open("a", encoding="utf-8") as f:
        f.write(raw + "\n")
    _write_head(path, header["seq"], raw)


def _chain_problems_unlocked(path: Path) -> list[str]:
    """Structural integrity check that needs no key: chain, sequence, anchor, no plain inserts."""
    lines = _raw_lines(path)
    problems: list[str] = []
    started = False
    expected_seq = 1
    for i, raw in enumerate(lines):
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            problems.append(f"line {i + 1} is not valid JSON")
            continue
        if not _is_encrypted(obj):
            if started:
                problems.append(f"line {i + 1} is a plain entry after encryption started")
            continue
        if not started:
            started = True
            if obj.get("anchor") != hashlib.sha256("\n".join(lines[:i]).encode("utf-8")).hexdigest():
                problems.append("records written before encryption have changed since encryption started")
        if obj.get("seq") != expected_seq:
            problems.append(f"line {i + 1} has sequence {obj.get('seq')}, expected {expected_seq}")
        expected_seq = (obj.get("seq") or expected_seq) + 1
        if obj.get("prev") != (line_hash(lines[i - 1]) if i else None):
            problems.append(f"line {i + 1} does not chain to the line before it")
    problems.extend(_head_problems(path, lines))
    return problems


def _head_problems(path: Path, lines: list[str]) -> list[str]:
    hp = head_path(path)
    if not hp.exists():
        return []
    try:
        head = json.loads(hp.read_text(encoding="utf-8"))
        head_seq, head_hash = int(head["seq"]), head["hash"]
    except (OSError, ValueError, KeyError, TypeError):
        return ["the audit trail head record is unreadable"]
    by_seq = {}
    for raw in lines:
        obj = json.loads(raw)
        if _is_encrypted(obj):
            by_seq[obj.get("seq")] = raw
    last_seq = max(by_seq, default=0)
    if head_seq > last_seq:
        return [f"entries are missing from the end of the audit trail (expected up to {head_seq}, found {last_seq})"]
    if head_seq in by_seq and line_hash(by_seq[head_seq]) != head_hash:
        return [f"audit entry {head_seq} does not match the head record"]
    if last_seq > head_seq + 1:
        return ["the audit trail head record is out of date by more than one entry"]
    return []


def _read_entries_unlocked(path: Path) -> list[dict]:
    """Every entry, oldest first, decrypted. Strict: raises AuditIntegrityError on any problem."""
    problems = _chain_problems_unlocked(path)
    if problems:
        raise AuditIntegrityError("audit trail integrity check failed: " + "; ".join(problems[:5]))
    entries: list[dict] = []
    key = None
    for raw in _raw_lines(path):
        obj = json.loads(raw)
        if not _is_encrypted(obj):
            entries.append(obj)
            continue
        if key is None:
            key = _load_key(create=False)
        header = {k: obj[k] for k in ("v", "seq", "prev", "anchor") if k in obj}
        try:
            plain = AESGCM(key).decrypt(base64.b64decode(obj["nonce"]), base64.b64decode(obj["ct"]), _aad(header))
        except (InvalidTag, ValueError, KeyError) as exc:
            raise AuditIntegrityError(f"audit entry {obj.get('seq')} failed authenticated decryption") from exc
        entries.append(json.loads(plain))
    return entries


def append(path: Path, entry: dict) -> None:
    """Append one entry. Raises OSError if the file can't be written, AuditIntegrityError if the trail is unsafe."""
    with _LOCK:
        _append_unlocked(path, entry)


def chain_problems(path: Path) -> list[str]:
    """Structural integrity check that needs no key: chain, sequence, anchor, head, no plain inserts."""
    with _LOCK:
        return _chain_problems_unlocked(path)


def read_entries(path: Path) -> list[dict]:
    """Every entry, oldest first, decrypted. Strict: raises AuditIntegrityError on any problem."""
    with _LOCK:
        return _read_entries_unlocked(path)
