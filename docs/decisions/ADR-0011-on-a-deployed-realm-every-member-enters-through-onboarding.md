# ADR-0011 — On a deployed realm every member enters through onboarding, into a community that starts clean

**Date:** 2026-09-27
**Status:** accepted

## Context

A community could be populated two ways. One is this service: a person fills in the wizard,
an operator approves, and approval creates the login, the registry member and the dataspace
identity. The other is a static bundle: a YAML file of members imported into the registry,
with accounts written from the same file by `celine-policies keycloak sync-users`.

The bundle path is what a local stack runs on, and it had also seeded deployed communities.
A member seeded that way carries whatever the file said. Where the file had only a meter's
sensor id, the rest — name, supply point, account — was a placeholder, and nothing in the
product can tell a placeholder from a person.

Several things written here assume the bundle path is live. ADR-0005 chose member-keyed
routes partly because "most registry members were imported and have none" (no submission),
and recorded that "nothing links a registry member back to a submission". The read route
`GET /api/me/data-sharing` provisions a dataspace identity for a member "admitted offline",
because such a member has no other door.

Repairing placeholders in place, adopting them at approval, or replacing them when a meter
is attached would each put migration logic into the product. The requester ruled that out.

## Decision

**On a deployed realm, a person becomes a member only through this service.** The wizard,
the operator's recorded verification and approval, as today. A community manager then
attaches the member's meter from the community dashboard; that write goes to the registry
from the dashboard's backend and does not pass through this service.

**A community goes live clean.** A new registry community and a new Keycloak organization,
with no members. The organization, its roles and its groups are created by the
provisioning service's reconcile, which this service starts
([ADR-0014](ADR-0014-registry-sync-sets-up-the-community-through-the-provisioning-reconcile.md)).
An old community seeded from a bundle is retired, not migrated.

**Managers first.** A community's managers are placed in its organization's `managers`
group by a platform admin before members are onboarded. Nothing here grants that group.

**A manager who also participates onboards like everyone else, with the same email.** The
provisioning service finds an existing account by address and adopts it
([ADR-0004](ADR-0004-ask-the-provisioning-service-instead-of-administering-the-realm.md),
"found by address and adopted"), so the person keeps one account. The invitation answers
`has_password` and nothing is sent. With a different email they get a second account, and
the registry member points at that one.

**The YAML bundle and `sync-users` are local-development tools.** A deployed realm neither
runs nor depends on them.

## Consequences

- **Nothing in this service knows about placeholders.** No prefix, no replacement branch, no
  adopt step. The member-keyed routes, the dataspace "second door" and the export's
  handling of members this service never registered stay as they are: they are what a local
  stack seeded from a bundle exercises, and they cost nothing on a deployed realm.
- **ADR-0005's decision stands; one of its reasons narrows.** Imported members without a
  submission now exist only on local stacks. On a deployed realm every member has a
  submission, and its reference is the member's key (ADR-0004). ADR-0005's consequence that a
  dashboard send changes no step row here is unchanged: nothing reads that link today, and
  this decision does not build one.
- **The order of go-live is fixed by people, not code.** Registry community, organization
  set-up, managers, then members. A member approved before the managers exist is onboarded
  correctly and simply has nobody to attach their meter yet.
- **A manager who onboards with a second address has two accounts**, one of them the
  participant. That is visible to a platform admin and is not repaired here.
- **How a manager who is not a participant gets an account is open.** On a deployed realm only
  this service and the provisioning service create accounts, and neither has a path for a
  person who never onboards. It is a go-live operation for the clean start, and it does not
  block the manager features: attaching meters and editing role and area need a manager
  account, not a way of making one.
- **What will tempt someone to undo it:** a community that wants its existing member list
  "just loaded". A bundle import onto a deployed realm brings back exactly the placeholders
  this decision retires, and the dashboard would then offer to attach meters to them.
