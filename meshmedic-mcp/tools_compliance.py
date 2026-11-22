"""Compliance controls over the audit trail and structured logs.

Split out of server.py (Phase 0, no behaviour change).
"""

import ast
from datetime import datetime, timezone
from pathlib import Path

import ai_suggestions
import provenance
import referral_letters
from app_core import _append_audit_entry, _logger, mcp
from audit_queries import _all_audit_entries
from logging_utils import TOOL_COMPLETED, TOOL_STARTED, log_event, new_correlation_id
from models import ComplianceCheckResult, ComplianceControlResult


def _check_audit_completeness(entries: list[dict]) -> ComplianceControlResult:
    """REQ-006: every clinician decision in the audit trail has a matching
    AI-drafted entry -- no decision exists whose note/suggestion/gap was
    never actually recorded as drafted. This is already structurally
    guaranteed by each decision tool's own _find_note/_find_suggestion/
    _find_gap lookup (a decision can't be written for an id that lookup
    doesn't find) -- this control re-verifies that guarantee held over the
    real, persisted log, which is what would catch a corrupted or
    hand-edited audit file that the code-level guarantee alone can't see."""
    drafted: set[tuple[str, str]] = set()
    for entry in entries:
        if entry["action"] == "generate_note":
            drafted.add(("note", entry["note_id"]))
        elif entry["action"] == "suggest_codes":
            drafted.add(("suggestion", entry["suggestion_id"]))
        elif entry["action"] == "identify_care_gap":
            drafted.add(("gap", entry["gap_id"]))
        elif entry["action"] == referral_letters.GENERATE_ACTION:
            drafted.add(("letter", entry["letter_id"]))
        elif entry["action"] == referral_letters.APPROVE_ACTION:
            drafted.add(("approved_letter", entry["letter_id"]))

    orphaned: list[str] = []
    for entry in entries:
        action = entry["action"]
        if (
            action in ("approve_note", "reject_note")
            and ("note", entry["note_id"]) not in drafted
        ):
            orphaned.append(
                f"{action} for note_id={entry['note_id']!r} has no generate_note entry"
            )
        elif (
            action in ("approve_codes", "reject_codes")
            and ("suggestion", entry["suggestion_id"]) not in drafted
        ):
            orphaned.append(
                f"{action} for suggestion_id={entry['suggestion_id']!r} has no suggest_codes entry"
            )
        elif action == "address_care_gap" and ("gap", entry["gap_id"]) not in drafted:
            orphaned.append(
                f"address_care_gap for gap_id={entry['gap_id']!r} has no identify_care_gap entry"
            )
        elif (
            action == referral_letters.APPROVE_ACTION
            and ("letter", entry["letter_id"]) not in drafted
        ):
            orphaned.append(
                f"{action} for letter_id={entry['letter_id']!r} has no "
                f"{referral_letters.GENERATE_ACTION} entry"
            )
        elif (
            action == referral_letters.SEND_ACTION
            and ("approved_letter", entry["letter_id"]) not in drafted
        ):
            orphaned.append(
                f"{action} for letter_id={entry['letter_id']!r} has no "
                f"{referral_letters.APPROVE_ACTION} entry"
            )
        elif action == provenance.PRESENT_ACTION:
            if ("note", entry["note_id"]) not in drafted:
                orphaned.append(
                    f"{action} for note_id={entry['note_id']!r} has no generate_note entry"
                )
            orphaned.extend(
                f"{action} for note_id={entry['note_id']!r} {problem}"
                for problem in provenance.present_entry_problems(entry)
            )
        elif action == referral_letters.VOID_ACTION:
            if ("letter", entry["letter_id"]) not in drafted:
                orphaned.append(
                    f"{action} for letter_id={entry['letter_id']!r} has no "
                    f"{referral_letters.GENERATE_ACTION} entry"
                )
            if not entry.get("void_reason") or not entry.get("voided_by"):
                orphaned.append(
                    f"{action} for letter_id={entry['letter_id']!r} is missing "
                    "voided_by or void_reason"
                )

    orphaned.extend(ai_suggestions.orphan_references(entries))

    passed = not orphaned
    return ComplianceControlResult(
        control_id="REQ-006-audit-completeness",
        description=(
            "Every clinician decision (approve/reject a note or code set, address a care "
            "gap, approve a referral letter) in the audit trail has a matching drafted entry, "
            "and every presentation of information records a valid source label per item."
        ),
        passed=passed,
        detail="No orphaned decisions found." if passed else "; ".join(orphaned),
    )


