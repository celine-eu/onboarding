import uuid
from datetime import datetime

from pydantic import BaseModel, Field, field_validator, model_validator

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
    # Data-sharing consent — collected in the statute step (after data exists, so
    # the choice is informed), optional by design. The offers, version, locale and
    # rendered-text hash record what the person saw; the connector enforces exactly
    # that.
    data_sharing_consent: bool | None = None
    data_sharing_consent_offer_ids: list[str] | None = None
    data_sharing_consent_text_version: str | None = Field(None, max_length=200)
    data_sharing_consent_locale: str | None = Field(None, max_length=20)
    data_sharing_consent_text_sha256: str | None = Field(None, max_length=64)
    keep_me_updated: bool | None = None
    status: SubmissionStatus | None = None
    notes: str | None = Field(None, max_length=2000)

    @model_validator(mode="after")
    def check_data_sharing_evidence(self) -> "SubmissionUpdate":
        """A data-sharing consent must arrive with proof of what was shown.

        The connector requires ``consent_text_version`` and
        ``rendered_text_sha256`` on every grant and refuses one without them, so
        accepting an incomplete consent here only defers the refusal to the
        approval path — where it is non-fatal by design and surfaces as a log
        line. The person would have consented, the connector would have refused
        the record, and nothing would say so.

        It cannot be repaired later either: you cannot retrospectively prove what
        somebody was shown. So the check belongs at capture.

        ``locale`` is required alongside them because the hash is over the text
        *in the language displayed* — without it the digest cannot be tied back
        to a rendering.
        """
        if self.data_sharing_consent is not True:
            return self

        missing = [
            name
            for name, value in (
                ("data_sharing_consent_text_version", self.data_sharing_consent_text_version),
                ("data_sharing_consent_text_sha256", self.data_sharing_consent_text_sha256),
                ("data_sharing_consent_locale", self.data_sharing_consent_locale),
            )
            if not (value or "").strip()
        ]
        if missing:
            raise ValueError(
                "A data-sharing consent needs evidence of what was shown; "
                f"missing: {', '.join(missing)}"
            )
        if not self.data_sharing_consent_offer_ids:
            raise ValueError(
                "A data-sharing consent must name the offers it covers "
                "(data_sharing_consent_offer_ids)"
            )
        return self

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
    rec_slug: str
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
    gdpr_consent: bool
    gdpr_consent_at: datetime | None
    gdpr_consent_version: str | None
    policy_consent: bool
    policy_consent_at: datetime | None
    policy_consent_version: str | None
    statute_consent: bool
    statute_consent_at: datetime | None
    statute_consent_version: str | None
    data_sharing_consent: bool
    data_sharing_consent_at: datetime | None
    data_sharing_consent_offer_ids: list[str] | None
    data_sharing_consent_text_version: str | None
    data_sharing_consent_locale: str | None
    data_sharing_consent_text_sha256: str | None
    share_provisioned: bool
    keep_me_updated: bool
    phone_verified: bool
    phone_verified_at: datetime | None
    notes: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class SubmissionCreatedRead(SubmissionRead):
    session_token: str


class SubmissionAdminRead(SubmissionRead):
    consent_ip: str
    dataspace_subject_id: str | None
    dataspace_did: str | None
    dataspace_vc_id: str | None
    dataspace_vc_issued_at: datetime | None
    # Why a data-sharing consent was not provisioned, if it was not. Share
    # provisioning is deliberately non-fatal, so without this the failure lives
    # only in a log and the person's decision is quietly not in force. Review is
    # the one moment a human looks at this submission.
    data_sharing_issues: list[str] = []

    @model_validator(mode="after")
    def explain_unprovisioned_share(self) -> "SubmissionAdminRead":
        if not (self.data_sharing_consent and not self.share_provisioned):
            return self

        # Name the specific evidence gap where there is one, because that class
        # of failure is permanent: the connector will refuse every retry, and no
        # amount of retrying can reconstruct what the person was shown.
        problems: list[str] = []
        for label, value in (
            ("consent text version", self.data_sharing_consent_text_version),
            ("rendered text hash", self.data_sharing_consent_text_sha256),
        ):
            if not (value or "").strip():
                problems.append(
                    f"No {label} was recorded, so the dataspace will refuse this "
                    "consent. It cannot be repaired — ask again."
                )
        if not self.data_sharing_consent_offer_ids:
            problems.append("Consent recorded but no offers were named.")

        self.data_sharing_issues = problems or [
            "Consent recorded but not yet provisioned to the dataspace. "
            "Retry from the admin action once the cause is resolved."
        ]
        return self


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


class PhoneVerifyRequest(BaseModel):
    """Request an OTP. `phone` defaults to the number already on the submission."""

    phone: str | None = Field(None, max_length=30)


class PhoneConfirmRequest(BaseModel):
    phone: str | None = Field(None, max_length=30)
    code: str = Field(..., min_length=4, max_length=10)


class PhoneVerifyStatus(BaseModel):
    phone_verified: bool
    sent: bool = False
    phone_verified_at: datetime | None = None
