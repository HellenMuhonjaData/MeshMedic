# MeshMedic — Readiness for real patient data (PHI)

**Status: real patient data is NOT approved.** MeshMedic runs on synthetic
(fictional) data only. The server and the web app refuse to start with
`MESHMEDIC_DATA_MODE=real` until every item below is signed off
(see `meshmedic-mcp/runtime_config.py`).

That startup check is a safety catch. **It does not make MeshMedic HIPAA
compliant, and neither does a vendor BAA.** Compliance depends on how the
whole system, the organization and its people handle the data. This
checklist is the minimum evidence needed before anyone *decides* to allow
real data. It is not that decision.

## How sign-off works

1. Each item below is verified by a named, accountable person, against
   current evidence (signed contracts, vendor documentation, test results).
   Nothing is assumed from memory or from this document.
2. The verifications are recorded in `meshmedic-mcp/phi_readiness.signed.json`:

   ```json
   {
     "items": {
       "openai_baa_executed": {"verified_by": "Name, role", "verified_on": "YYYY-MM-DD", "evidence": "contract ref"},
       "...": {}
     }
   }
   ```

3. Real-data mode also needs `MESHMEDIC_AUDIO_RETENTION_DAYS` and
   `MESHMEDIC_RETENTION_POLICY_REF` set from the organization's approved
   retention policy. The synthetic default of 7 days is never used for real data.

## Part A — Vendors (OpenAI for transcription, Anthropic for drafting)

| Item key | What must be verified |
|---|---|
| `openai_baa_executed` | A Business Associate Agreement with OpenAI is executed and covers the services MeshMedic calls. |
| `anthropic_baa_executed` | A Business Associate Agreement with Anthropic is executed and covers the services MeshMedic calls. |
| `openai_eligible_endpoints_and_retention_verified` | The exact OpenAI endpoints and models MeshMedic uses (today: audio transcription) are eligible under that BAA, and the required data-retention configuration (for example zero data retention) is enabled on the account in use. Checked against OpenAI's current documentation and account settings. |
| `anthropic_eligible_endpoints_and_retention_verified` | The exact Anthropic API endpoints, models and features MeshMedic uses (today: Messages API with structured output) are eligible under that BAA, and the required retention configuration is in place. Checked against Anthropic's current documentation and account settings. |
| `vendor_specific_hipaa_requirements_reviewed` | Any vendor-specific conditions (features that are excluded, regions, account types, logging settings) are reviewed and MeshMedic's configuration complies with them. |

## Part B — MeshMedic's own obligations

| Item key | What must be verified |
|---|---|
| `meshmedic_security_risk_assessment_completed` | A documented security risk assessment of MeshMedic and the machine it runs on, with risks and mitigations recorded. |
| `access_control_and_authentication_reviewed` | Only authenticated clinicians can see patient data; roles and permissions are reviewed; signing and authorization are clinician-only (Phase 1). Accounts are provisioned and removed through a defined process. |
| `encryption_at_rest_and_in_transit_verified` | Audio, transcripts, notes and the audit trail are encrypted at rest; every network hop is encrypted in transit (HTTPS to vendors; TLS if the app is ever reachable beyond the local machine). Keys are stored outside the code and the repository. |
| `audit_log_review_procedure_in_place` | Someone is responsible for reviewing the audit trail, on a defined schedule, with a process for what to do about anomalies. |
| `incident_response_plan_in_place` | A breach and incident response plan, including notification duties, exists and names who acts. |
| `backup_and_recovery_tested` | Clinical records and the audit trail are backed up, and a restore has actually been tested. |
| `minimum_necessary_use_reviewed` | Only the data each feature needs is sent to vendors, logged or kept (for example, no patient data in job metadata or structured logs). |
| `workforce_training_completed` | Everyone using or administering MeshMedic is trained on privacy and security procedures. |
| `audio_and_record_retention_policy_approved` | The organization's privacy, legal and clinical-records owners have approved retention periods for raw audio and, separately, for clinical notes. Deleting audio never deletes a clinical note. |
| `privacy_and_security_officer_signoff` | The organization's privacy officer and security officer have reviewed all of the above and approve go-live. |

## What MeshMedic already does (as of Phase 0)

- Synthetic data only, enforced at startup.
- An append-only audit trail of AI output and clinician actions, with who acted (`actor`) on every new entry.
- No patient data in structured logs (checked by `run_compliance_check`).
- The browser cannot call clinician-only actions through the generic tool route.

## What is still missing before real data could even be considered

- Clinician sign-in and server-enforced signing and authorization (Phase 1).
- Encrypted audio storage with retention and logged deletion (Phase 2).
- Encryption at rest for the audit trail and notes (not yet planned in a phase; must be added before real data).
- Backups, monitoring and an operational owner for the machine MeshMedic runs on.
