"""Which MCP tools the browser may call through the bridge's generic route.

POST /api/tools/<name> forwards straight to the MCP server. That route must
never become a way around clinician authorization, so it is an allowlist:
a tool not named here is refused with 403, including every tool added in
future until someone deliberately lists it.

Clinician-attested actions (signing a note, authorizing orders, final code
approval, approving a referral for sending -- Phase 1 onwards) must NEVER be
added here. They go through authenticated bridge routes that obtain a
clinician attestation first.
"""

PASSTHROUGH_TOOLS = frozenset(
    {
        "search_ehr_patient",
        "fetch_patient_from_ehr",
        "read_transcript_file",
        "generate_encounter_note",
        "edit_encounter_note",
        "approve_encounter_note",
        "reject_encounter_note",
        "suggest_codes",
        "approve_codes",
        "reject_codes",
        "identify_care_gaps",
        "address_care_gap",
        "prioritize_care_gaps",
        "record_ai_suggestions",
        "order_test",
        "record_test_result",
        "get_pending_items_for_patient",
        "generate_referral_letter",
        "approve_referral_letter",
        "list_referral_letters",
        "mark_referral_letter_sent",
        "void_referral_letter",
        "request_citation",
        "label_information_sources",
        "get_activity_summary",
        "get_attention_items",
        "check_github_issue_status",
        "run_compliance_check",
    }
)

# Named here so a future edit that adds one to PASSTHROUGH_TOOLS fails tests.
ATTESTED_TOOLS = frozenset(
    {"sign_encounter_note", "authorize_orders", "finalize_code_review", "approve_referral_for_sending"}
)


def is_passthrough_allowed(tool_name: str) -> bool:
    return tool_name in PASSTHROUGH_TOOLS and tool_name not in ATTESTED_TOOLS
