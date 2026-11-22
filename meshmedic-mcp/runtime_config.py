"""Runtime configuration that decides whether MeshMedic may touch real patient data.

MESHMEDIC_DATA_MODE
    "synthetic" (default) -- fictional patients only. This is the only mode
    approved today.
    "real" -- refused at startup unless BOTH of these are true:
      * audio retention is set explicitly from the organization's approved
        policy (MESHMEDIC_AUDIO_RETENTION_DAYS and MESHMEDIC_RETENTION_POLICY_REF),
        never the synthetic-data default;
      * the PHI readiness sign-off file (docs/PHI_READINESS.md explains it)
        lists every required item with who verified it and when.
    This gate is a safety catch, not compliance: passing it does not make
    MeshMedic HIPAA compliant (see docs/PHI_READINESS.md).

MESHMEDIC_AUDIO_RETENTION_DAYS
    Days to keep raw encounter audio after the note is signed. Default 7 in
    synthetic mode only. Separate from clinical-note retention: deleting
    audio never deletes a note.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal

DataMode = Literal["synthetic", "real"]

SYNTHETIC_AUDIO_RETENTION_DAYS = 7
READINESS_FILE = Path(__file__).parent / "phi_readiness.signed.json"

# Every item must be verified by a named person before real PHI is processed.
REQUIRED_READINESS_ITEMS = (
    "openai_baa_executed",
    "anthropic_baa_executed",
    "openai_eligible_endpoints_and_retention_verified",
    "anthropic_eligible_endpoints_and_retention_verified",
    "vendor_specific_hipaa_requirements_reviewed",
    "meshmedic_security_risk_assessment_completed",
    "access_control_and_authentication_reviewed",
    "encryption_at_rest_and_in_transit_verified",
    "audit_log_review_procedure_in_place",
    "incident_response_plan_in_place",
    "backup_and_recovery_tested",
    "minimum_necessary_use_reviewed",
    "workforce_training_completed",
    "audio_and_record_retention_policy_approved",
    "privacy_and_security_officer_signoff",
)


class ConfigError(Exception):
    """MeshMedic must not start with this configuration."""


@dataclass(frozen=True)
class RuntimeConfig:
    data_mode: DataMode
    audio_retention_days: int
    retention_policy_ref: str | None


def _retention_days(raw: str) -> int:
    try:
        days = int(raw)
    except ValueError as exc:
        raise ConfigError(f"MESHMEDIC_AUDIO_RETENTION_DAYS must be a whole number of days, got {raw!r}.") from exc
    if days < 0:
        raise ConfigError("MESHMEDIC_AUDIO_RETENTION_DAYS cannot be negative.")
    return days


def readiness_problems(readiness_path: Path) -> list[str]:
    """Why the readiness sign-off file does not (yet) allow real PHI. Empty = complete."""
    if not readiness_path.exists():
        return [f"readiness sign-off file {readiness_path.name} does not exist"]
    try:
        data = json.loads(readiness_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"readiness sign-off file could not be read ({type(exc).__name__})"]
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, dict):
        return ["readiness sign-off file has no 'items' object"]
    problems = []
    for item in REQUIRED_READINESS_ITEMS:
        entry = items.get(item)
        if not isinstance(entry, dict) or not entry.get("verified_by") or not entry.get("verified_on"):
            problems.append(f"{item} is not signed off (needs verified_by and verified_on)")
            continue
        try:
            date.fromisoformat(str(entry["verified_on"]))
        except ValueError:
            problems.append(f"{item} has an invalid verified_on date")
    return problems


def load_config(env: Mapping[str, str], readiness_path: Path = READINESS_FILE) -> RuntimeConfig:
    mode = (env.get("MESHMEDIC_DATA_MODE") or "synthetic").strip().lower()
    raw_days = (env.get("MESHMEDIC_AUDIO_RETENTION_DAYS") or "").strip()

    if mode == "synthetic":
        days = _retention_days(raw_days) if raw_days else SYNTHETIC_AUDIO_RETENTION_DAYS
        return RuntimeConfig("synthetic", days, None)

    if mode != "real":
        raise ConfigError(f"MESHMEDIC_DATA_MODE must be 'synthetic' or 'real', got {mode!r}.")

    if not raw_days:
        raise ConfigError(
            "Real-data mode needs MESHMEDIC_AUDIO_RETENTION_DAYS set explicitly from the "
            "organization's approved retention policy; the synthetic default is not used."
        )
    policy_ref = (env.get("MESHMEDIC_RETENTION_POLICY_REF") or "").strip()
    if not policy_ref:
        raise ConfigError(
            "Real-data mode needs MESHMEDIC_RETENTION_POLICY_REF naming the approved policy document."
        )
    problems = readiness_problems(readiness_path)
    if problems:
        raise ConfigError(
            "Real patient data is not approved for this installation: " + "; ".join(problems)
            + ". See docs/PHI_READINESS.md."
        )
    return RuntimeConfig("real", _retention_days(raw_days), policy_ref)
