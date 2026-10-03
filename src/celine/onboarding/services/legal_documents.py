"""The documents an applicant accepts, resolved per community, legal host included.

A deployment may run a legal host that serves every community's documents
(`LEGAL_BASE_URL`): `<base>/<community>/current.json` names each *slot*'s current version
and its page per language. A consent slot the manifest declares without its own `url` or
`file` resolves there, so a community's template needs no legal links at all. Per slot:

1. the manifest's own `url` or `file` wins (an operator can always point elsewhere);
2. otherwise, with `LEGAL_BASE_URL` set, the legal host: the versioned page in the
   community's language, and that version;
3. if the host has never answered, the slot's address (`<base>/<community>/<slot>/`, which
   always shows the current version) and **no version**: the acceptance's date says which
   version it was, from the host's dated history;
4. a slot whose document the community does not have (not on the host, or published
   nowhere) is not asked: nobody can be made to accept a document they cannot read;
5. without `LEGAL_BASE_URL`, today's behaviour: the declared slot as it is.

`current.json` is refreshed every few minutes, and the last good copy is kept for as long
as the host does not answer.
"""

from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import quote

import httpx

from celine.onboarding.config.settings import settings

logger = logging.getLogger(__name__)

REFRESH_SECONDS = 300.0
TIMEOUT_SECONDS = 3.0

#: The wizard's consent slot → the legal host's slot.
LEGAL_SLOT = {"gdpr": "privacy", "policy": "regulations", "statute": "statute"}
#: The notice shown above the sharing offers.
NOTICE_SLOT = "data_sharing_notice"

_current: dict[str, dict[str, Any]] = {}
_fetched_at: dict[str, float] = {}


def _base() -> str:
    return (settings.legal_base_url or "").rstrip("/")


def owner_key(manifest: dict[str, Any], rec_slug: str) -> str:
    """The community's key on the legal host: its registry community key."""
    return (manifest.get("rec_registry") or {}).get("community") or manifest.get("organization") or rec_slug


def slot_address(owner: str, slot: str) -> str:
    return f"{_base()}/{quote(owner, safe='')}/{slot}/"


async def refresh(rec_slug: str, manifest: dict[str, Any], client: httpx.AsyncClient | None = None) -> None:
    """Fetch the community's `current.json` when it is due. Never raises."""
    if not _base():
        return
    if time.monotonic() - _fetched_at.get(rec_slug, -REFRESH_SECONDS) < REFRESH_SECONDS:
        return
    _fetched_at[rec_slug] = time.monotonic()
    url = f"{_base()}/{quote(owner_key(manifest, rec_slug), safe='')}/current.json"
    try:
        if client is None:
            async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as own:
                response = await own.get(url)
        else:
            response = await client.get(url)
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict) or not isinstance(body.get("slots"), dict):
            raise ValueError("not a current.json")
        _current[rec_slug] = body
    except Exception as exc:  # the host down keeps the last good copy
        logger.warning("legal host: %s unavailable (%s); %s", url, exc,
                       "keeping the last good copy" if rec_slug in _current else "no copy yet")


def current(rec_slug: str) -> dict[str, Any] | None:
    return _current.get(rec_slug)


def _from_host(legal: dict[str, Any], locale: str) -> dict[str, Any] | None:
    """A slot of `current.json` as `{url, version, sha256}`, or None when it has no page."""
    if legal.get("external"):
        return {"url": legal["url"], "version": legal.get("version"), "sha256": legal.get("sha256")} if legal.get("url") else None
    paths = legal.get("paths") or {}
    lang = locale if locale in paths else next(iter(paths), None)
    if lang is None:
        return None
    return {"url": _base() + paths[lang], "version": legal.get("version"),
            "sha256": (legal.get("sha256") or {}).get(lang)}


def _resolve_one(rec_slug: str, manifest: dict[str, Any], legal_slot: str) -> dict[str, Any] | None:
    known = current(rec_slug)
    if known is None:
        return {"url": slot_address(owner_key(manifest, rec_slug), legal_slot), "version": None}
    legal = (known.get("slots") or {}).get(legal_slot)
    return _from_host(legal, manifest.get("locale", "it")) if legal else None


def consent_documents(rec_slug: str, manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Per asked consent slot, what it shows: the manifest's entry, completed by the legal host."""
    consent = manifest.get("consent") or {}
    resolved: dict[str, dict[str, Any]] = {}
    for slot, legal_slot in LEGAL_SLOT.items():
        declared = consent.get(slot)
        if not isinstance(declared, dict):
            continue
        if declared.get("url") or declared.get("file") or not _base():
            resolved[slot] = dict(declared)
            continue
        found = _resolve_one(rec_slug, manifest, legal_slot)
        if found is not None:
            resolved[slot] = {**declared, **found}
    return resolved


def data_sharing_notice(rec_slug: str, manifest: dict[str, Any]) -> dict[str, Any] | None:
    """The notice shown above the sharing offers: the manifest's `notice_url`, else the host's."""
    block = (manifest.get("consent") or {}).get("data_sharing")
    if not isinstance(block, dict):
        return None
    if block.get("notice_url"):
        return {"url": block["notice_url"], "version": block.get("notice_version")}
    if not _base():
        return None
    return _resolve_one(rec_slug, manifest, NOTICE_SLOT)
