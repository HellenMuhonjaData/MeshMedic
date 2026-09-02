# STORY-009 — Optimize documentation time

As a clinician, I want to reduce documentation time, so that I can focus more on patient care.

**Release:** r4 · Efficiency and Compliance (weeks 8–9)
**Owner:** Clinician
**Blocked by:** STORY-007, STORY-008

## The requirement this satisfies

- **REQ-012** (Non-functional, must) — The system must reduce documentation time to under 2 minutes of review per note.

## How to build it

Optimize AI algorithms and user interface to minimize review time.

## Failure paths you must handle

- Review time exceeds 2 minutes
- Suggestions not helpful
- System fails to log time

## Acceptance — your stop condition

Tick each box as it genuinely passes. This file is yours — the platform reads
the same criteria out of `.colaberry/progress.json`, which Claude Code keeps in
step (see the managed block in CLAUDE.md). Ticking something you have not
actually met only misleads you.

- [ ] Given a patient encounter, when the AI generates a note, then the review time is under 2 minutes.
- [ ] Given a note review, when it exceeds 2 minutes, then the system provides suggestions to speed up the process.
- [ ] Trust: The system must log documentation time for each encounter.

When every box above is ticked, stop and show the demo.
