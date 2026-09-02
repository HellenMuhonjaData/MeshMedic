# STORY-004 — Suggest ICD-10 and CPT codes

As a clinician, I want the system to suggest ICD-10 and CPT codes, so that I can efficiently complete billing documentation.

**Release:** r1 · Advanced Coding and Care Gap (weeks 2–3)
**Owner:** Clinician
**Blocked by:** STORY-001

## The requirement this satisfies

- **REQ-002** (Functional, must) — The system must suggest appropriate ICD-10 and CPT codes based on encounter data.

## How to build it

Develop code suggestion algorithms based on encounter data and integrate with the audit trail.

## Failure paths you must handle

- Incorrect code suggestions
- Clinician unable to approve codes
- Audit trail not logging codes

## Acceptance — your stop condition

Tick each box as it genuinely passes. This file is yours — the platform reads
the same criteria out of `.colaberry/progress.json`, which Claude Code keeps in
step (see the managed block in CLAUDE.md). Ticking something you have not
actually met only misleads you.

- [ ] Given encounter data, when the AI suggests codes, then the clinician can review and approve them.
- [ ] Given a code suggestion, when the clinician rejects it, then they can provide feedback for improvement.
- [ ] Trust: The audit trail must log code suggestions and clinician decisions.

When every box above is ticked, stop and show the demo.
