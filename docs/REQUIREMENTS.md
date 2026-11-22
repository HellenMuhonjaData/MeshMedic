# MeshMedic — Requirements

AI-powered clinical documentation and workflow copilot for healthcare professionals.

This is the source of truth for what you are building. Your Claude Code prompts
point here. If you sharpen a requirement, edit it — your version is the real one.

| Kind | Meaning |
|---|---|
| Functional | something the system does |
| Safety | a guardrail, with a check that enforces it |
| Reliability | how it behaves when something fails |
| Constraint | a technology or vendor you must use — context, not a task |

## Audit and Compliance

### REQ-006 — Safety · must

The system must maintain an auditable history of AI recommendations and clinician actions.

Fulfilled by: STORY-001, STORY-002

## Care Gap Identification

### REQ-003 — Functional · must

The system must identify potential gaps in care from encounter data.

Fulfilled by: STORY-005

## Clinical Documentation

### REQ-001 — Functional · must

The system must process patient encounter conversations to generate structured clinical notes.

Fulfilled by: STORY-001

## Clinician Control

### REQ-005 — Functional · must

The system must allow clinicians to review, edit, approve, or reject AI-generated notes, codes, and recommendations.

Fulfilled by: STORY-001

### REQ-014 — Safety · must

The system must not execute clinical decisions or actions without clinician approval.

Fulfilled by: STORY-001

## Coding Assistance

### REQ-002 — Functional · must

The system must suggest appropriate ICD-10 and CPT codes based on encounter data.

Fulfilled by: STORY-004

## Confidence Indication

### REQ-008 — Functional · must

The system must flag low-confidence sections with a visual warning and explanation.

Fulfilled by: STORY-003, STORY-008

## Data Input

### REQ-015 — Constraint

The system must use ambient audio recordings and text transcripts as input for note generation.

Context for the stories that use it — constraints do not get their own story.

### REQ-016 — Constraint

The system must access patient demographics and problem list/history from EHRs.

Context for the stories that use it — constraints do not get their own story.

## Efficiency

### REQ-012 — Non-functional · must

The system must reduce documentation time to under 2 minutes of review per note.

Fulfilled by: STORY-009

## EHR Integration

### REQ-009 — Functional · must

The system must retrieve and return authorized patient information through EHR systems using FHIR APIs.

Fulfilled by: STORY-006

### REQ-010 — Constraint

The system must remain EHR-agnostic while integrating with Epic and Oracle Health/Cerner.

Fulfilled by: STORY-006

## Follow-up Documentation

### REQ-004 — Functional · must

The system must prepare follow-up documentation such as referral letters for clinician review.

Fulfilled by: STORY-011

## Information Distinction

### REQ-013 — Functional · must

The system must distinguish between encounter-supported and AI-generated information.

Fulfilled by: STORY-012

## Recommendation Transparency

### REQ-007 — Functional · must

The system must provide clear explanations for its recommendations, including source citations.

Fulfilled by: STORY-007

## Security and Compliance

### REQ-011 — Safety · must

The system must protect PHI and ensure privacy and security compliance.

Fulfilled by: STORY-010

### REQ-018 — Safety · must

The system must support healthcare compliance standards relevant to clinical documentation.

Fulfilled by: STORY-010

## User Interface

### REQ-017 — Functional · should

The system must provide a visual interface for clinicians to interact with AI-generated content.

_Not yet fulfilled by any story._

## Asynchronous Clinical Workflow Copilot

Added 2026-11-22 for the approved async-copilot plan (Phases 0-8).

### REQ-019 — Safety · must

Only an authenticated clinician can sign a clinical note, authorize an order, finalize code decisions or approve a referral for sending. AI clients and background processing can never perform these actions, and the server enforces this.

Fulfilled by: STORY-013, STORY-014, STORY-018

### REQ-020 — Functional · must

After a consultation ends, the system processes the recording in the background (transcript, note, codes, care gaps, follow-up) so the clinician can move to the next patient without waiting.

Fulfilled by: STORY-015

### REQ-021 — Safety · must

Each patient encounter is an independent session; recordings, notes and recommendations are never mixed between patients.

Fulfilled by: STORY-015

### REQ-022 — Functional · must

Processed encounters enter a clinician review queue that shows attention items (missing information, contradictions, unsupported statements) first, keeps the full note visible, and never allows bulk signing.

Fulfilled by: STORY-016

### REQ-023 — Reliability · must

Drafts and review progress are saved on the server so a clinician can leave an encounter and return without losing work.

Fulfilled by: STORY-015

### REQ-024 — Functional · should

A dashboard shows encounters processed, notes awaiting review, signed notes, outstanding orders and pending referrals, plus active review time and notes unsigned at the end of the clinic day.

Fulfilled by: STORY-021

### REQ-025 — Safety · must

Real patient data is refused until the PHI readiness sign-off is complete. Raw audio is encrypted, access-restricted, deleted on a configurable retention schedule separate from clinical notes, and every deletion is logged. The audit trail is encrypted at rest while staying append-only, preserving existing records, and remaining fully verifiable.

Fulfilled by: STORY-013, STORY-015

### REQ-026 — Functional · must

AI-drafted notes are structured into HPI, ROS, Examination and Assessment/Plan, and flag missing or unclear information instead of inventing it.

Fulfilled by: STORY-016

### REQ-027 — Functional · must

Each suggested ICD-10 code shows its rationale, verified supporting evidence and the model's self-reported confidence, and can be approved, edited or rejected individually.

Fulfilled by: STORY-017

### REQ-028 — Functional · must

Care gaps are identified from chart data and the encounter, each with its source (rule and chart evidence, or transcript quote), and every accept or dismiss is audited.

Fulfilled by: STORY-019

### REQ-029 — Safety · must

AI may propose tests and follow-ups, but no order takes effect until an authenticated clinician authorizes it with an indication; identity, indication and time are recorded, and urgent orders can always be authorized directly.

Fulfilled by: STORY-018

### REQ-030 — Functional · must

Referral letters are drafted by AI only from signed clinical documentation, with unsupported statements flagged and clinician approval required before sending.

Fulfilled by: STORY-020

### REQ-031 — Safety · must

Model-generated confidence is never treated as validated clinical accuracy and never removes or shortens clinician review.

Fulfilled by: STORY-016, STORY-017
