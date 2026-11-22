# STORY-016 — Structured notes and review-by-exception

As a clinician, I want structured notes and review-by-exception, so that MeshMedic prepares documentation in the background while I stay in control of every clinical decision.

**Release:** Async copilot · Phase 3
**Owner:** MeshMedic
**Blocked by:** STORY-015

## The requirement this satisfies

- **REQ-022** — Processed encounters enter a clinician review queue that shows attention items (missing information, contradictions, unsupported statements) first, keeps the full note visible, and never allows bulk signing.
- **REQ-026** — AI-drafted notes are structured into HPI, ROS, Examination and Assessment/Plan, and flag missing or unclear information instead of inventing it.
- **REQ-031** — Model-generated confidence is never treated as validated clinical accuracy and never removes or shortens clinician review.

## How to build it

HPI, ROS, Examination and Assessment/Plan sections with missing and unclear flags; server-side flagging of unsupported sentences; attention items that sort the queue and must be resolved before signing.

## Failure paths you must handle

- The AI invents an examination finding.
- A section is empty.
- Older notes without sections.

## Acceptance — your stop condition

- [ ] A transcript with no exam produces 'Not documented in this encounter' plus a missing flag.
- [ ] On a fixed set of test transcripts, no Examination or ROS finding appears that is not in the transcript.
- [ ] Sign is refused while any attention item is unresolved.
- [ ] A note with confidence 0.99 and no flags still requires individual review and signature.
- [ ] The review screen shows the full note and the attention items together.
- [ ] Notes without sections still display, review and sign.
- [ ] A self-contradicting note produces a contradiction item.
