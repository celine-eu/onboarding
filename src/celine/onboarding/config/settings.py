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
    max_upload_size_mb: int = 10

    encryption_key: str = ""
    require_encryption: bool = True
    dpa_signed: bool = False

    admin_token: str = ""
    cors_origins: str = "http://localhost:3000,http://localhost:5173"
    security_headers: bool = True

    # --- Admin console authorization -------------------------------------
    # OPA policies evaluated in-process for every /api/admin request. Default is
    # the repo's own policies/ directory rather than PoliciesSettings' relative
    # "./policies", because the API is started from ./src and a relative path
    # would resolve to src/policies.
    policies_dir: str = str(REPO_ROOT / "policies")
    # When the policy bundle cannot be loaded, deny. A permissive fallback is
    # what celine-grid does, and it is genuinely convenient in development — but
    # the failure it papers over is "no authorization at all", so it has to be
    # asked for explicitly.
    allow_permissive_policy: bool = False

    download_token_ttl: int = 86400  # 24 hours

    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_tls: bool = True
    smtp_notify: str = ""

    dataspace_enabled: bool = False
    identity_registry_url: str = ""
    oidc_base_url: str = ""
    ds_onboarding_client_id: str = "svc-ds-onboarding"
    ds_onboarding_client_secret: str = ""
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
    # Provenance base URL for recording an offline data disclosure (a CSV export)
    # as a DataDisclosed event (Block C). Empty disables the emission — the export
    # itself still runs. Uses the svc-ds-onboarding M2M token (provenance.write).
    ds_provenance_url: str = ""

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

    # REC registry — where an approved participant is registered as a community
    # member. Empty disables registration entirely, which is the configuration a
    # deployment without a registry runs.
    rec_registry_url: str = ""

    # The dataspace binding is per-REC and lives in the template manifest's
    # `dataspace:` block. There is deliberately no deployment-wide equivalent:
    # this platform is multi-tenant, and a single global alias would file every
    # community's members into one dataspace organisation.

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
