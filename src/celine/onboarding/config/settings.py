from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings

REPO_ROOT = Path(__file__).resolve().parents[4]

# The docker bridge gateway, and the only address that means the same thing from
# both sides of the container boundary: inside a container it is the host, and on
# the host it is a local interface, so anything bound to `0.0.0.0` answers on it
# either way. That is what lets one default serve `task run:api` and
# `docker compose up` without the checkout holding two contradictory sets of
# addresses — which is what it held until 2026-09-12, when `backend` pointed at a
# `postgres` service this compose file does not define.
#
# It only works for a service published on a host port. The provisioning service
# is deliberately not published — its whole safety argument is that a holder of
# realm-wide Keycloak administration is unreachable from outside the internal
# network — so it has no both-sides address at all. That is why `provisioning_url`
# below has no default rather than a worse one.
#
# Same convention as `celine-forecasting` and `celine-ai-assistant`.
DEV_HOST = "172.17.0.1"

# SMS providers by whether they send anything to a third party. `services/sms.py`
# builds one for each name.
DEV_SMS_PROVIDERS = frozenset({"log", "console", "dev"})
REAL_SMS_PROVIDERS = frozenset({"brevo"})


class Settings(BaseSettings):
    # Dev default: the host Postgres this workspace's stacks share, reachable at
    # the same address from a container and from the host.
    database_url: str = (
        f"postgresql+asyncpg://postgres:securepassword123@{DEV_HOST}:15432/rec_onboarding"
    )
    extraction_api_key: str = ""
    extraction_base_url: str = "https://api.openai.com/v1"
    extraction_model: str = "gpt-5.4"

    data_dir: str = str(REPO_ROOT / "data")
    templates_dir: str = str(REPO_ROOT / "templates")
    max_upload_size_mb: int = 10

    encryption_key: str = ""
    require_encryption: bool = True
    # Set only once the endpoint at `extraction_base_url` is operated by this
    # deployment's own operator, or covered by a processing agreement (GDPR Art. 28)
    # that keeps processing in the EU. See `document_processing_enabled`.
    extraction_enabled: bool = False

    # The two names the switch had until 2026-09-14. Declared only so a leftover is
    # reported at boot instead of silently leaving the feature off: `DPA_SIGNED`
    # asserted a contract an in-house endpoint does not have, and `OPENAI_API_KEY`
    # named a vendor the endpoint need not be. Nothing reads them.
    removed_dpa_signed: str = Field(default="", validation_alias="DPA_SIGNED")
    removed_openai_api_key: str = Field(default="", validation_alias="OPENAI_API_KEY")

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
    onboarding_api_url: str = f"http://{DEV_HOST}:8040"
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

    # Dev default: the Mailpit `../celine-policies`' compose publishes on the host's
    # 1025 — the same catch-all Keycloak's invitation emails land in, so a
    # developer sees every message this workspace sends in one inbox and none of
    # them reaches a real person. A deployment sets SMTP_HOST (and the rest) to
    # its relay; `SMTP_HOST=` switches email off, which is what leaving it unset
    # used to mean.
    smtp_host: str = DEV_HOST
    smtp_port: int = 1025
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = "onboarding@celine.localhost"
    # Unset means "on, unless the dev Mailpit is the host": Mailpit offers no
    # STARTTLS, and a deployment that points SMTP_HOST at its relay must not
    # silently lose TLS because it never thought to set this. Resolved below, so
    # every reader sees a plain bool.
    smtp_tls: bool | None = None
    smtp_notify: str = ""

    dataspace_enabled: bool = False
    identity_registry_url: str = ""
    # **A hostname, not `DEV_HOST`, and the difference is not cosmetic.** This
    # value is compared, not just dialled: `security/oidc.py` checks it against
    # the `iss` claim, and Keycloak mints `iss` from its own `KC_HOSTNAME`
    # regardless of the address the caller used. Dialling `172.17.0.1:8080`
    # reaches the same realm and reports `iss` as
    # `http://keycloak.celine.localhost:8080/realms/celine` — a different string
    # — so every operator token minted through the proxy would fail
    # verification. The name below is the both-sides form in its own right:
    # `/etc/hosts` maps it to the bridge on the host, `extra_hosts:
    # host-gateway` maps it to the same gateway in a container.
    #
    # This is a **development** default. `main.py` warns when it is the value in
    # force, because it is the one setting here whose default is silently wrong
    # in production rather than merely absent.
    oidc_base_url: str = "http://keycloak.celine.localhost/realms/celine"
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

    # --- the community's own organisation client -------------------------
    # Registering a consent is an act of an *organisation*, not of a service. A
    # connector classifies its caller from the token, and a plain service client
    # is bound to no participant — so it could write at any connector for
    # anybody's members. ds retired that path: `svc-ds-onboarding` is now refused
    # on `POST /consent/admin/shares` with a 403 naming the client to use.
    #
    # That client is `svc-ds-connector-<alias>`, the community's own, and the
    # alias is the REC's `dataspace.organization` — so the id is derived per REC
    # and this setting only overrides it. The secret has no default and no
    # derivation, because it is a credential.
    #
    # One secret per process. A deployment serving two dataspace communities from
    # a single instance would need two, and there is nowhere to put the second: a
    # manifest is the per-REC home for configuration and is not a secret store.
    ds_org_client_id: str = ""
    ds_org_client_secret: str = ""

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

    @model_validator(mode="after")
    def _resolve_smtp_tls(self) -> "Settings":
        if self.smtp_tls is None:
            self.smtp_tls = self.smtp_host != DEV_HOST
        return self

    @property
    def document_processing_enabled(self) -> bool:
        """Whether participants may upload a bill or ID card and have it read.

        Scanning sends identity documents to the extraction endpoint, so it needs
        the operator to have switched it on (`EXTRACTION_ENABLED`, which asserts the
        endpoint is in-house or under an EU processing agreement) and a key to call
        it with (`EXTRACTION_API_KEY`). Either missing means the feature is off — not a refusal to
        start: the wizard still onboards a participant from the fields they type.
        Upload follows the same switch, because a stored document exists only to
        be scanned or reviewed alongside a scan.
        """
        return self.extraction_enabled and bool(self.extraction_api_key)

    @property
    def phone_verification_enabled(self) -> bool:
        """Whether participants can verify their phone number by SMS.

        A development provider sends nothing — it logs the code — so it needs no
        agreement. A real gateway receives the participant's phone number and is a
        processor under GDPR Art. 28, so it is used only with `DPA_SMS_SIGNED`. An
        unknown provider name cannot send anything either way. Off is a degraded
        feature, not a refusal to start: the wizard drops the `phone_verify` step.
        """
        name = self.sms_provider.strip().lower()
        if name in DEV_SMS_PROVIDERS:
            return True
        return name in REAL_SMS_PROVIDERS and self.dpa_sms_signed

    def smtp_is_dev_default(self) -> bool:
        """Whether email is going to the workspace's Mailpit because nothing said otherwise."""
        default = type(self).model_fields["smtp_host"].default
        return bool(self.smtp_host) and self.smtp_host == default

    def resolve_path(self, value: str) -> Path:
        p = Path(value)
        return p if p.is_absolute() else REPO_ROOT / p


settings = Settings()
