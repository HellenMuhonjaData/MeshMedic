"""Compliance controls added in Phase 2: audit-trail integrity, and no autonomous clinical actions.

Both are pure functions over the audit trail; tools_compliance.run_compliance_check
runs them alongside the original controls.
"""

from pathlib import Path

import audit_actor
import audit_store
import referral_letters
from models import ComplianceControlResult

# Actions that carry clinical authority. None of them may ever be recorded as
# done by the AI or by MeshMedic itself (background processing included).
CLINICIAN_ONLY_ACTIONS = frozenset(
    {
        "sign_note",
        "approve_note",
        "reject_note",
        "approve_codes",
        "reject_codes",
        "order_test",
        "authorize_orders",
        "address_care_gap",
        referral_letters.APPROVE_ACTION,
        referral_letters.SEND_ACTION,
        referral_letters.VOID_ACTION,
    }
)


def check_audit_integrity(path: Path) -> tuple[ComplianceControlResult, list[dict] | None]:
    """Verify the chain, head and every entry's authenticated decryption.

    Returns the control result and, when the trail is readable, its entries
    (so the other controls can run). An unreadable trail fails this control
    instead of crashing the compliance check.
    """
    problems = audit_store.chain_problems(path)
    entries = None
    if not problems:
        try:
            entries = audit_store.read_entries(path)
        except audit_store.AuditIntegrityError as exc:
            problems = [str(exc)]
    passed = not problems
    return (
        ComplianceControlResult(
            control_id="REQ-025-audit-trail-integrity",
            description=(
                "The audit trail is append-only and intact: every encrypted entry decrypts and "
                "authenticates, chains to the entry before it, sequence numbers have no gaps, "
                "records from before encryption are unchanged, and nothing is missing from the end."
            ),
            passed=passed,
            detail=f"{len(entries)} entries verified." if passed else "; ".join(problems[:5]),
        ),
        entries,
    )


def check_no_autonomous_clinical_actions(entries: list[dict]) -> ComplianceControlResult:
    """REQ-019 / REQ-029: no clinician-only action was ever done by the AI or the system."""
    violations = [
        f"{e.get('action')} at {e.get('timestamp')} was recorded as done by {audit_actor.entry_actor(e)['type']}"
        for e in entries
        if e.get("action") in CLINICIAN_ONLY_ACTIONS and audit_actor.entry_actor(e)["type"] in ("ai", "system")
    ]
    passed = not violations
    return ComplianceControlResult(
        control_id="REQ-019-no-autonomous-clinical-actions",
        description=(
            "No sign-off, approval, authorization, order or sending in the audit trail was done "
            "by the AI or by background processing."
        ),
        passed=passed,
        detail="No clinician-only action was done by the AI or the system." if passed else "; ".join(violations[:5]),
    )


def unreadable(control_id: str, description: str) -> ComplianceControlResult:
    """A control that could not run because the audit trail failed its integrity check."""
    return ComplianceControlResult(
        control_id=control_id, description=description, passed=False,
        detail="Not evaluated: the audit trail failed its integrity check.",
    )
