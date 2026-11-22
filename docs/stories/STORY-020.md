# STORY-020 — AI referral letters from signed notes

As a clinician, I want aI referral letters from signed notes, so that MeshMedic prepares documentation in the background while I stay in control of every clinical decision.

**Release:** Async copilot · Phase 7
**Owner:** MeshMedic
**Blocked by:** STORY-014, STORY-016

## The requirement this satisfies

- **REQ-030** — Referral letters are drafted by AI only from signed clinical documentation, with unsupported statements flagged and clinician approval required before sending.

## How to build it

The AI drafts from the signed note, approved codes and authorized orders only; unsupported sentences flagged; template fallback; attested approval; sending stays separate.

## Failure paths you must handle

- The note is not signed.
- The AI fails or returns malformed output.
- A sentence not supported by the note.

## Acceptance — your stop condition

- [ ] Drafting from an unsigned note is refused.
- [ ] The AI's input contains no raw transcript text.
- [ ] An unsupported letter sentence is flagged.
- [ ] An AI failure falls back to the template and the audit trail records which method drafted the letter.
- [ ] Approval for sending without an attestation is refused; generate, approve and send stay separate.
