# STORY-010 — Ensure compliance with healthcare standards

As a compliance officer, I want the system to comply with healthcare standards, so that we avoid legal issues.

**Release:** r4 · Efficiency and Compliance (weeks 8–9)
**Owner:** Compliance Officer
**Blocked by:** STORY-007, STORY-008

## The requirement this satisfies

- **REQ-011** (Safety, must) — The system must protect PHI and ensure privacy and security compliance.
- **REQ-018** (Safety, must) — The system must support healthcare compliance standards relevant to clinical documentation.

## How to build it

Implement security and compliance checks throughout the system.

## Failure paths you must handle

- Non-compliance detected
- Security breach occurs
- System fails to log incidents

## Acceptance — your stop condition

Tick each box as it genuinely passes. This file is yours — the platform reads
the same criteria out of `.colaberry/progress.json`, which Claude Code keeps in
step (see the managed block in CLAUDE.md). Ticking something you have not
actually met only misleads you.

- [ ] Given a compliance audit, when the system is reviewed, then it meets all relevant standards.
- [ ] Given a security breach attempt, when the system detects it, then it prevents unauthorized access.
- [ ] Trust: The system must log all compliance checks and security incidents.

When every box above is ticked, stop and show the demo.
