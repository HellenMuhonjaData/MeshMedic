# STORY-007 — Provide source citations for AI recommendations

As a clinician, I want source citations for AI recommendations, so that I can verify their accuracy.

**Release:** r3 · Recommendation Transparency (weeks 6–7)
**Owner:** Clinician
**Blocked by:** STORY-006

## The requirement this satisfies

- **REQ-007** (Functional, must) — The system must provide clear explanations for its recommendations, including source citations.

## How to build it

Implement source citation feature linking recommendations to transcript sections.

## Failure paths you must handle

- Citation not available
- Incorrect citation provided
- System fails to log citation requests

## Acceptance — your stop condition

Tick each box as it genuinely passes. This file is yours — the platform reads
the same criteria out of `.colaberry/progress.json`, which Claude Code keeps in
step (see the managed block in CLAUDE.md). Ticking something you have not
actually met only misleads you.

- [ ] Given an AI recommendation, when the clinician requests a source citation, then the system highlights the relevant transcript section.
- [ ] Given a citation request, when the system cannot provide it, then it explains the reason.
- [ ] Trust: The system must log all citation requests and responses.

When every box above is ticked, stop and show the demo.
