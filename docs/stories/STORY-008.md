# STORY-008 — Indicate AI recommendation confidence

As a clinician, I want to see the confidence level of AI recommendations, so that I can assess their reliability.

**Release:** r3 · Recommendation Transparency (weeks 6–7)
**Owner:** Clinician
**Blocked by:** STORY-006

## The requirement this satisfies

- **REQ-008** (Functional, must) — The system must flag low-confidence sections with a visual warning and explanation.

## How to build it

Develop confidence scoring and display mechanisms for AI recommendations.

## Failure paths you must handle

- Confidence score not displayed
- Incorrect confidence score
- System fails to log interactions

## Acceptance — your stop condition

Tick each box as it genuinely passes. This file is yours — the platform reads
the same criteria out of `.colaberry/progress.json`, which Claude Code keeps in
step (see the managed block in CLAUDE.md). Ticking something you have not
actually met only misleads you.

- [ ] Given an AI recommendation, when the system displays it, then it shows a confidence score.
- [ ] Given a low-confidence recommendation, when the clinician reviews it, then they see an explanation for the low confidence.
- [ ] Trust: The system must log all confidence scores and clinician interactions.

When every box above is ticked, stop and show the demo.
