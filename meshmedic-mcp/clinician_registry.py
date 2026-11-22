"""Local clinician accounts: who may sign in, sign notes and authorize actions.

Stored in clinicians.json (path from MESHMEDIC_CLINICIANS_FILE), which is
git-ignored. Passwords and signing PINs are never stored -- only salted
scrypt hashes (Python standard library, no extra dependency). Manage
accounts with `python manage_clinicians.py`.

Record shape:
    {"clinicians": {"<id>": {
        "display_name": "Dr. A. Rivera", "role": "physician", "active": true,
        "clinic_hours": {"start": "08:00", "end": "17:00", "utc_offset_minutes": 0},
        "password": {"salt": "<hex>", "hash": "<hex>"},
        "pin": {"salt": "<hex>", "hash": "<hex>"}}}}
"""

import hashlib
import hmac
import json
import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path

DEFAULT_FILE = Path(__file__).parent / "clinicians.json"
_SCRYPT = {"n": 2**14, "r": 8, "p": 1, "dklen": 32}
_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")
MIN_PASSWORD_LENGTH = 10
PIN_PATTERN = re.compile(r"^\d{4,8}$")


class RegistryError(Exception):
    """The clinician registry is missing, unreadable or the request is invalid."""


@dataclass(frozen=True)
class Clinician:
    clinician_id: str
    display_name: str
    role: str
    clinic_hours: dict


def registry_path() -> Path:
    return Path(os.environ.get("MESHMEDIC_CLINICIANS_FILE") or DEFAULT_FILE)


def _hash(secret: str, salt: bytes) -> str:
    return hashlib.scrypt(secret.encode("utf-8"), salt=salt, **_SCRYPT).hex()


def _make_secret(secret: str) -> dict:
    salt = secrets.token_bytes(16)
    return {"salt": salt.hex(), "hash": _hash(secret, salt)}


def _matches(secret: str, stored: dict) -> bool:
    try:
        expected = stored["hash"]
        actual = _hash(secret, bytes.fromhex(stored["salt"]))
    except (KeyError, TypeError, ValueError):
        return False
    return hmac.compare_digest(actual, expected)


def _load(path: Path) -> dict:
    if not path.exists():
        return {"clinicians": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RegistryError(f"clinician registry {path.name} could not be read ({type(exc).__name__})") from exc
    if not isinstance(data, dict) or not isinstance(data.get("clinicians"), dict):
        raise RegistryError(f"clinician registry {path.name} is malformed")
    return data


def _record_to_clinician(clinician_id: str, record: dict) -> Clinician:
    return Clinician(clinician_id, record["display_name"], record.get("role", "clinician"), record.get("clinic_hours", {}))


def add_clinician(
    clinician_id: str,
    display_name: str,
    password: str,
    pin: str,
    role: str = "physician",
    clinic_hours: dict | None = None,
    path: Path | None = None,
) -> Clinician:
    path = path or registry_path()
    if not _ID.match(clinician_id):
        raise RegistryError("clinician id must be 2-64 lowercase letters, digits, '.', '_' or '-'")
    if not display_name.strip():
        raise RegistryError("display name is required")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise RegistryError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    if not PIN_PATTERN.match(pin):
        raise RegistryError("signing PIN must be 4-8 digits")
    data = _load(path)
    if clinician_id in data["clinicians"]:
        raise RegistryError(f"clinician {clinician_id!r} already exists")
    record = {
        "display_name": display_name.strip(),
        "role": role,
        "active": True,
        "clinic_hours": clinic_hours or {"start": "08:00", "end": "17:00", "utc_offset_minutes": 0},
        "password": _make_secret(password),
        "pin": _make_secret(pin),
    }
    data["clinicians"][clinician_id] = record
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return _record_to_clinician(clinician_id, record)


def set_active(clinician_id: str, active: bool, path: Path | None = None) -> None:
    path = path or registry_path()
    data = _load(path)
    if clinician_id not in data["clinicians"]:
        raise RegistryError(f"no clinician {clinician_id!r}")
    data["clinicians"][clinician_id]["active"] = active
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def get_active(clinician_id: str, path: Path | None = None) -> Clinician | None:
    record = _load(path or registry_path())["clinicians"].get(clinician_id)
    if not record or not record.get("active", False):
        return None
    return _record_to_clinician(clinician_id, record)


def list_clinicians(path: Path | None = None) -> list[tuple[Clinician, bool]]:
    data = _load(path or registry_path())["clinicians"]
    return [(_record_to_clinician(cid, r), bool(r.get("active"))) for cid, r in sorted(data.items())]


def verify_password(clinician_id: str, password: str, path: Path | None = None) -> Clinician | None:
    """The clinician if the password is right and the account is active; otherwise None."""
    record = _load(path or registry_path())["clinicians"].get(clinician_id)
    if not record or not record.get("active", False):
        _hash(password, b"\0" * 16)  # same work either way, so timing doesn't reveal which ids exist
        return None
    return _record_to_clinician(clinician_id, record) if _matches(password, record.get("password", {})) else None


def verify_pin(clinician_id: str, pin: str, path: Path | None = None) -> bool:
    record = _load(path or registry_path())["clinicians"].get(clinician_id)
    return bool(record and record.get("active") and _matches(pin, record.get("pin", {})))
