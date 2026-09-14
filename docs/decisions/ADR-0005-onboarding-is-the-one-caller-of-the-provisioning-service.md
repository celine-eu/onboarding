# ADR-0005 — This service is the one caller of the provisioning service

**Date:** 2026-09-14
**Status:** accepted

Builds on [ADR-0004](ADR-0004-ask-the-provisioning-service-instead-of-administering-the-realm.md),
which made this service a client of `celine-policies`' provisioning service instead of a
Keycloak administrator.

## Context

A community manager needs two buttons on the `celine-community` dashboard: "Send
invitation" and "Reset password". Both end in the provisioning service's
`POST /participants/{community}/{key}/invitation`, which asks Keycloak to send the email.

The shorter route was for the dashboard's backend to call the provisioning service directly.
`svc-community` was briefly granted `provisioning.participants.write` for that purpose. That
route was rejected. One reason given for it was this service's admin API: every member route
there is keyed on a submission, and most registry members were imported and have none.

Against the direct route:
- **The provisioning service holds realm-wide account administration.** It is safe only
  because very few things may reach it. Every additional holder of
  `provisioning.participants.write` is another service whose compromise can create, disable
  and email accounts in every community.
- **The rules about when a member is emailed would live in two places.** This service already
  owns approval, the invitation on approval, the retry of a failed send, and revocation. A
  second caller would need its own copy of "who may cause an email, and how it is recorded".
- **The obstacle was local, not structural.** The admin API is keyed on submissions because
  nothing needed it keyed otherwise. It can gain routes keyed on the registry's own pair.

## Decision

**`provisioning.*` is granted to this service's client only, and every send reaches the
provisioning service through this service.** `celine-community` holds an onboarding scope,
`onboarding.members.invite`, instead of a provisioning scope.

**The routes are member-keyed:** `POST /api/admin/communities/{community}/members/{key}/invitation`
and `…/password-reset`.
- `{community}` is the registry community key. It resolves to exactly one REC manifest.
- No submission is read, so a member imported into the registry and a member onboarded here
  are the same request.

**The call is delegated, and the manager is proven, not asserted.**
- The dashboard's backend presents its own service token.
- It forwards the manager's own access token in `X-Acting-User-Token`.
- This service verifies both tokens. It allows the call only when the service holds the scope
  **and** the manager holds `admins` or `managers` on that REC. A header naming the manager
  was rejected: any holder of the scope could name anyone, and the audit row would record an
  unverifiable string.

## Consequences

- **One more hop.** The dashboard → this service → provisioning → Keycloak. Every answer the
  provisioning service gives passes through by its code, so the dashboard loses nothing but
  latency.
- **This service must be reachable from `celine-community` on the internal network.** It must
  not be called through the public ingress: oauth2-proxy rewrites the auth headers there, and
  the route refuses a request that carries oauth2-proxy's header.
- **Two audit rows per press**, one in each service. Each service records its own act.
- **No service can send alone**, not even with `onboarding.admin`. An unattended job that wants
  to email members would need a person's token, and that is the point.
- **A send from the dashboard changes no step row here.** Nothing links a registry member back
  to a submission, and building that link is not part of this decision.
- **What will tempt someone to undo it:** a new consumer that "only needs one call" to the
  provisioning service. The answer is a route here, not another grant there. So is the idea
  of letting the manager call these routes directly with the console token: that would drop
  the dashboard's own audit row and make the button reachable from anywhere the token is.
