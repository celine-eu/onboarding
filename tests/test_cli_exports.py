"""The export commands are API clients, like `onboarding-cli admin`.

They used to open the database themselves, which made them a second
implementation nobody reviewed: they skipped the API's startup (every community
looked missing), carried no connector address of their own, and ran no
authorization. Now the console and the terminal call the same routes, pass the
same policy and write the same audit row. `--local` stays the break-glass.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx
from typer.testing import CliRunner

from celine.onboarding.cli.main import app

runner = CliRunner()
API = "http://api.test"

POD_LIST = (
    b"# Supply points authorised for release under offer household-energy-flexibility\n"
    b"# Controller: grid-operator\n"
    b"pod_code\n"
    b"IT001E00000001\n"
    b"IT001E00000002\n"
)
REGISTER = b"id,ref,status\n1,20260730-aaa1,approved\n"


@pytest.fixture()
def api(monkeypatch):
    from celine.onboarding.config.settings import settings

    monkeypatch.setattr(settings, "onboarding_api_url", API)
    with respx.mock(base_url=API, assert_all_called=False) as mock:
        yield mock


def pod_list(tmp_path, *extra):
    return runner.invoke(
        app,
        [
            "export-pod-list",
            "--rec", "rec-a",
            "--offer", "household-energy-flexibility",
            "--recipient", "grid-operator",
            "--output", str(tmp_path / "pods.csv"),
            "--token", "test-token",
            *extra,
        ],
    )  # fmt: skip


# ── over the API ──────────────────────────────────────────────────


def test_the_pod_list_is_the_consoles_request(api, tmp_path):
    route = api.post("/api/admin/rec-a/exports/pod-list").mock(
        return_value=httpx.Response(200, content=POD_LIST)
    )

    result = pod_list(tmp_path, "--purpose", "FlexibilityResearch", "--agreement-ref", "dsa-1")

    assert result.exit_code == 0, result.output
    request = route.calls.last.request
    assert request.headers["Authorization"] == "Bearer test-token"
    assert json.loads(request.content) == {
        "offer_id": "household-energy-flexibility",
        "recipient_ref": "grid-operator",
        "purpose": ["FlexibilityResearch"],
        "agreement_ref": "dsa-1",
    }
    assert (tmp_path / "pods.csv").read_bytes() == POD_LIST
    assert "Exported 2 supply points" in result.output


def test_a_refused_recipient_is_reported_and_writes_nothing(api, tmp_path):
    api.post("/api/admin/rec-a/exports/pod-list").mock(
        return_value=httpx.Response(
            422,
            json={"detail": "Recipient 'dso' is an alias of 'example-dso'."},
        )
    )

    result = pod_list(tmp_path)

    assert result.exit_code == 1
    assert "Refused: Recipient 'dso' is an alias" in result.output
    assert "Traceback" not in result.output
    assert not (tmp_path / "pods.csv").exists()


def test_the_register_is_the_consoles_request(api, tmp_path):
    route = api.post("/api/admin/rec-a/exports/csv").mock(
        return_value=httpx.Response(200, content=REGISTER)
    )

    result = runner.invoke(
        app,
        ["export-csv", "--rec", "rec-a", "--output", str(tmp_path / "r.csv"), "--token", "t"],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(route.calls.last.request.content) == {}
    assert (tmp_path / "r.csv").read_bytes() == REGISTER
    assert "Exported 1 submissions" in result.output


def test_a_quoted_newline_is_one_row(api, tmp_path):
    api.post("/api/admin/rec-a/exports/csv").mock(
        return_value=httpx.Response(200, content=b'id,note\n1,"two\nlines"\n2,plain\n')
    )

    result = runner.invoke(
        app,
        ["export-csv", "--rec", "rec-a", "--output", str(tmp_path / "r.csv"), "--token", "t"],
    )

    assert "Exported 2 submissions" in result.output


def test_the_register_export_names_no_recipient(api):
    """It is the community's own copy; handing data to another party is the POD list."""
    result = runner.invoke(
        app, ["export-csv", "--rec", "rec-a", "--recipient", "distributor-x", "--token", "t"]
    )

    assert result.exit_code != 0
    assert "No such option" in result.output


# ── --local, the break-glass ──────────────────────────────────────


def test_local_is_refused_unless_asked_for(monkeypatch, tmp_path):
    from celine.onboarding.config.settings import settings

    monkeypatch.setattr(settings, "allow_local_admin", False)

    result = pod_list(tmp_path, "--local")

    assert result.exit_code == 1
    assert "ALLOW_LOCAL_ADMIN" in result.output


@pytest.fixture()
def local(monkeypatch):
    """`--local` against a stand-in session, recording what it audits."""
    from celine.onboarding.cli import transport
    from celine.onboarding.config.settings import settings
    from celine.onboarding.services import audit_service

    monkeypatch.setattr(settings, "allow_local_admin", True)

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    async def _session(self):
        return _Session()

    audited: list[dict] = []

    async def _record(db, **kw):
        audited.append(kw)

    monkeypatch.setattr(transport.LocalTransport, "_session", _session)
    monkeypatch.setattr(audit_service, "record_and_commit", _record)
    return audited


def test_local_runs_the_same_export_and_audits_it(local, monkeypatch, tmp_path):
    from celine.onboarding.outputs import csv_export

    async def _export(db, path, **kw):
        assert kw["recipient_ref"] == "grid-operator"
        path.write_bytes(POD_LIST)
        return 2

    monkeypatch.setattr(csv_export, "export_pod_list", _export)

    result = pod_list(tmp_path, "--local")

    assert result.exit_code == 0, result.output
    assert (tmp_path / "pods.csv").read_bytes() == POD_LIST
    assert local[-1]["action"] == "export_pod_list"
    assert "recipient=grid-operator" in local[-1]["detail"]


def test_local_refusal_is_reported_and_not_audited(local, monkeypatch, tmp_path):
    from celine.onboarding.outputs import csv_export

    async def _refuse(db, path, **kw):
        raise ValueError("Recipient 'dso' is an alias of 'example-dso'.")

    monkeypatch.setattr(csv_export, "export_pod_list", _refuse)

    result = pod_list(tmp_path, "--local")

    assert result.exit_code == 1
    assert "Refused: Recipient 'dso' is an alias" in result.output
    assert not (tmp_path / "pods.csv").exists()
    assert local == []


# ── check-offers still reads the database, so it loads the manifests ──


def test_check_offers_loads_the_communities_first(monkeypatch):
    from contextlib import asynccontextmanager

    import celine.onboarding.models.database as database
    from celine.onboarding.services import template_service

    order: list[str] = []

    async def _load():
        order.append("load_recs")

    @asynccontextmanager
    async def _session():
        class _Db:
            async def execute(self, _query):
                order.append("query")

                class _R:
                    def scalars(self):
                        return self

                    def all(self):
                        return []

                return _R()

        yield _Db()

    monkeypatch.setattr(template_service, "load_recs_from_db", _load)
    monkeypatch.setattr(database, "async_session", _session)

    result = runner.invoke(app, ["check-offers", "--rec", "example"])

    assert result.exit_code == 0, result.output
    assert order == ["load_recs", "query"]
