# STORY-003 — Flag low-confidence AI recommendations

As a clinician, I want low-confidence AI recommendations flagged, so that I can manually review and correct them.

**Release:** r0 · Initial Skeleton (weeks 0–1)
**Owner:** Clinician
**Blocked by:** nothing — you can start this now

## The requirement this satisfies

- **REQ-008** (Functional, must) — The system must flag low-confidence sections with a visual warning and explanation.

## How to build it

Implement confidence scoring and visual warnings for low-confidence recommendations.

## Failure paths you must handle

- AI fails to flag low-confidence
- Clinician cannot see warning
- Manual input not saved

## Acceptance — your stop condition

Tick each box as it genuinely passes. This file is yours — the platform reads
the same criteria out of `.colaberry/progress.json`, which Claude Code keeps in
step (see the managed block in CLAUDE.md). Ticking something you have not
actually met only misleads you.

- [ ] Given a low-confidence recommendation, when the AI flags it, then the clinician sees a warning icon.
- [ ] Given a flagged recommendation, when the clinician reviews it, then they can manually input corrections.
- [ ] Trust: The system must log flagged recommendations and clinician corrections.

When every box above is ticked, stop and show the demo.
