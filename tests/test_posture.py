"""Development defaults are refused anywhere but `CELINE_ENV=dev`.

The service runs with no configuration on the celine-dev workspace because its
defaults name that workspace: its Postgres password, its Keycloak issuer, its
Mailpit, an SMS provider that logs the code. Each is also a value no deployment
may run with, so the environment signal decides: only `dev` relaxes, and unset is
hardened (`celine.sdk.posture`).
"""

from __future__ import annotations

import logging

import pytest
from celine.sdk.posture import InsecureConfiguration
from fastapi import FastAPI

import celine.onboarding.main as app_main
from celine.onboarding.config.posture import enforce_posture, posture_guard
from celine.onboarding.config.settings import Settings

HARDENED = ["", "staging", "prod", "develop"]

# A configuration a deployment could run with: nothing in it is a dev default.
DEPLOYED = dict(
    database_url="postgresql+asyncpg://onboarding:7d1f0c9e2b@db.rec.example.org:5432/onboarding",
    oidc_base_url="https://auth.rec.example.org/realms/example-rec",
    oidc_client_secret="a-real-secret",
    smtp_host="smtp.rec.example.org",
    sms_provider="none",
    require_encryption=True,
    allow_permissive_policy=False,
    allow_local_admin=False,
)

# Each development-only value, as the override that introduces it, and the name
# the refusal must carry.
DEV_VALUES = [
    (
        {"database_url": "postgresql+asyncpg://postgres:securepassword123@db:5432/x"},
        "DATABASE_URL",
    ),
    ({"allow_permissive_policy": True}, "ALLOW_PERMISSIVE_POLICY"),
    ({"require_encryption": False}, "REQUIRE_ENCRYPTION"),
    ({"allow_local_admin": True}, "ALLOW_LOCAL_ADMIN"),
    ({"sms_provider": "log"}, "SMS_PROVIDER"),
    ({"sms_provider": "console"}, "SMS_PROVIDER"),
    ({"sms_provider": "dev"}, "SMS_PROVIDER"),
    ({"oidc_base_url": Settings.model_fields["oidc_base_url"].default}, "OIDC_BASE_URL"),
    ({"smtp_host": Settings.model_fields["smtp_host"].default}, "SMTP_HOST"),
    ({"smtp_tls": False}, "SMTP_TLS"),
    ({"oidc_client_secret": "svc-onboarding"}, "OIDC_CLIENT_SECRET"),
    ({"ds_onboarding_client_secret": "svc-ds-onboarding"}, "DS_ONBOARDING_CLIENT_SECRET"),
]


def _settings(**overrides) -> Settings:
    # No env files: the checkout's `.env` must not decide what is being tested.
    return Settings(_env_file=None, **{**DEPLOYED, **overrides})


@pytest.mark.parametrize("env", HARDENED)
def test_a_deployed_configuration_starts_hardened(env):
    """
    @verifies REQ-0028
    """
    enforce_posture(_settings(), env=env)


@pytest.mark.parametrize("env", ["", "staging"])
@pytest.mark.parametrize(("override", "setting"), DEV_VALUES)
def test_each_development_value_is_refused_outside_dev(env, override, setting):
    """
    @verifies REQ-0028
    """
    with pytest.raises(InsecureConfiguration, match=setting):
        enforce_posture(_settings(**override), env=env)


def test_the_defaults_are_refused_together():
    """A checkout with no configuration is refused once, naming everything.

    @verifies REQ-0028
    """
    defaults = Settings(_env_file=None, database_url=Settings.model_fields["database_url"].default)
    with pytest.raises(InsecureConfiguration) as refused:
        enforce_posture(defaults, env="")
    for setting in ("DATABASE_URL", "SMS_PROVIDER", "OIDC_BASE_URL", "SMTP_HOST"):
        assert setting in str(refused.value)


def test_switching_email_off_is_not_a_violation():
    """
    @verifies REQ-0028
    """
    enforce_posture(_settings(smtp_host=""), env="staging")


def test_a_real_sms_gateway_is_not_a_violation():
    """
    @verifies REQ-0028
    """
    enforce_posture(_settings(sms_provider="brevo", dpa_sms_signed=True), env="staging")


@pytest.mark.parametrize("override", [o for o, _ in DEV_VALUES])
def test_dev_warns_and_starts(override, caplog):
    """
    @verifies REQ-0029
    """
    with caplog.at_level(logging.WARNING, logger="celine.sdk.posture"):
        enforce_posture(_settings(**override), env="dev")
    assert "development setting(s) in use" in caplog.text


def test_the_signal_is_read_from_the_environment(monkeypatch):
    """Unset is hardened; CELINE_ENV wins over ENVIRONMENT.

    @verifies REQ-0028
    @verifies REQ-0029
    """
    monkeypatch.delenv("CELINE_ENV", raising=False)
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    assert posture_guard(_settings()).hardened

    monkeypatch.setenv("ENVIRONMENT", "dev")
    assert not posture_guard(_settings()).hardened

    monkeypatch.setenv("CELINE_ENV", "staging")
    assert posture_guard(_settings()).hardened


@pytest.mark.parametrize("env", ["", "staging"])
async def test_startup_refuses_before_touching_the_database(env, monkeypatch, tmp_path):
    """
    @verifies REQ-0028
    """
    from celine.onboarding.services import template_service

    touched = []

    async def _load():
        touched.append(True)

    monkeypatch.setenv("CELINE_ENV", env)
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.setattr(template_service, "load_recs_from_db", _load)
    monkeypatch.setattr(app_main.settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(app_main.settings, "sms_provider", "log")

    with pytest.raises(InsecureConfiguration, match="SMS_PROVIDER"):
        async with app_main.lifespan(FastAPI()):
            pass
    assert not touched


async def test_dev_startup_proceeds_with_the_defaults(monkeypatch, tmp_path):
    """
    @verifies REQ-0029
    """
    from celine.onboarding.services import template_service

    async def _nothing():
        return None

    monkeypatch.setenv("CELINE_ENV", "dev")
    monkeypatch.setattr(template_service, "load_recs_from_db", _nothing)
    monkeypatch.setattr(app_main, "_validate_dataspace_config", _nothing)
    monkeypatch.setattr(app_main, "_validate_admin_config", lambda: None)
    monkeypatch.setattr(app_main, "_validate_provisioning_config", lambda: None)
    monkeypatch.setattr(app_main.settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(app_main.settings, "sms_provider", "log")
    monkeypatch.setattr(app_main.settings, "require_encryption", False)

    async with app_main.lifespan(FastAPI()):
        pass
