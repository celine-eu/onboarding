"""Every call this service makes into the dataspace, as data.

The drift that prompted this plan went unseen for three weeks because these
calls existed only as `httpx` invocations scattered across two modules: there
was nothing to compare against ds's published API, because nothing said what we
call. This list is that missing declaration.

**Adding a call to the code means adding a row here.** A row that nobody added is
a call nobody checks, which is exactly the state this file ends.

``sends`` is what the caller puts in the request body — used to prove that every
field ds marks required is one we actually send. It is deliberately not the full
payload: extra fields are the server's business, missing required ones are ours.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Call:
    service: str  # "ir" | "connector" | "provenance"
    method: str
    path: str  # the OpenAPI path template, not a formatted URL
    sends: frozenset[str] = field(default_factory=frozenset)
    #: For an endpoint whose body is a `oneOf` union, the schema this call means.
    #: `/prov/events` is the shape that broke us: its top-level schema has no
    #: `required` at all, so a checker that reads only the top level sees nothing
    #: and passes.
    variant: str | None = None
    why: str = ""


CALLS: tuple[Call, ...] = (
    Call(
        "ir",
        "get",
        "/owners/resolve",
        why="Resolve the bound community's organisation at boot, by alias.",
    ),
    Call(
        "ir", "get", "/users/resolve", why="Reuse an existing subject DID before minting a new one."
    ),
    Call(
        "ir",
        "post",
        "/admin/credentials/data-subject",
        sends=frozenset({"subject_id", "role", "ttl_days"}),
        why="Issue the data-subject credential on approval.",
    ),
    Call(
        "ir",
        "post",
        "/admin/memberships",
        sends=frozenset({"user_did", "organization_alias"}),
        why=(
            "Membership is what the consent endpoints check. No role: a "
            "membership says where somebody belongs, and what they are there is "
            "a credential claim."
        ),
    ),
    Call(
        "ir",
        "delete",
        "/admin/memberships/{user_did}/{organization_alias}",
        why="Revoke membership before the credential it points at.",
    ),
    Call(
        "ir",
        "post",
        "/admin/keycloak/sync",
        sends=frozenset({"did", "keycloak_realm", "keycloak_user_id", "email", "username"}),
        why=(
            "Put the dataspace DID on the Keycloak user, and the username beside "
            "it — the connector reads that back to name a consenting subject to "
            "the data plane, which joins it against the registry's Member.user_id."
        ),
    ),
    Call("ir", "get", "/admin/credentials/{cred_id}", why="Read a credential back when revoking."),
    Call("ir", "delete", "/admin/credentials/{cred_id}", why="Revoke the credential on removal."),
    Call(
        "connector",
        "get",
        "/ns/sharing-offers",
        why="Render the statute step's offers, and validate recorded ids.",
    ),
    Call(
        "connector",
        "post",
        "/consent/admin/shares",
        sends=frozenset({"subject_id", "offer_id", "enabled", "legal_basis"}),
        why="Provision standing sharing consent after approval.",
    ),
    Call(
        "connector",
        "get",
        "/consent/admin/shares",
        why=(
            "Read that decision back before exporting against it: who currently "
            "consents to this offer. The read counterpart to the POST above, and "
            "what lets the POD export stop reading the intake form."
        ),
    ),
    # The three below are made **as the member**, with the credential this
    # service resolved for them (`X-Subject-Id` + `X-User-VC`), not with the
    # service client every other call uses. The schema half does not care — it
    # reads what ds publishes and authenticates as nobody — but a reader who
    # assumes one principal for the whole file would be wrong about these.
    Call(
        "connector",
        "get",
        "/consent/my/shares",
        why=(
            "The member's own standing decisions, read as themselves. What the "
            "sharing page merges the published offers against."
        ),
    ),
    Call(
        "connector",
        "post",
        "/consent/my/shares",
        sends=frozenset({"offer_id", "enabled"}),
        why=(
            "The member turning one offer on or off. Names an offer and never a "
            "dataset, so the decision cannot drift from the copy they read."
        ),
    ),
    Call(
        "provenance",
        "get",
        "/prov/my/events",
        why=(
            "The member's Art. 15 read of what has happened with their data, "
            "served under their own credential. Read-only, and the only "
            "provenance call left here."
        ),
    ),
    Call(
        "connector",
        "post",
        "/admin/disclosure",
        sends=frozenset(
            {
                "offer_id",
                "recipient_ref",
                "purpose",
                "columns",
                "subject_count",
                "source_ref",
                "disclosed_by",
                "agreement_ref",
                "event_id",
            }
        ),
        why="Record a POD-list handover before it happens.",
    ),
)
