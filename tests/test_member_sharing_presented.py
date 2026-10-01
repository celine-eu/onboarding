"""The member page gets the community's presentation of each offer, as the wizard does."""

from __future__ import annotations

import celine.onboarding.services.template_service as ts
from celine.onboarding.services import member_sharing

_OFFERS = [
    {"id": "meter-release", "recipients": {"recipient": "example-rec"}},
    {"id": "research", "recipients": {"recipient": "example-lab"}},
    {"id": "legacy", "recipients": {"controller": "example-rec"}},
]


def _manifest(monkeypatch, data_sharing):
    monkeypatch.setattr(ts, "load_manifest", lambda slug: {"consent": {"data_sharing": data_sharing}})


def test_each_offer_carries_its_recipient_name_and_the_switch(monkeypatch):
    switch = {"en": {"title": "Access", "label": "Allow it"}}
    _manifest(monkeypatch, {"recipients": {"example-rec": "Example REC"}, "summary": switch})
    out = member_sharing._presented("example", _OFFERS)
    assert [o["recipient_name"] for o in out] == ["Example REC", "example-lab", "Example REC"]
    assert all(o["switch"] == switch for o in out)


def test_no_summary_means_no_switch(monkeypatch):
    _manifest(monkeypatch, {})
    out = member_sharing._presented("example", _OFFERS)
    assert all("switch" not in o for o in out)
    assert out[0]["recipient_name"] == "example-rec"


def test_the_input_is_not_mutated(monkeypatch):
    _manifest(monkeypatch, {"summary": {"en": {"title": "t", "label": "l"}}})
    member_sharing._presented("example", _OFFERS)
    assert "recipient_name" not in _OFFERS[0] and "switch" not in _OFFERS[0]
