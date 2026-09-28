"""Which client the REC registry member client authenticates as (D60).

The end-to-end run on a local stack without the dataspace failed every approval
at the registry step: the member client authenticated as `svc-ds-onboarding`,
a client only a dataspace host declares, and the token endpoint answered 401.
`svc-onboarding` holds `rec-registry.members.write` and `rec-registry.lookup`
on every realm, so it is the identity whenever the dataspace is disabled.
"""

from __future__ import annotations

import pytest

from celine.onboarding.services import rec_registry, service_auth


@pytest.fixture()
def identities(monkeypatch):
    service_auth.reset_token_providers()
    monkeypatch.setattr(service_auth.settings, "oidc_base_url", "http://kc.test/realms/celine")
    monkeypatch.setattr(service_auth.settings, "oidc_client_id", "svc-onboarding")
    monkeypatch.setattr(service_auth.settings, "oidc_client_secret", "onboarding-secret")
    monkeypatch.setattr(service_auth.settings, "ds_onboarding_client_id", "svc-ds-onboarding")
    monkeypatch.setattr(service_auth.settings, "ds_onboarding_client_secret", "ds-secret")
    monkeypatch.setattr(rec_registry.settings, "rec_registry_url", "http://registry.test")
    monkeypatch.setattr(rec_registry, "_client", None)
    yield
    service_auth.reset_token_providers()


def _registry_client_id() -> str:
    # `_token_provider` and `_client_id` are the SDK's own attributes; which
    # client is presented is the whole point, and there is no public reader.
    return rec_registry._get_client()._token_provider._client_id


class TestTheRegistryMemberClientIdentity:
    def test_without_the_dataspace_it_is_celines_own_client(self, identities, monkeypatch):
        """
        @verifies REQ-0022
        """
        monkeypatch.setattr(service_auth.settings, "dataspace_enabled", False)

        assert service_auth.registry_token_provider()._client_id == "svc-onboarding"
        assert _registry_client_id() == "svc-onboarding"

    def test_with_the_dataspace_it_is_the_dataspace_client(self, identities, monkeypatch):
        """
        @verifies REQ-0022
        """
        monkeypatch.setattr(service_auth.settings, "dataspace_enabled", True)

        assert service_auth.registry_token_provider()._client_id == "svc-ds-onboarding"
        assert _registry_client_id() == "svc-ds-onboarding"

    def test_without_the_dataspace_no_dataspace_secret_is_needed(self, identities, monkeypatch):
        """The failure the end-to-end run hit: no usable dataspace secret.

        @verifies REQ-0022
        """
        monkeypatch.setattr(service_auth.settings, "dataspace_enabled", False)
        monkeypatch.setattr(service_auth.settings, "ds_onboarding_client_secret", "")

        provider = service_auth.registry_token_provider()

        assert provider._client_id == "svc-onboarding"
        assert provider is service_auth.celine_token_provider()

    def test_the_default_token_asks_for_no_optional_scope(self, identities, monkeypatch):
        """The member writes need only default scopes; the sync's optional
        community write is never carried by the approval's token.

        @verifies REQ-0022
        """
        monkeypatch.setattr(service_auth.settings, "dataspace_enabled", False)

        assert service_auth.registry_token_provider() is not service_auth.celine_token_provider(
            "rec-registry.community.write"
        )
