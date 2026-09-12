"""Which file has the last word on a setting.

`.env` is the deployment's configuration, written once and shared. `.env.local`
is this machine's — a developer's own service URLs, and the dev secrets that
must not travel — so it is gitignored and it wins. The precedence is the whole
point of the split, and it is pydantic-settings' behaviour rather than ours,
which is exactly why it is worth pinning: a version that changed it would
otherwise turn every local override into a value that silently does nothing.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings

from celine.onboarding.config import settings as settings_module


def test_both_files_are_read_and_the_local_one_is_last():
    """Order is the mechanism. `.env.local` is read after `.env`."""
    files = settings_module.Settings.model_config["env_file"]

    assert [str(f).rsplit("/", 1)[-1] for f in files] == [".env", ".env.local"]


def test_the_second_file_overrides_the_first(tmp_path):
    """The behaviour the order relies on, asserted against the library."""
    base = tmp_path / ".env"
    base.write_text("shared=from-env\nonly_in_base=kept\n")
    local = tmp_path / ".env.local"
    local.write_text("shared=from-env-local\n")

    class _Settings(BaseSettings):
        shared: str = ""
        only_in_base: str = ""

        model_config = {"env_file": (str(base), str(local)), "env_file_encoding": "utf-8"}

    loaded = _Settings()

    assert loaded.shared == "from-env-local"
    # An override file names what it overrides and nothing else; everything the
    # deployment's own file said stays in force.
    assert loaded.only_in_base == "kept"


def test_a_missing_override_file_is_not_an_error(tmp_path):
    """Neither file has to exist. The common case is a checkout with no
    `.env.local` at all, and a deployment configured through real environment
    variables has neither."""

    class _Settings(BaseSettings):
        shared: str = "default"

        model_config = {
            "env_file": (str(tmp_path / ".env"), str(tmp_path / ".env.local")),
            "env_file_encoding": "utf-8",
        }

    assert _Settings().shared == "default"


def test_the_environment_still_wins(tmp_path, monkeypatch):
    """A container passing `-e` must not be overruled by a file baked into the
    image. pydantic-settings reads the environment ahead of both files."""
    base = tmp_path / ".env"
    base.write_text("shared=from-env\n")
    local = tmp_path / ".env.local"
    local.write_text("shared=from-env-local\n")
    monkeypatch.setenv("SHARED", "from-the-environment")

    class _Settings(BaseSettings):
        shared: str = ""

        model_config = {"env_file": (str(base), str(local)), "env_file_encoding": "utf-8"}

    assert _Settings().shared == "from-the-environment"


def _default(name: str):
    """The value declared in the class, not the one this process resolved.

    `tests/conftest.py` sets `DATABASE_URL` for every test, and a real deployment
    sets more, so reading `settings_module.settings` here would assert about the
    environment rather than about the defaults.
    """
    return settings_module.Settings.model_fields[name].default


class TestABareCheckoutHasWorkingDefaults:
    """Every address defaults to a value that resolves from both sides.

    The failure this pins is not "a setting is missing" but "the checkout holds
    two contradictory sets of addresses" — a `localhost` default is the host when
    the process is on it and the container when it is in one, so the same string
    means two different services and `task run:api` and `docker compose up` need
    different configuration for the same code.
    """

    def test_settings_construct_with_nothing_configured_at_all(self, monkeypatch):
        """`database_url` was required, so this raised `ValidationError` before
        any code ran and a fresh clone could not start.

        The environment is cleared rather than trusted: `conftest.py` supplies
        `DATABASE_URL` to every other test, which is exactly the prop this one
        has to do without.
        """
        monkeypatch.delenv("DATABASE_URL", raising=False)

        loaded = settings_module.Settings(_env_file=None)

        assert loaded.database_url == _default("database_url")

    def test_the_dialled_addresses_use_the_bridge_not_localhost(self):
        """172.17.0.1 is the host from inside a container and a local interface
        from outside it, so one string serves both."""
        for name in ("database_url", "onboarding_api_url"):
            value = _default(name)
            assert settings_module.DEV_HOST in value, name
            assert "localhost" not in value, name

    def test_the_issuer_is_a_hostname_rather_than_the_bridge_address(self):
        """`security/oidc.py` checks this against the `iss` claim, and Keycloak
        mints `iss` from its own hostname whatever the caller dialled. Reaching
        the realm on `172.17.0.1:8080` reports an issuer carrying `:8080`, which
        matches no token minted through the proxy — so this one default is a name
        that resolves on both sides, not an address that merely connects."""
        issuer = _default("oidc_base_url")

        assert settings_module.DEV_HOST not in issuer
        assert issuer.startswith("http://keycloak.")

    def test_the_addresses_whose_emptiness_disables_a_dependency_keep_no_default(self):
        """An address is the only thing that says whether a dependency is there.
        A default would switch one on that nobody deployed, and the failure would
        surface in front of an operator mid-review rather than at boot.

        `provisioning_url` could not have one in any case: the provisioning
        service is deliberately unpublished, so it has no host-side address."""
        for name in (
            "provisioning_url",
            "rec_registry_url",
            "ds_connector_url",
            "ds_ns_url",
            "ds_provenance_url",
            "identity_registry_url",
            "dataspace_keycloak_realm",
        ):
            assert _default(name) == "", name

    def test_the_pii_gate_is_not_bought_off_by_the_defaults(self):
        """Making the service startable must not make it startable *unencrypted*.
        No key is defaulted and encryption stays required: a committed Fernet key
        in an open-source repository holding applicant PII would be worse than
        the failure it removes, and a permissive default makes the plaintext
        configuration the one reached by accident."""
        assert _default("encryption_key") == ""
        assert _default("require_encryption") is True

    def test_cors_origins_stay_on_localhost(self):
        """These are browser origins, and the browser is on the host in every
        case — including when the app is in a container. The bridge address would
        be wrong here, which is why the rule is about what the string *means*
        rather than a search-and-replace for `localhost`."""
        origins = _default("cors_origins")

        assert "localhost" in origins
        assert settings_module.DEV_HOST not in origins
