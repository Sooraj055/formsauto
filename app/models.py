"""Pydantic schemas for FormRush profile, questions, runs, and history."""

from __future__ import annotations

from enum import Enum
from typing import Any
from pydantic import BaseModel, Field


class QuestionType(str, Enum):
    SHORT_ANSWER = "short_answer"
    PARAGRAPH = "paragraph"
    MULTIPLE_CHOICE = "multiple_choice"  # Radio buttons
    DROPDOWN = "dropdown"
    CHECKBOX = "checkbox"
    UNKNOWN = "unknown"


class MatchStatus(str, Enum):
    MATCHED = "matched"       # Green: confidently matched and ready/filled
    UNCERTAIN = "uncertain"   # Yellow: ambiguous or low confidence or optional skipped
    MISSING = "missing"       # Red: required and missing / unavailable option


class ProgressStage(str, Enum):
    LINK_RECEIVED = "link_received"
    FORM_OPENED = "form_opened"
    QUESTIONS_DETECTED = "questions_detected"
    ANSWERS_MATCHED = "answers_matched"
    FORM_FILLED = "form_filled"
    VALIDATION_COMPLETED = "validation_completed"
    SUBMISSION_INITIATED = "submission_initiated"
    SUBMISSION_CONFIRMED = "submission_confirmed"


STAGE_ORDER: list[ProgressStage] = [
    ProgressStage.LINK_RECEIVED,
    ProgressStage.FORM_OPENED,
    ProgressStage.QUESTIONS_DETECTED,
    ProgressStage.ANSWERS_MATCHED,
    ProgressStage.FORM_FILLED,
    ProgressStage.VALIDATION_COMPLETED,
    ProgressStage.SUBMISSION_INITIATED,
    ProgressStage.SUBMISSION_CONFIRMED,
]


class RunStatus(str, Enum):
    RUNNING = "running"
    PAUSED = "paused"            # Needs user input for missing/ambiguous required questions
    SUBMITTED = "submitted"      # Confirmed submitted
    BLOCKED = "blocked"          # CAPTCHA, sign-in required, closed form, etc.
    UNKNOWN = "unknown"          # Submission initiated, outcome uncertain; never blindly retry
    UNCERTAIN_TIMEOUT = "uncertain_timeout"  # Legacy alias kept for compatibility
    FAILED = "failed"
    DUPLICATE_BLOCKED = "duplicate_blocked"


class CustomProfileField(BaseModel):
    key: str = Field(..., min_length=1, max_length=64)
    label: str = Field(..., min_length=1, max_length=120)
    value: str = Field(default="", max_length=500)
    aliases: list[str] = Field(default_factory=list)


class UserProfile(BaseModel):
    full_name: str = Field(..., min_length=1, max_length=150)
    mobile_number: str = Field(..., min_length=5, max_length=30)
    email: str = Field(..., min_length=3, max_length=180)
    preferred_ticket_quantity: int = Field(..., ge=1, le=50)
    place_city: str = Field(..., min_length=1, max_length=150)
    address_locality: str = Field(..., min_length=1, max_length=300)
    pin_code: str = Field(..., min_length=3, max_length=20)
    custom_fields: list[CustomProfileField] = Field(default_factory=list)


class ProfileUpdateRequest(BaseModel):
    full_name: str = Field(..., min_length=1, max_length=150)
    mobile_number: str = Field(..., min_length=5, max_length=30)
    email: str = Field(..., min_length=3, max_length=180)
    preferred_ticket_quantity: int = Field(..., ge=1, le=50)
    place_city: str = Field(..., min_length=1, max_length=150)
    address_locality: str = Field(..., min_length=1, max_length=300)
    pin_code: str = Field(..., min_length=3, max_length=20)
    custom_fields: list[CustomProfileField] = Field(default_factory=list)


class DetectedQuestion(BaseModel):
    question_id: str
    page_index: int = 0
    title: str
    description: str = ""
    question_type: QuestionType
    required: bool = False
    options: list[str] = Field(default_factory=list)
    has_other_option: bool = False


class QuestionAnalysis(BaseModel):
    question_id: str
    page_index: int = 0
    title: str
    question_type: QuestionType
    required: bool
    options: list[str] = Field(default_factory=list)
    matched_field: str | None = None
    matched_field_label: str | None = None
    proposed_answer: str | list[str] | None = None
    confidence: float = 0.0
    status: MatchStatus = MatchStatus.MISSING
    filled: bool = False
    reason: str = ""
    candidate_fields: list[dict[str, Any]] = Field(default_factory=list)


class StartRunRequest(BaseModel):
    url: str = Field(..., min_length=5, max_length=2048)
    force_resubmit: bool = False


class ResumeAnswerItem(BaseModel):
    question_id: str
    answer: str | list[str]
    save_to_profile: bool = False
    profile_field_label: str | None = None


class ResumeRunRequest(BaseModel):
    answers: list[ResumeAnswerItem] = Field(default_factory=list)


class RunSummary(BaseModel):
    run_id: str
    form_title: str = ""
    form_url: str
    canonical_key: str
    status: RunStatus
    current_stage: ProgressStage
    completed_stages: list[ProgressStage] = Field(default_factory=list)
    total_questions: int = 0
    matched_count: int = 0
    filled_count: int = 0
    missing_required_count: int = 0
    optional_unanswered_count: int = 0
    questions: list[QuestionAnalysis] = Field(default_factory=list)
    unresolved_questions: list[QuestionAnalysis] = Field(default_factory=list)
    message: str = ""
    confirmation_text: str = ""
    error_detail: str = ""
    duplicate_warning: dict[str, Any] | None = None
    diagnostics: dict[str, Any] = Field(default_factory=dict)
    elapsed_ms: int = 0


class HistoryEntry(BaseModel):
    id: int
    run_id: str
    canonical_key: str
    form_title: str
    status: str
    total_questions: int
    matched_count: int
    filled_count: int
    message: str
    elapsed_ms: int
    created_at: str
    updated_at: str
