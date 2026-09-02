# STORY-001 — Generate and approve AI clinical notes

As a clinician, I want to generate and approve AI clinical notes, so that I can ensure accuracy and control over patient documentation.

**Release:** r0 · Initial Skeleton (weeks 0–1)
**Owner:** Clinician
**Blocked by:** nothing — you can start this now

## The requirement this satisfies

- **REQ-001** (Functional, must) — The system must process patient encounter conversations to generate structured clinical notes.
- **REQ-005** (Functional, must) — The system must allow clinicians to review, edit, approve, or reject AI-generated notes, codes, and recommendations.
- **REQ-006** (Safety, must) — The system must maintain an auditable history of AI recommendations and clinician actions.
- **REQ-014** (Safety, must) — The system must not execute clinical decisions or actions without clinician approval.

## How to build it

Implement note generation using audio and transcript data. Ensure clinician approval is logged in the audit trail.

## Failure paths you must handle

- AI generates incorrect note
- Clinician unable to approve note
- Audit trail fails to log action

## Acceptance — your stop condition

Tick each box as it genuinely passes. This file is yours — the platform reads
the same criteria out of `.colaberry/progress.json`, which Claude Code keeps in
step (see the managed block in CLAUDE.md). Ticking something you have not
actually met only misleads you.

- [ ] Given a patient encounter, when the AI generates a note, then the clinician can review and approve it.
- [ ] Given a clinician rejects a note, when they provide feedback, then the system logs the action in the audit trail.
- [ ] Trust: The audit trail must show the AI's note and the clinician's approval or rejection.

When every box above is ticked, stop and show the demo.
