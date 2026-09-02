# STORY-011 — Prepare follow-up documentation for clinician review

As a clinician, I want the system to prepare follow-up documentation such as referral letters, so that I can review and send them to patients.

**Release:** r4 · Efficiency and Compliance (weeks 8–9)
**Owner:** Clinical Documentation System
**Blocked by:** STORY-010

## The requirement this satisfies

- **REQ-004** (Functional, must) — The system must prepare follow-up documentation such as referral letters for clinician review.

## How to build it

Implement a module to generate referral letters based on encounter data. Ensure the clinician can review and approve the document before it is finalized.

## Failure paths you must handle

- Document generation fails due to missing encounter data.
- Clinician approval fails to save.
- Audit log entry fails to create.

## Acceptance — your stop condition

Tick each box as it genuinely passes. This file is yours — the platform reads
the same criteria out of `.colaberry/progress.json`, which Claude Code keeps in
step (see the managed block in CLAUDE.md). Ticking something you have not
actually met only misleads you.

- [ ] Given a completed patient encounter, when the system generates follow-up documentation, then the clinician can review and approve the referral letter.
- [ ] Given a clinician has reviewed a referral letter, when they approve it, then the system marks it as ready to send.
- [ ] Trust: The system logs the creation and approval of each referral letter for audit purposes.

When every box above is ticked, stop and show the demo.
