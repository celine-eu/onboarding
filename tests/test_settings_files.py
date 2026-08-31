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
