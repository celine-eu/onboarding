import uuid
from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from celine.onboarding.models.submission import SubmissionStatus
from celine.onboarding.models.document import DocumentType
from celine.onboarding.validators.fiscal_code import validate_fiscal_code
from celine.onboarding.validators.pod_code import validate_pod_code


class ConsentCreate(BaseModel):
    gdpr_consent: bool
    gdpr_consent_version: str = Field(max_length=20)
    policy_consent: bool
    policy_consent_version: str = Field(max_length=20)
    statute_consent: bool
    statute_consent_version: str = Field(max_length=20)


class SubmissionUpdate(BaseModel):
    first_name: str | None = Field(None, max_length=100)
    last_name: str | None = Field(None, max_length=100)
    email: str | None = Field(None, max_length=255)
    phone: str | None = Field(None, max_length=30)
    fiscal_code: str | None = Field(None, max_length=16)
    pod_code: str | None = Field(None, max_length=20)
    extracted_data: dict | None = None
    id_extracted_data: dict | None = None
    extra_data: dict | None = None
    statute_consent: bool | None = None
    keep_me_updated: bool | None = None
    status: SubmissionStatus | None = None
    notes: str | None = None

    @field_validator("fiscal_code")
    @classmethod
    def check_fiscal_code(cls, v: str | None) -> str | None:
        if v and not validate_fiscal_code(v):
            raise ValueError("Invalid Italian fiscal code")
        return v.upper().strip() if v else v

    @field_validator("pod_code")
    @classmethod
    def check_pod_code(cls, v: str | None) -> str | None:
        if v and not validate_pod_code(v):
            raise ValueError("Invalid POD code")
        return v.upper().strip() if v else v


class SubmissionRead(BaseModel):
    id: uuid.UUID
    ref: str
    status: SubmissionStatus
    first_name: str | None
    last_name: str | None
    email: str | None
    phone: str | None
    fiscal_code: str | None
    pod_code: str | None
    extracted_data: dict | None
    id_extracted_data: dict | None
    extra_data: dict | None
    consent_ip: str
    gdpr_consent: bool
    gdpr_consent_at: datetime | None
    gdpr_consent_version: str | None
    policy_consent: bool
    policy_consent_at: datetime | None
    policy_consent_version: str | None
    statute_consent: bool
    statute_consent_at: datetime | None
    statute_consent_version: str | None
    keep_me_updated: bool
    notes: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class SubmissionCreatedRead(SubmissionRead):
    session_token: str


class DocumentRead(BaseModel):
    id: uuid.UUID
    submission_id: uuid.UUID
    doc_type: DocumentType
    original_filename: str
    mime_type: str
    size_bytes: int
    created_at: datetime

    model_config = {"from_attributes": True}


class ExtractionRead(BaseModel):
    id: uuid.UUID
    document_id: uuid.UUID
    extracted_data: dict
    confirmed_by_user: bool
    confirmed_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


class ExtractionConfirm(BaseModel):
    extracted_data: dict | None = None


class AuditLogRead(BaseModel):
    id: uuid.UUID
    action: str
    entity_type: str
    entity_id: str | None
    ip_address: str | None
    detail: str | None
    created_at: datetime

    model_config = {"from_attributes": True}
