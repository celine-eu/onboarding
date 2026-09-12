from pathlib import Path

from pydantic import Field
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

    # Declared only so that startup can refuse to run with it set — nothing reads
    # it. The shared admin token was replaced by Keycloak identities and OPA
    # policies; a leftover value in a `.env` would otherwise look like protection.
    removed_admin_token: str = Field(default="", validation_alias="ADMIN_TOKEN")

    # Same, for the Keycloak administrator this service used to log in as. It
    # provisioned participant logins with `grant_type=password` against the
    # master realm, which put a human realm administrator's credential in the
    # environment of a service facing the public wizard — for a job that reaches
    # one group of one realm. It now uses its own service account
    # (`OIDC_CLIENT_ID`). These are declared so that a value left behind is
    # rejected rather than ignored: nothing reads it any more, and it is still a
    # realm administrator's password sitting in a deployment.
    removed_keycloak_admin_username: str = Field(
        default="", validation_alias="DATASPACE_KEYCLOAK_ADMIN_USERNAME"
    )
    removed_keycloak_admin_password: str = Field(
        default="", validation_alias="DATASPACE_KEYCLOAK_ADMIN_PASSWORD"
    )
    removed_keycloak_admin_client_secret: str = Field(
        default="", validation_alias="DATASPACE_KEYCLOAK_ADMIN_CLIENT_SECRET"
    )

    cors_origins: str = "http://localhost:3000,http://localhost:5173"
    security_headers: bool = True

    # --- Admin console authentication ------------------------------------
    # Inbound operator and service tokens are verified against the same issuer
    # this service already uses for its outbound M2M calls (`OIDC_BASE_URL`).
    # See security/oidc.py.
    oidc_jwks_uri: str = ""  # derived from oidc_base_url when empty
    oidc_audience: str = "svc-onboarding"
    oidc_client_id: str = "svc-onboarding"
    oidc_client_secret: str = ""
    # oauth2-proxy forwards the verified access token here; `Authorization:
    # Bearer` is the fallback, which is what the CLI and service accounts use.
    jwt_header_name: str = "x-auth-request-access-token"

    # --- onboarding-cli ---------------------------------------------------
    # The CLI drives the same HTTP endpoints the console does, with a service
    # account, so it exercises the real authorization and writes real audit rows.
    onboarding_api_url: str = "http://localhost:8040"
    onboarding_cli_client_id: str = "svc-onboarding-cli"
    onboarding_cli_client_secret: str = ""
    # `--local` talks to the database directly, bypassing HTTP and therefore
    # authorization. It is the break-glass for a deployment with no Keycloak, and
    # has to be asked for.
    allow_local_admin: bool = False

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

    # Public-endpoint rate limits, keyed per IP. Configurable because the right
    # value depends on the deployment: a community whose members share one
    # corporate or municipal NAT looks like a single very busy address.
    rate_limit_submissions: str = "20/hour"
    rate_limit_pdf: str = "5/minute"
    rate_limit_extraction: str = "10/hour"
    rate_limit_otp_send: str = "10/hour"
    rate_limit_otp_confirm: str = "20/hour"

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

    # Provenance, for `GET /api/me/data-sharing/history` and **nothing else**.
    #
    # This setting was removed when `DataDisclosed` moved to the connector's
    # `POST /admin/disclosure`, which computes the consent-snapshot hash a
    # disclosure record requires and which posting the event directly cannot.
    # None of that is reverted: this is a member's Art. 15 read of events already
    # recorded, under their own credential, and no disclosure is ever written
    # through it. Empty returns an empty history rather than failing — the
    # decisions stand without it.
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

    # Where `../celine-policies`' provisioning service is, on the internal
    # network — `http://provisioning:8010` under compose. **Empty disables the
    # login step**, which is a supported deployment: participants are onboarded
    # and given no login. Same shape as `rec_registry_url` and
    # `ds_connector_url` above, and for the same reason — an address is the only
    # thing that decides whether a dependency is there, so a separate flag could
    # only ever contradict it.
    #
    # There is no default. The service holds realm-wide Keycloak administration
    # and is safe to hold it only because nothing outside the network can reach
    # it, so this is a deployment's own internal address and a default naming
    # somebody's hostname would be wrong on every other checkout.
    provisioning_url: str = ""

    # The realm the participant's account lives in. **Not an administration
    # setting any more** — nothing here administers a realm. The dataspace step
    # tells the identity registry where to find the account, and the answer has
    # to match where the provisioning service put it: the realm `OIDC_BASE_URL`
    # issues from, which is the one realm this deployment has. Set it only where
    # that URL names no realm. A default naming a particular deployment's realm
    # would be wrong on every other checkout, so there is none.
    dataspace_keycloak_realm: str = ""

    # `DATASPACE_KEYCLOAK_ENABLED`, `_BASE_URL`, `_PARTICIPANTS_GROUP` and
    # `_UPDATE_EXISTING` were here and are gone, along with the Admin API calls
    # they configured. Declared below only so a value left behind is rejected
    # rather than ignored.
    removed_keycloak_enabled: str = Field(default="", validation_alias="DATASPACE_KEYCLOAK_ENABLED")
    removed_keycloak_base_url: str = Field(
        default="", validation_alias="DATASPACE_KEYCLOAK_BASE_URL"
    )
    removed_keycloak_participants_group: str = Field(
        default="", validation_alias="DATASPACE_KEYCLOAK_PARTICIPANTS_GROUP"
    )
    removed_keycloak_update_existing: str = Field(
        default="", validation_alias="DATASPACE_KEYCLOAK_UPDATE_EXISTING"
    )
    # `DATASPACE_KEYCLOAK_DEFAULT_PASSWORD` and `..._TEMPORARY_PASSWORD` were
    # here and went earlier. They set one shared password on every account this
    # service created: a single credential for the whole cohort, readable by
    # anyone who can read a deployment's environment. Participant credentials
    # are the provisioning service's to issue, one at a time, temporary. Do not
    # reintroduce either name.

    # `.env` then `.env.local`, and the second wins. `.env` is the deployment's
    # configuration — the file that is written once and shared; `.env.local` is
    # this machine's overrides and is gitignored, which is what makes it the
    # right home for a value that is true here and nowhere else: a developer's
    # own service URLs, and the dev secrets that must not travel. Same split as
    # `celine-policies/taskfile.local.yaml`, for the same reason.
    #
    # Neither file has to exist. A deployment that configures the process
    # through real environment variables is unaffected: those still win over
    # both, because pydantic-settings reads the environment first.
    model_config = {
        "env_file": (str(REPO_ROOT / ".env"), str(REPO_ROOT / ".env.local")),
        "env_file_encoding": "utf-8",
    }

    def resolve_path(self, value: str) -> Path:
        p = Path(value)
        return p if p.is_absolute() else REPO_ROOT / p


settings = Settings()
