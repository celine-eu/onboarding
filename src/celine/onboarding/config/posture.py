"""The settings that are safe only in development, declared in one place.

Every default in `settings.py` that makes a checkout run with no configuration —
the shared host Postgres password, the workspace's Keycloak issuer, the
workspace's Mailpit, an SMS provider that logs the code — is also a value no
deployment may run with. `celine.sdk.posture` is the shared rule for telling the
two apart: **only `CELINE_ENV=dev` relaxes**; unset, empty, `prod`, `staging`, a
typo — anything else — is hardened, and startup refuses every value registered
here, all of them in one message.

In dev the same list is logged as one warning and startup proceeds, which is what
the celine-dev workspace and `task run:api` (which exports `CELINE_ENV=dev`) rely
on.

TODO: `celine.sdk.posture` is not in a released celine-sdk yet. Raise the
`celine-sdk` floor in pyproject.toml to the first release that ships it; until
then this needs the local SDK checkout installed editable.
"""

from __future__ import annotations

from celine.sdk.posture import PostureGuard

from celine.onboarding.config.settings import DEV_SMS_PROVIDERS, Settings

SERVICE = "onboarding"


def posture_guard(settings: Settings, env: str | None = None) -> PostureGuard:
    """A guard holding every development-only value `settings` carries.

    ``env`` overrides the environment signal, for tests; ``None`` reads
    ``CELINE_ENV`` then ``ENVIRONMENT`` as the SDK does.
    """
    guard = PostureGuard(SERVICE, env=env)

    guard.forbid_dev_database_url("DATABASE_URL", settings.database_url)

    guard.forbid_true(
        "ALLOW_PERMISSIVE_POLICY",
        settings.allow_permissive_policy,
        "Unset it. With it on, every /api/admin request is allowed whenever the "
        "policy bundle fails to load.",
    )
    guard.forbid_false(
        "REQUIRE_ENCRYPTION",
        settings.require_encryption,
        "Unset it and set ENCRYPTION_KEY: participants' personal data is stored "
        "unencrypted without it.",
    )
    guard.forbid_true(
        "ALLOW_LOCAL_ADMIN",
        settings.allow_local_admin,
        "Unset it in the service's environment. `onboarding-cli --local` bypasses "
        "authorization; for a break-glass, set it on that one CLI invocation only.",
    )

    # A development provider logs the code instead of sending it, so phone
    # verification "succeeds" for whoever can read the log. Its presence is what
    # switches verification on (`phone_verification_enabled`).
    if settings.sms_provider.strip().lower() in DEV_SMS_PROVIDERS:
        guard.add(
            "SMS_PROVIDER",
            f"is {settings.sms_provider!r}, a development provider that logs OTP codes "
            "instead of sending them",
            "Set SMS_PROVIDER=brevo with DPA_SMS_SIGNED=true, or SMS_PROVIDER=none to "
            "switch phone verification off.",
        )

    # The issuer is compared against every operator token's `iss`, and its JWKS
    # is what admin requests are verified with. The default names the celine-dev
    # workspace's realm, which exists nowhere else.
    if settings.oidc_base_url == Settings.model_fields["oidc_base_url"].default:
        guard.add(
            "OIDC_BASE_URL",
            f"is the development default {settings.oidc_base_url!r}",
            "Point it at this deployment's realm.",
        )

    # Same for email: the default is the workspace's Mailpit, which offers no
    # TLS. `SMTP_HOST=` (empty) switches email off and is not a violation.
    if settings.smtp_host:
        if settings.smtp_is_dev_default():
            guard.add(
                "SMTP_HOST",
                f"is the development default {settings.smtp_host}:{settings.smtp_port} "
                "(the workspace's Mailpit)",
                "Set SMTP_HOST to this deployment's relay, or SMTP_HOST= to switch email off.",
            )
        elif not settings.smtp_tls:
            guard.add(
                "SMTP_TLS",
                "is off — submission emails would cross the network in clear",
                "Unset SMTP_TLS (it defaults to on for any relay) or set it to true.",
            )

    # Locally each client's secret equals its own id, on the realm side and in
    # the compose defaults. An empty secret is a separate question — some are
    # optional — so only the dev default is flagged here.
    for name, client_id, secret in (
        ("OIDC_CLIENT_SECRET", settings.oidc_client_id, settings.oidc_client_secret),
        (
            "DS_ONBOARDING_CLIENT_SECRET",
            settings.ds_onboarding_client_id,
            settings.ds_onboarding_client_secret,
        ),
    ):
        if secret.strip():
            guard.forbid_secret_equal_to_client_id(name, client_id, secret)

    return guard


def enforce_posture(settings: Settings, env: str | None = None) -> None:
    """Refuse to start outside dev with any development-only value; warn in dev."""
    posture_guard(settings, env=env).enforce()
