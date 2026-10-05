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

``acts_as`` is who the call is made as, and ``scope`` the one scope its token asks
for: ``service`` (this service's own ``svc-ds-onboarding``), ``member`` (the
member's credential and login token), or ``collector`` — the community's own
``svc-ds-collector-<alias>``, one token per scope (ds collector contract v1).
``tests/test_collector_client.py`` holds the code to this column.
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
    acts_as: str = "service"  # "service" | "collector" | "member"
    #: For ``collector``: the single scope the token asks for.
    scope: str | None = None


CALLS: tuple[Call, ...] = (
    Call(
        "ir",
        "get",
        "/owners/resolve",
        why="Resolve the bound community's organisation at boot, by alias.",
    ),
    Call(
        "ir",
        "post",
        "/users/resolve",
        sends=frozenset({"realm", "user_id", "email"}),
        why=(
            "Reuse an existing subject DID before minting a new one. A body, so "
            "no identifier is in a URL; ds serves no GET."
        ),
    ),
    Call(
        "ir",
        "post",
        "/admin/credentials/data-subject",
        sends=frozenset({"subject_id", "role", "ttl_days"}),
        why="Issue the data-subject credential on approval.",
        acts_as="collector",
        scope="identity-registry.credentials.write",
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
        acts_as="collector",
        scope="identity-registry.memberships.write",
    ),
    Call(
        "ir",
        "delete",
        "/admin/memberships/{user_did}/{organization_alias}",
        why=(
            "Revoke membership before the credential it points at. A refusal is a "
            "failure: a row left behind still counts the person as a member."
        ),
        acts_as="collector",
        scope="identity-registry.memberships.write",
    ),
    Call(
        "ir",
        "post",
        "/admin/keycloak/sync",
        sends=frozenset({"did", "keycloak_realm", "keycloak_user_id", "email", "username"}),
        why=(
            "Put the dataspace DID on the Keycloak user, and the username beside "
            "it — the connector reads that back to name a consenting subject to "
            "the data plane, which joins it against the registry's Member.user_id. "
            "The community's act (ds ADR-0026, amended 2026-10-05): only for a DID "
            "holding a credential linked to it."
        ),
        acts_as="collector",
        scope="identity-registry.keycloak.sync",
    ),
    Call("ir", "get", "/admin/credentials/{cred_id}", why="Read a credential back when revoking."),
    Call(
        "ir",
        "delete",
        "/admin/credentials/{cred_id}",
        why="Revoke the credential on removal.",
        acts_as="collector",
        scope="identity-registry.credentials.write",
    ),
    Call(
        "connector",
        "get",
        "/ns/sharing-offers",
        why="Render the statute step's offers, and validate recorded ids.",
    ),
    # The consent calls below are made as the **community's own collector client**
    # (`svc-ds-collector-<alias>`), not as this service. ds classifies the caller
    # from its token and refuses a plain service client: one shared service
    # account is bound to no participant and could write a consent at any
    # connector for anybody's members. They are also the only calls that may go
    # to *another* participant's connector — the holder that accepted this
    # community as a consent collector. The write and the read-backs ask for
    # different scopes, so a read never carries a write-capable token.
    Call(
        "connector",
        "post",
        "/consent/admin/shares",
        sends=frozenset({"subject_id", "offer_id", "enabled", "legal_basis", "decided_by", "keys"}),
        why=(
            "Register a member's standing decision at the connector that holds "
            "the data. `decided_by` says whose decision it is — the member's, "
            "relayed, or the community's own — and `keys` carry their supply "
            "points to a holder whose data plane has no other way to find them."
        ),
        acts_as="collector",
        scope="connector.consent.provision",
    ),
    Call(
        "connector",
        "get",
        "/consent/admin/subject-shares",
        why=(
            "Read back what a holder recorded for one of our members. The member "
            "cannot: their credential has no standing at that connector and "
            "`/consent/my/*` refuses an organisation token. Per subject and "
            "never a roster. Also read before provisioning, at every connector "
            "an offer is recorded at, so a retry writes only what is missing and "
            "never lifts a withdrawal the member made there."
        ),
        acts_as="collector",
        scope="connector.consent.collector.read",
    ),
    Call(
        "connector",
        "get",
        "/consent/admin/shares",
        why=(
            "Read that decision back before exporting against it: who currently "
            "consents to this offer, at every connector holding it (ADR-0008). "
            "The community's act like the rest, with its own scope "
            "`connector.consent.audience` — and what lets the POD export stop "
            "reading the intake form."
        ),
        acts_as="collector",
        scope="connector.consent.audience",
    ),
    # The three below are made **as the member**, with the credential this
    # service resolved for them (`X-Subject-Id` + `X-User-VC`) and the member's own
    # login token (`Authorization`, ds ADR-0024), never with a token of ours. The
    # schema half does not care — it reads what ds publishes and authenticates as
    # nobody — but a reader who assumes one principal for the whole file would be
    # wrong about these.
    Call(
        "connector",
        "get",
        "/consent/my/shares",
        why=(
            "The member's own standing decisions, read as themselves. What the "
            "sharing page merges the published offers against."
        ),
        acts_as="member",
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
        acts_as="member",
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
        acts_as="member",
    ),
    Call(
        "connector",
        "get",
        "/consent/admin/decisions",
        why=(
            "Who withdrew, for the POD export's withdrawn column (ADR-0009): the "
            "community's own members' decisions on one offer, granted and withdrawn, "
            "at every connector holding it, paged to a null cursor. The "
            "community's collector client, like the per-subject read-back — the "
            "service client is refused. ds ADR-0021."
        ),
        acts_as="collector",
        scope="connector.consent.collector.read",
    ),
)
