"""SMTP defaults: the workspace's Mailpit in dev, a deployment's relay otherwise."""

from __future__ import annotations

import pytest

from celine.onboarding.config.settings import DEV_HOST, Settings

SMTP_ENV = ("SMTP_HOST", "SMTP_PORT", "SMTP_TLS", "SMTP_FROM", "SMTP_USER", "SMTP_PASSWORD")


@pytest.fixture()
def clean_env(monkeypatch):
    for name in SMTP_ENV:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def _settings(**env) -> Settings:
    # No env files: the checkout's `.env` must not decide what a default is.
    return Settings(_env_file=None, **env)


def test_the_default_is_the_workspace_mailpit_without_tls(clean_env):
    s = _settings()
    assert (s.smtp_host, s.smtp_port, s.smtp_tls) == (DEV_HOST, 1025, False)
    assert s.smtp_is_dev_default()


def test_a_deployment_relay_gets_tls_without_asking_for_it(clean_env):
    """Pointing SMTP_HOST at a real relay must not inherit the dev host's
    plaintext: TLS follows the host unless it is set explicitly."""
    clean_env.setenv("SMTP_HOST", "smtp.example.org")
    clean_env.setenv("SMTP_PORT", "587")
    s = _settings()
    assert s.smtp_tls is True
    assert not s.smtp_is_dev_default()


@pytest.mark.parametrize("value, expected", [("false", False), ("true", True)])
def test_an_explicit_tls_setting_wins(clean_env, value, expected):
    clean_env.setenv("SMTP_HOST", "smtp.example.org")
    clean_env.setenv("SMTP_TLS", value)
    assert _settings().smtp_tls is expected


def test_an_empty_host_switches_email_off(clean_env):
    clean_env.setenv("SMTP_HOST", "")
    s = _settings()
    assert s.smtp_host == ""
    assert not s.smtp_is_dev_default()
