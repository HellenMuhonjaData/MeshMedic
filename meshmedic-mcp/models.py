"""Pydantic input/output models for MeshMedic's MCP tools (the tools' public contract).

Split out of server.py (Phase 0, no behaviour change).
"""

from typing import Annotated, Literal

from pydantic import BaseModel, Field

from review_tracking import ReviewRecord


class PatientMatch(BaseModel):
    mrn: str
    first_name: str
    last_name: str
    date_of_birth: str
    ehr_system: str
    patient_id: str


class SearchResult(BaseModel):
    matches: list[PatientMatch]
    count: int
    message: str


class GeneratedNote(BaseModel):
    note_id: str
    patient_id: str
    ehr_system: str
    status: Literal["draft"]
    note_text: str
    generated_at: str
    confidence: float
    flagged: bool
    warning: str | None


class ReviewedNote(BaseModel):
    note_id: str
    patient_id: str
    ehr_system: str
    status: Literal["approved", "rejected"]
    note_text: str
    feedback: str | None
    reviewed_at: str
    documentation_time_seconds: float
    exceeded_target: bool
    suggestions: list[str] | None
    review: ReviewRecord | None = None


class EditedNote(BaseModel):
    note_id: str
    patient_id: str
    ehr_system: str
    status: Literal["draft"]
    note_text: str
    edited_at: str


class CodeSuggestion(BaseModel):
    code: Annotated[str, Field(min_length=1, max_length=20)]
    code_system: Literal["icd-10", "cpt"]
    description: Annotated[str, Field(min_length=1, max_length=300)]


class SuggestedCodes(BaseModel):
    suggestion_id: str
    note_id: str
    patient_id: str
    ehr_system: str
    status: Literal["draft"]
    codes: list[CodeSuggestion]
    confidence: float
    flagged: bool
    warning: str | None
    suggested_at: str


class ReviewedCodes(BaseModel):
    suggestion_id: str
    note_id: str
    patient_id: str
    ehr_system: str
    status: Literal["approved", "rejected"]
    codes: list[CodeSuggestion]
    feedback: str | None
    reviewed_at: str
    review: ReviewRecord | None = None


class CareGapInput(BaseModel):
    description: Annotated[str, Field(min_length=1, max_length=300)]
    confidence: Annotated[float, Field(ge=0.0, le=1.0)]
    confidence_reason: Annotated[str | None, Field(max_length=1000)] = None
    # item_id from record_ai_suggestions when the clinician is accepting an
    # AI-suggested gap (possibly edited); omit for a gap the clinician entered.
    suggestion_item_id: str | None = None


class CareGap(BaseModel):
    gap_id: str
    note_id: str
    patient_id: str
    ehr_system: str
    status: Literal["flagged"]
    description: str
    confidence: float
    flagged: bool
    warning: str | None
    identified_at: str
    review: ReviewRecord | None = None


class AddressedCareGap(BaseModel):
    gap_id: str
    note_id: str
    patient_id: str
    ehr_system: str
    status: Literal["addressed"]
    description: str
    resolution: str
    addressed_at: str


class OpenCareGapSummary(BaseModel):
    gap_id: str
    description: str


class CareGapPriorityResult(BaseModel):
    patient_id: str
    gap_count: int
    open_gaps: list[OpenCareGapSummary]
    degraded: bool
    degraded_reason: str | None
    ranking_text: str | None
    prioritized_at: str


class TestOrder(BaseModel):
    order_id: str
    note_id: str
    patient_id: str
    ehr_system: str
    status: Literal["pending"]
    test_name: str
    reason: str
    ordered_at: str
    review: ReviewRecord | None = None


class ResultedTestOrder(BaseModel):
    order_id: str
    note_id: str
    patient_id: str
    ehr_system: str
    status: Literal["resulted"]
    test_name: str
    reason: str
    result_text: str
    ordered_at: str
    resulted_at: str


class PendingItemsSummary(BaseModel):
    patient_id: str
    open_care_gaps: list[OpenCareGapSummary]
    pending_orders: list[TestOrder]
    resulted_orders: list[ResultedTestOrder]
    checked_at: str


class GitHubIssueStatus(BaseModel):
    issue_number: int
    ok: bool
    found: bool | None
    state: str | None
    title: str | None
    is_pull_request: bool | None
    error: str | None
    checked_at: str


class TranscriptFileRead(BaseModel):
    file_path: str
    denied: bool
    transcript: str | None
    error: str | None
    read_at: str


class CitationResult(BaseModel):
    note_id: str
    patient_id: str
    ehr_system: str
    claimed_excerpt: str
    found: bool
    matched_text: str | None
    start_offset: int | None
    end_offset: int | None
    explanation: str | None
    requested_at: str


class ComplianceControlResult(BaseModel):
    control_id: str
    description: str
    passed: bool
    detail: str


class ComplianceCheckResult(BaseModel):
    checked_at: str
    controls: list[ComplianceControlResult]
    all_passed: bool
