"""Files no MCP tool may read, whatever roots the connected client declares.

read_transcript_file trusts the client's declared roots. A client could
declare this project folder as a root, so secrets that live here are refused
by name and location as well: the attestation signing key (anything that can
read it can forge clinician attestations), the clinician registry, .env, the
Epic private key and the audit trail itself.
"""

from pathlib import Path

import attestation
import clinician_registry

_PROTECTED_NAMES = {
    ".env",
    "clinicians.json",
    "epic_fhir_private_key.pem",
    "audit_log.jsonl",
    "phi_readiness.signed.json",
    attestation.PRIVATE_KEY_NAME,
    attestation.PUBLIC_KEY_NAME,
}


def is_protected(path: Path) -> bool:
    resolved = Path(path).resolve()
    if resolved.name in _PROTECTED_NAMES or resolved.suffix == ".pem":
        return True
    protected_dirs = [attestation.key_dir(), Path(__file__).parent / ".secrets"]
    if any(resolved.is_relative_to(d.resolve()) for d in protected_dirs):
        return True
    return resolved == clinician_registry.registry_path().resolve()
