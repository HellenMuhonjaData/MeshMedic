# STORY-002 — Maintain auditable history of actions

As a compliance officer, I want an auditable history of AI and clinician actions, so that I can ensure accountability and traceability.

**Release:** r0 · Initial Skeleton (weeks 0–1)
**Owner:** Compliance Officer
**Blocked by:** nothing — you can start this now

## The requirement this satisfies

- **REQ-006** (Safety, must) — The system must maintain an auditable history of AI recommendations and clinician actions.

## How to build it

Develop audit logging for all AI recommendations and clinician interactions.

## Failure paths you must handle

- Audit trail not capturing actions
- Data integrity issues in audit logs
- Unauthorized access to audit logs

## Acceptance — your stop condition

Tick each box as it genuinely passes. This file is yours — the platform reads
the same criteria out of `.colaberry/progress.json`, which Claude Code keeps in
step (see the managed block in CLAUDE.md). Ticking something you have not
actually met only misleads you.

- [ ] Given an AI recommendation, when a clinician approves it, then the action is logged in the audit trail.
- [ ] Given a clinician edits a recommendation, when they save changes, then the edit is logged in the audit trail.
- [ ] Trust: The audit trail must reflect all AI recommendations and clinician actions.

When every box above is ticked, stop and show the demo.
