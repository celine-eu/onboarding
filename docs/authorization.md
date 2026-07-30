# Authorization

Who may administer a community, and how the platform decides.

Until recently the answer was "whoever holds `ADMIN_TOKEN`". That token is gone:
startup refuses to run if it is still set, because a leftover value reads as
protection that is not there.

## The two subject types

The console authorises humans and machines on different evidence, the same split
`celine-grid` makes.

**Operators are authorised by group membership.** Keycloak has already verified
which organization somebody belongs to, so the organization is the tenancy
boundary and the group is the role. An operator carries no `onboarding.*` scope
at all.

**Service accounts are authorised by scope.** A `client_credentials` token has no
organization, so a scope is the only way for it to express intent — and, because
there is no organization to check, a scoped service can act on any community.
That is why a narrow scope matters more for a service than for a person.

Subject type is decided by **organization/group presence first**, falling back to
`is_service_account()`. The heuristic alone misfires on a user JWT that carries a
`scope` claim but no `groups`.

## Groups

`celine-policies` defines one hierarchy, created both at realm level and inside
every organization: `admins > managers > editors > viewers`.

The two levels mean different things, and the console keeps them **apart** — it
does not use `celine.sdk.auth.jwt.extract_groups`, which merges them. A merged
list would let a `managers` badge held inside community A satisfy a realm-level
check and authorise an action on community B.

| Group | May |
|---|---|
| `viewers` | read submissions (fiscal code and POD masked), read the audit trail |
| `editors` | + take in charge, edit fields and notes, unmask identifiers |
| `managers` | + approve, reject, reopen, retry a failed enablement step, export |
| `admins` | + GDPR erasure, reverse enablement |

An **organization**-level group grants those for that community's RECs. A
**realm**-level group grants them across every community — the platform operator.

`submissions.purge` and `enablement.revoke` are deliberately not reachable from
`submissions.review`: rejecting somebody is recoverable, erasing them or revoking
their credential is not, and a deployment must be able to grant one without the
other.

## Tenancy

A REC's manifest names the Keycloak organization that owns it:

```yaml
organization: my-community      # = KC org alias = identity-registry owner id
```

One identifier across the platform — no mapping table. When the manifest also has
a `dataspace:` block, `organization` is resolved from there if not stated
separately, and `onboarding-cli import-templates` **refuses** a manifest where the
two disagree.

The key is optional. A REC without one is administrable only by realm-level
platform operators, which is a coherent setup for a single-community deployment.
It fails closed — no organization means no organization-scoped grant can match —
and startup logs a warning naming every affected REC.

## Scopes

For service accounts and `onboarding-cli`. Defined in `celine-policies`'
`clients.yaml`; `onboarding.admin` satisfies all of them.

```
onboarding.recs.read            onboarding.enablement.retry
onboarding.submissions.read     onboarding.enablement.revoke
onboarding.submissions.reveal   onboarding.audit.read
onboarding.submissions.write    onboarding.export
onboarding.submissions.review   onboarding.submissions.purge
```

## How a request is decided

1. `AdminAuthMiddleware` rejects any `/api/admin/**` request with no recognisable
   token — **401, never a redirect**, because the console fetches this surface with
   XHR and a 302 to an HTML login page surfaces as a CORS failure.
2. The token is verified against the issuer's JWKS: signature, issuer, audience,
   expiry. Headers are never trusted. The anonymous wizard shares this process, so
   anything reachable at `/api/*` is reachable unauthenticated — a
   "trust the proxy header" mode would be a hole, not a shortcut.
3. The REC in the path is resolved to its organization.
4. `policies/celine/onboarding/access.rego` is evaluated in-process. An action
   name it does not know is denied, so adding an endpoint without declaring its
   capability fails closed.
5. The action is written to the audit trail against the caller.

## Configuration

| Variable | Notes |
|---|---|
| `OIDC_BASE_URL` | Realm issuer. Startup refuses without it. |
| `OIDC_JWKS_URI` | Defaults to `{OIDC_BASE_URL}/protocol/openid-connect/certs`. |
| `OIDC_AUDIENCE` | `svc-onboarding`. A token minted for another service is rejected. |
| `JWT_HEADER_NAME` | `x-auth-request-access-token`, set by oauth2-proxy. |
| `POLICIES_DIR` | Where the rego lives. Shipped in the image. |
| `ALLOW_PERMISSIVE_POLICY` | **Allows everything** when the bundle fails to load. Development only. |

Startup refuses four configurations in which the console would *appear* guarded
and not be: `ADMIN_TOKEN` still set, no OIDC issuer, unloadable policies without
the permissive flag, and a REC whose slug collides with a literal admin path
(`recs`, `me`, `ping`).

## Ingress

The wizard is anonymous and the console is not, on one host, so auth is scoped to
paths (`celine-dev/config/caddy/Caddyfile`):

- `/admin`, `/admin/*` → `(auth)`, which redirects a 401 to sign-in.
- `/api/admin*` → `(auth_api)`, which lets the 401 through for the SPA to handle.
- everything else → no auth.

Caddy's `forward_auth` buys the browser login flow, not the authorization. The
service enforces its own on every request, which is what makes the CLI, a service
account and a browser all subject to the same rules.

## Break-glass

A deployment with no Keycloak still has an operator with a shell and a
`DATABASE_URL`. That is `onboarding-cli --local`, which requires
`ALLOW_LOCAL_ADMIN=true`, goes through the same service layer as the API, and
records every action as `actor_type=cli` with the OS user and host. It is a better
trust boundary than a shared string in an env file — and unlike one, it cannot be
copied out of a chat message.
