# STORY-012 — Distinguish between encounter-supported and AI-generated information

As a clinician, I want the system to distinguish between encounter-supported and AI-generated information, so that I can make informed decisions based on the source of the data.

**Release:** r4 · Efficiency and Compliance (weeks 8–9)
**Owner:** Clinical Decision Support System
**Blocked by:** STORY-010

## The requirement this satisfies

- **REQ-013** (Functional, must) — The system must distinguish between encounter-supported and AI-generated information.

## How to build it

Develop a user interface component that labels data sources as either encounter-supported or AI-generated. Ensure this distinction is clear in all relevant views.

## Failure paths you must handle

- Data source labeling fails to display.
- Encounter-supported data is mislabeled as AI-generated.
- Audit log entry fails to create.

## Acceptance — your stop condition

Tick each box as it genuinely passes. This file is yours — the platform reads
the same criteria out of `.colaberry/progress.json`, which Claude Code keeps in
step (see the managed block in CLAUDE.md). Ticking something you have not
actually met only misleads you.

- [ ] Given a patient encounter, when information is presented, then the system clearly labels encounter-supported data.
- [ ] Given AI-generated information is presented, when a clinician reviews it, then the system clearly labels it as AI-generated.
- [ ] Trust: The system logs the source of each piece of information presented to the clinician.

When every box above is ticked, stop and show the demo.
