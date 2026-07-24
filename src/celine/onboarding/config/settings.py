from pathlib import Path

from pydantic_settings import BaseSettings

REPO_ROOT = Path(__file__).resolve().parents[4]


class Settings(BaseSettings):
    database_url: str
    openai_api_key: str = ""
    extraction_base_url: str = "https://api.openai.com/v1"
    extraction_model: str = "gpt-5.4"

    data_dir: str = str(REPO_ROOT / "data")
    templates_dir: str = str(REPO_ROOT / "templates")
    template_dir: str = ""
    max_upload_size_mb: int = 10

    encryption_key: str = ""
    require_encryption: bool = True
    dpa_signed: bool = False

    admin_token: str = ""
    cors_origins: str = "http://localhost:3000,http://localhost:5173"
    security_headers: bool = True

    download_token_ttl: int = 86400  # 24 hours

    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_tls: bool = True
    smtp_notify: str = ""

    dataspace_vc_enabled: bool = False
    identity_registry_url: str = ""
    oidc_base_url: str = ""
    ds_onboarding_client_id: str = "svc-ds-onboarding"
    ds_onboarding_client_secret: str = ""
    dataspace_linked_participant_did: str = ""
    dataspace_user_role: str = "DataSubject"
    dataspace_allowed_actions: str = "consent.manage,data.share"
    dataspace_vc_ttl_days: int = 365
    dataspace_subject_source: str = "email_hash"

    # Connector base URL for provisioning standing data-sharing consent after
    # approval (POST /consent/admin/shares). Empty disables share provisioning.
    # ds_ns_url is the public vocabulary base (GET /ns/sharing-offers) the wizard
    # renders offers from; empty falls back to the connector's /ns path.
    ds_connector_url: str = ""
    ds_ns_url: str = ""

    sms_provider: str = "log"
    brevo_api_key: str = ""
    brevo_sms_sender: str = ""
    sms_otp_template: str = "Il tuo codice di verifica e' {code}"
    dpa_sms_signed: bool = False

    otp_code_length: int = 6
    otp_ttl_seconds: int = 600
    otp_max_attempts: int = 3
    otp_max_sends_per_hour: int = 3
    otp_lockout_seconds: int = 3600

    dataspace_organization_alias: str = ""
    dataspace_organization_name: str = ""
    dataspace_organization_did: str = ""
    dataspace_organization_auto_create: bool = True
    dataspace_membership_role: str = "member"

    dataspace_keycloak_enabled: bool = False
    dataspace_keycloak_base_url: str = ""
    dataspace_keycloak_realm: str = "dataspaces"
    dataspace_keycloak_admin_realm: str = "master"
    dataspace_keycloak_admin_client_id: str = "admin-cli"
    dataspace_keycloak_admin_client_secret: str = ""
    dataspace_keycloak_admin_username: str = ""
    dataspace_keycloak_admin_password: str = ""
    dataspace_keycloak_default_password: str = ""
    dataspace_keycloak_temporary_password: bool = False
    dataspace_keycloak_update_existing: bool = True

    model_config = {"env_file": str(REPO_ROOT / ".env"), "env_file_encoding": "utf-8"}

    def resolve_path(self, value: str) -> Path:
        p = Path(value)
        return p if p.is_absolute() else REPO_ROOT / p


settings = Settings()
