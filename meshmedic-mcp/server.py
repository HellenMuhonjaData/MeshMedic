"""MeshMedic MCP server entry point.

The tools live in tools_*.py (split out of this file in Phase 0, with no
behaviour change); importing each module registers its tools on the shared
`mcp` instance from app_core. This file keeps every name it exported before
the split, so existing imports such as `server.approve_encounter_note` keep
working. Code that needs to redirect the audit log (tests) patches
`app_core.AUDIT_LOG_PATH`; to replace a dependency, patch it on the module
that uses it (for example `tools_github.get_issue_or_pr`).
"""

import os
from typing import Literal

import app_core
import audit_queries
import models
import runtime_config
import tools_codes
import tools_compliance
import tools_gap_priority
import tools_gaps
import tools_github
import tools_insight
import tools_note_drafts
import tools_note_review
import tools_note_signoff
import tools_orders
import tools_patient
import tools_referrals
import tools_suggestions
from app_core import _logger, mcp
from logging_utils import SERVER_STARTED, log_event, new_correlation_id

# Re-export every name the pre-split server.py defined or imported.
for _module in (
    app_core,
    models,
    audit_queries,
    tools_codes,
    tools_compliance,
    tools_gap_priority,
    tools_gaps,
    tools_github,
    tools_insight,
    tools_note_drafts,
    tools_note_review,
    tools_note_signoff,
    tools_orders,
    tools_patient,
    tools_referrals,
    tools_suggestions,
):
    globals().update({k: v for k, v in vars(_module).items() if not k.startswith("__")})
del _module


@mcp.prompt(name="prepare-encounter-note")
def prepare_encounter_note(
    patient_hint: str = "",
    ehr_system: Literal["epic", "oracle_health"] = "epic",
) -> str:
    """Clinician-triggered workflow: resolve the patient, read their chart,
    draft a structured encounter note. Returns text; a multi-turn version of
    this workflow could instead return list[Message] (e.g. a user turn plus
    a pre-filled assistant turn) -- not needed here since one instruction
    block covers the whole flow."""
    return f"""You are preparing to document a clinical encounter. Two things are available to you: the search_ehr_patient tool and the patient-chart resource (ehr://{ehr_system}/patient/{{patient_id}}/chart). Use them in this order, and do not skip ahead.

Known so far:
- Patient hint: {patient_hint or "(none given -- nothing to go on but this conversation)"}
- EHR system to check first: {ehr_system}

1. Confirm identity. If the hint above doesn't already give you a confirmed patient_id, call search_ehr_patient with ehr_system={ehr_system} and whatever you have -- the MRN if you have it, otherwise last_name together with date_of_birth. Do not invent either value.
2. Once you have exactly one confirmed patient_id, read the patient-chart resource for that patient_id before drafting anything, so the note is grounded in the real record rather than the conversation alone.
3. Draft the note as: patient identity (name, MRN, DOB), a one-paragraph encounter summary, and status: draft -- never mark a note final. A clinician must review and approve it before it counts as anything (see REQ-005 / REQ-014).
4. Assess your own confidence (0.0-1.0) in how well the transcript and chart actually support the note you drafted -- vague symptoms, a short or ambiguous transcript, or details the chart doesn't corroborate all lower it. Pass this as `confidence` to generate_encounter_note. If it's below 0.7, you must also pass `confidence_reason` explaining specifically what's uncertain (REQ-008) -- do not round confidence up just to skip writing a reason.

Handle these three situations explicitly, the way you would explain an edge case to a colleague -- do not guess past any of them:
- Information missing: search_ehr_patient has neither an MRN nor a last_name+date_of_birth to work with. Say plainly what's missing and stop; do not fabricate an identifier.
- Input ambiguous: search_ehr_patient returns more than one candidate. List them (name, DOB, MRN) and ask which one is meant; do not pick for the clinician.
- Nothing to report: search_ehr_patient returns zero matches. Say plainly that no patient was found in {ehr_system}, suggest checking the details or trying the other EHR system, and stop -- do not invent a patient or draft a note without one."""


if __name__ == "__main__":
    try:
        runtime_config.load_config(os.environ)
    except runtime_config.ConfigError as exc:
        raise SystemExit(f"MeshMedic will not start: {exc}") from exc
    # STDIO, single-user, single-process -- per docs/TRANSPORT_DECISION.md.
    # This process assumes exactly one connected client for its whole
    # lifetime (matching .mcp.json's spawn-per-session launch) and is not
    # safe to run as multiple concurrent instances: audit_log.jsonl is a
    # single-writer local file with no locking, and roots enforcement /
    # MCP sampling both address "the currently connected client," which
    # only means something with one client per process. Do not scale this
    # by running more copies of server.py without redoing that design.
    log_event(
        _logger,
        "info",
        SERVER_STARTED,
        new_correlation_id(),
        transport="stdio",
        state_model="single-user-single-process",
    )
    mcp.run(transport="stdio")
