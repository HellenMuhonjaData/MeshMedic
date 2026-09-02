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
