"""The contract check accepts a call's fallback method only under CELINE_ENV=dev.

`tests/contract` needs a live ds and is not part of the unit suite, so the rule
that decides which method a call is checked under is held here. A fallback that
counted outside dev would hide exactly the drift the contract suite exists to
show: the code refuses the fallback there, so the check must too.
"""

from __future__ import annotations

import pytest
from contract.inventory import CALLS, Call
from contract.test_ds_openapi_contract import accepted_method

RESOLVE = Call("ir", "post", "/users/resolve", fallback_method="get")
OLD_IR = {"paths": {"/users/resolve": {"get": {}}}}
NEW_IR = {"paths": {"/users/resolve": {"get": {}, "post": {}}}}
NEITHER = {"paths": {"/users/resolve": {"put": {}}}}


def test_dev_accepts_the_fallback_when_the_primary_is_missing():
    assert accepted_method(OLD_IR, RESOLVE, dev=True) == "get"


def test_outside_dev_the_fallback_is_refused():
    assert accepted_method(OLD_IR, RESOLVE, dev=False) is None


@pytest.mark.parametrize("dev", [True, False])
def test_the_primary_wins_whenever_it_is_published(dev):
    assert accepted_method(NEW_IR, RESOLVE, dev=dev) == "post"


@pytest.mark.parametrize("dev", [True, False])
def test_neither_method_published_is_refused(dev):
    assert accepted_method(NEITHER, RESOLVE, dev=dev) is None


def test_a_call_without_a_fallback_never_gets_one():
    call = Call("ir", "post", "/users/resolve")
    assert accepted_method(OLD_IR, call, dev=True) is None


def test_the_resolve_row_declares_get_as_its_dev_fallback():
    (row,) = [c for c in CALLS if c.path == "/users/resolve"]
    assert (row.method, row.fallback_method) == ("post", "get")
