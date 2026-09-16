"""The standalone export commands see the communities the API sees.

`template_service.load_manifest` reads a cache the API fills at startup. The
commands below ran without it and reported every imported, active community as
missing — so the CLI answered a different question from the console, and ran
none of the export's checks.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from typer.testing import CliRunner

from celine.onboarding.cli.main import app

runner = CliRunner()


@pytest.fixture()
def calls(monkeypatch):
    """Records the order of the cache load and the command's own work."""
    import celine.onboarding.models.database as database
    from celine.onboarding.outputs import csv_export
    from celine.onboarding.services import submission_service, template_service

    order: list[str] = []

    async def _load():
        order.append("load_recs")

    @asynccontextmanager
    async def _session():
        class _Db:
            async def execute(self, _query):
                class _R:
                    def scalars(self):
                        return self

                    def all(self):
                        return []

                return _R()

        yield _Db()

    async def _pods(db, output, **kw):
        order.append(f"export_pod_list:{kw['recipient_ref']}")
        return 0

    async def _submissions(db, output, **kw):
        order.append("export_csv")
        return 0

    async def _validate(rec, ids):
        order.append("validate")

    monkeypatch.setattr(template_service, "load_recs_from_db", _load)
    monkeypatch.setattr(database, "async_session", _session)
    monkeypatch.setattr(csv_export, "export_pod_list", _pods)
    monkeypatch.setattr(csv_export, "export_submissions_csv", _submissions)
    monkeypatch.setattr(submission_service, "_validate_sharing_offer_ids", _validate)
    return order


def test_export_pod_list_loads_the_communities_first(calls, tmp_path):
    result = runner.invoke(
        app,
        [
            "export-pod-list",
            "--rec",
            "example",
            "--offer",
            "household-energy-flexibility",
            "--recipient",
            "grid-operator",
            "--output",
            str(tmp_path / "pods.csv"),
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == ["load_recs", "export_pod_list:grid-operator"]


def test_export_csv_loads_the_communities_first(calls, tmp_path):
    result = runner.invoke(app, ["export-csv", "--output", str(tmp_path / "subs.csv")])

    assert result.exit_code == 0, result.output
    assert calls == ["load_recs", "export_csv"]


def test_check_offers_loads_the_communities_first(calls):
    result = runner.invoke(app, ["check-offers", "--rec", "example"])

    assert result.exit_code == 0, result.output
    assert calls == ["load_recs"]


def test_a_refused_export_is_reported_not_raised(calls, monkeypatch, tmp_path):
    from celine.onboarding.outputs import csv_export

    async def _refuse(db, output, **kw):
        raise ValueError(
            "Recipient 'dso-org' is not 'grid-operator', the controller this offer names."
        )

    monkeypatch.setattr(csv_export, "export_pod_list", _refuse)
    result = runner.invoke(
        app,
        [
            "export-pod-list",
            "--rec",
            "example",
            "--offer",
            "household-energy-flexibility",
            "--recipient",
            "dso-org",
            "--output",
            str(tmp_path / "pods.csv"),
        ],
    )

    assert result.exit_code == 1
    assert "Refused: Recipient 'dso-org' is not 'grid-operator'" in result.output
    assert not (tmp_path / "pods.csv").exists()