def _check_single_decision_integrity(entries: list[dict]) -> ComplianceControlResult:
    """REQ-014: no note or code-suggestion ever accumulates more than one
    recorded clinician decision. Same re-verification purpose as
    _check_audit_completeness -- approve_encounter_note/reject_encounter_note
    (and their _codes equivalents) already refuse a conflicting second
    decision via ToolError, so this checks that guarantee actually held over
    the persisted log rather than trusting the code path alone."""
    note_decisions: dict[str, int] = {}
    suggestion_decisions: dict[str, int] = {}
    letter_decisions: dict[str, int] = {}
    letter_sends: dict[str, int] = {}
    letter_voids: dict[str, int] = {}
    for entry in entries:
        action = entry["action"]
        if action in ("approve_note", "reject_note"):
            note_decisions[entry["note_id"]] = (
                note_decisions.get(entry["note_id"], 0) + 1
            )
        elif action in ("approve_codes", "reject_codes"):
            suggestion_decisions[entry["suggestion_id"]] = (
                suggestion_decisions.get(entry["suggestion_id"], 0) + 1
            )
        elif action == referral_letters.APPROVE_ACTION:
            letter_decisions[entry["letter_id"]] = (
                letter_decisions.get(entry["letter_id"], 0) + 1
            )
        elif action == referral_letters.SEND_ACTION:
            letter_sends[entry["letter_id"]] = (
                letter_sends.get(entry["letter_id"], 0) + 1
            )
        elif action == referral_letters.VOID_ACTION:
            letter_voids[entry["letter_id"]] = (
                letter_voids.get(entry["letter_id"], 0) + 1
            )

    violations = [
        f"note_id={note_id!r} has {count} decisions"
        for note_id, count in note_decisions.items()
        if count > 1
    ]
    violations += [
        f"suggestion_id={suggestion_id!r} has {count} decisions"
        for suggestion_id, count in suggestion_decisions.items()
        if count > 1
    ]
    violations += [
        f"letter_id={letter_id!r} has {count} approvals"
        for letter_id, count in letter_decisions.items()
        if count > 1
    ]
    violations += [
        f"letter_id={letter_id!r} was marked sent {count} times"
        for letter_id, count in letter_sends.items()
        if count > 1
    ]
    violations += [
        f"letter_id={letter_id!r} was voided {count} times"
        for letter_id, count in letter_voids.items()
        if count > 1
    ]
    violations += [
        f"letter_id={letter_id!r} was both voided and marked sent"
        for letter_id in letter_voids
        if letter_id in letter_sends
    ]

    passed = not violations
    return ComplianceControlResult(
        control_id="REQ-014-single-decision-integrity",
        description="No note, code-suggestion set, or referral letter has more than one recorded clinician decision.",
        passed=passed,
        detail="Every note, suggestion set, and referral letter has at most one decision."
        if passed
        else "; ".join(violations),
    )


def _check_security_incident_logging(entries: list[dict]) -> ComplianceControlResult:
    """REQ-011: every entry explicitly tagged `security_incident: true` in
    the audit trail (currently written only by read_transcript_file's
    roots-guard denial -- the one real unauthorized-access boundary this
    server enforces) is a complete record: timestamp, action, outcome, and
    error_class all present, not just a bare tag with no context a reviewer
    could act on. Keying off this explicit tag, rather than inferring from
    `error_class == "AccessDenied"` alone, matters because AccessDenied is
    also used for ordinary business-rule conflicts elsewhere in this file
    (e.g. approve_encounter_note's "already decided" case) that are not
    security incidents -- those don't reach the audit trail today, but the
    explicit tag keeps this control correct even if one someday does."""
    incidents = [entry for entry in entries if entry.get("security_incident") is True]
    incomplete = [
        entry
        for entry in incidents
        if not entry.get("timestamp")
        or not entry.get("action")
        or entry.get("outcome") != "failure"
        or not entry.get("error_class")
    ]

    passed = not incomplete
    if passed:
        detail = f"{len(incidents)} security-incident event(s) in the audit trail, each fully logged."
    else:
        detail = f"{len(incomplete)} of {len(incidents)} security-incident event(s) are missing a required field."
    return ComplianceControlResult(
        control_id="REQ-011-security-incident-logging",
        description="Every security-incident event (tagged security_incident=true) carries timestamp, action, outcome, and error_class.",
        passed=passed,
        detail=detail,
    )


# Keyword argument names log_event(...) must never carry (REQ-011): raw
# patient identity, free-text clinical content, or anything else
# logging_utils.log_event's own contract calls out as forbidden --
# "identifiers, counts, and durations only... never... a raw patient record
# (name, DOB, MRN, note/transcript text)". Opaque ids already logged
# throughout this file today (note_id, patient_id, suggestion_id, gap_id,
# ehr_system) are deliberately NOT in this set -- REQ-011 forbids raw PHI
# content, not the identifiers this codebase already treats as safe to log.
FORBIDDEN_LOG_CONTEXT_KEYS = frozenset(
    {
        "transcript",
        "note_text",
        "edited_note_text",
        "first_name",
        "last_name",
        "date_of_birth",
        "mrn",
        "patient_name",
        "feedback",
        "confidence_reason",
        "claimed_excerpt",
        "matched_text",
        "resolution",
        "description",
        "ranking_text",
        "letter_text",
        "edited_letter_text",
        "referral_reason",
        "void_reason",
    }
)


