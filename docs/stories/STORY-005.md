# STORY-005 — Identify and flag care gaps

As a clinician, I want the system to identify care gaps, so that I can address them proactively.

**Release:** r1 · Advanced Coding and Care Gap (weeks 2–3)
**Owner:** Clinician
**Blocked by:** STORY-001

## The requirement this satisfies

- **REQ-003** (Functional, must) — The system must identify potential gaps in care from encounter data.

## How to build it

Implement care gap identification logic and ensure logging in the audit trail.

## Failure paths you must handle

- Care gaps not identified
- False positives in care gap identification
- Audit trail not logging actions

## Acceptance — your stop condition

Tick each box as it genuinely passes. This file is yours — the platform reads
the same criteria out of `.colaberry/progress.json`, which Claude Code keeps in
step (see the managed block in CLAUDE.md). Ticking something you have not
actually met only misleads you.

- [ ] Given encounter data, when the AI identifies a care gap, then it flags it for clinician review.
- [ ] Given a flagged care gap, when the clinician addresses it, then the action is logged in the audit trail.
- [ ] Trust: The system must log identified care gaps and clinician actions.

When every box above is ticked, stop and show the demo.
