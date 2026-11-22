# STORY-014 — Clinician identity, authorization and note sign-off

As a clinician, I want clinician identity, authorization and note sign-off, so that MeshMedic prepares documentation in the background while I stay in control of every clinical decision.

**Release:** Async copilot · Phase 1
**Owner:** MeshMedic
**Blocked by:** STORY-013

## The requirement this satisfies

- **REQ-019** — Only an authenticated clinician can sign a clinical note, authorize an order, finalize code decisions or approve a referral for sending. AI clients and background processing can never perform these actions, and the server enforces this.

## How to build it

Clinician sign-in with session and CSRF protection; short-lived signed attestations with one-time numbers; a separate sign step after review that locks the note.

## Failure paths you must handle

- An AI client calls the sign tool directly.
- An attestation is replayed, expired, or for different content.
- Repeated wrong PINs.

## Acceptance — your stop condition

- [ ] Signing without an attestation is refused, including when called directly as an AI client, and nothing is written.
- [ ] Expired, reused, wrong-action, wrong-record and wrong-content attestations are each refused.
- [ ] Signing a note that is not reviewed, or changed after review, is refused.
- [ ] A signed note records clinician ID, time and content fingerprint; later edits are refused.
- [ ] Signing twice with the same request does nothing the second time.
- [ ] There is no way to sign several notes in one action.
- [ ] Not signed in returns 401, missing CSRF returns 403, five wrong PINs locks re-authentication with an audit entry.
- [ ] Older approved notes show as approved (legacy, unsigned).