def _log_event_violations(source: str) -> list[str]:
    """Static scan: every log_event(...) call site in `source`, checked for
    a keyword argument name in FORBIDDEN_LOG_CONTEXT_KEYS. Returns one
    human-readable violation string per bad call site (empty if none).
    AST-based, not a text/regex search, so a forbidden name appearing only
    as a string *value* elsewhere (e.g. tool="note_text") or in a comment
    can't produce a false positive -- only an actual keyword-argument name
    at an actual log_event(...) call site counts."""
    tree = ast.parse(source)
    violations = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "log_event"
        ):
            continue
        for kw in node.keywords:
            if kw.arg in FORBIDDEN_LOG_CONTEXT_KEYS:
                violations.append(
                    f"log_event(...) at line {node.lineno} passes forbidden key {kw.arg!r}"
                )
    return violations


def _check_no_phi_in_structured_logs() -> ComplianceControlResult:
    """REQ-011: no log_event(...) call site in this server's own source
    passes a keyword argument that could carry raw PHI to the structured
    stderr stream -- the one log channel in this system that is NOT
    supposed to carry it (unlike audit_log.jsonl, which legitimately does,
    by design, as REQ-006's source of truth). Unlike this file's other three
    controls, this one is static (checks source code, not audit_log.jsonl)
    -- it catches a violation the moment it's written, before it ever logs a
    single real byte of PHI, rather than after the fact."""
    violations = _log_event_violations(Path(__file__).read_text(encoding="utf-8"))
    passed = not violations
    return ComplianceControlResult(
        control_id="REQ-011-no-phi-in-structured-logs",
        description="No log_event(...) call site passes a keyword argument that could carry raw PHI.",
        passed=passed,
        detail="No forbidden keys found in any log_event(...) call."
        if passed
        else "; ".join(violations),
    )


@mcp.tool()
def run_compliance_check() -> ComplianceCheckResult:
    """
    Evaluate this server's audit trail against a fixed, named set of internal
    compliance controls (REQ-011 / REQ-018) and record the check itself in
    the audit trail (REQ-006 / STORY-010's "log all compliance checks").

    This does not claim certification against any external standard (HIPAA,
    HITRUST, or otherwise) -- no such standard is named anywhere in this
    project's requirements, and claiming one without doing the actual
    compliance work behind it would be a false claim, not a shortcut. What it
    does check, honestly: four controls this codebase can actually verify --
    audit completeness (REQ-006), single-decision integrity (REQ-014),
    security-incident logging completeness (REQ-011), and no PHI reaching
    the structured stderr log stream (REQ-011). A "compliance audit" against
    this tool means reviewing these four named controls and their pass/fail
    detail, not an opaque yes/no.

    Three controls are checked from `audit_log.jsonl` as it actually is,
    never assumed to pass because the code that writes it looks correct --
    a control failing here means the persisted data disagrees with what the
    code guarantees (a real regression, or a hand-edited/corrupted log), and
    is reported as such rather than hidden. The fourth (no-PHI-in-logs) is a
    static check of this file's own source instead, since there's nothing
    to replay for "what wasn't logged." `all_passed` is only true if every
    control passed; a partial pass is reported in full, not rounded up.
    """
    correlation_id = new_correlation_id()
    log_event(
        _logger,
        "info",
        TOOL_STARTED,
        correlation_id,
        tool="run_compliance_check",
    )

    entries = _all_audit_entries()
    controls = [
        _check_audit_completeness(entries),
        _check_single_decision_integrity(entries),
        _check_security_incident_logging(entries),
        _check_no_phi_in_structured_logs(),
    ]
    all_passed = all(control.passed for control in controls)
    checked_at = datetime.now(timezone.utc).isoformat()

    _append_audit_entry(
        {
            "timestamp": checked_at,
            "action": "compliance_check",
            "all_passed": all_passed,
            "controls": [control.model_dump() for control in controls],
        }
    )
    log_event(
        _logger,
        "info",
        TOOL_COMPLETED,
        correlation_id,
        tool="run_compliance_check",
        outcome="success",
        all_passed=all_passed,
    )

    return ComplianceCheckResult(
        checked_at=checked_at, controls=controls, all_passed=all_passed
    )
