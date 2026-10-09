# Authorization

Who may administer a community, and how the platform decides.

Until recently the answer was "whoever holds `ADMIN_TOKEN`". That token is gone:
startup refuses to run if it is still set, because a leftover value reads as
protection that is not there.

## The two subject types

The console authorises humans and machines on different evidence, the same split
`celine-grid` makes.

**Operators are authorised by group membership, or by the platform role.**
Keycloak has already verified which organization somebody belongs to, so the
organization is the tenancy boundary and the group is the role. The one exception
is the platform administrator, who holds the realm role `platform-admin`. An
operator carries no `onboarding.*` scope at all.

**Service accounts are authorised by scope.** A `client_credentials` token has no
organization, so a scope is the only way for it to express intent — and, because
there is no organization to check, a scoped service can act on any community.
That is why a narrow scope matters more for a service than for a person.

**A delegated action needs both.** See [Delegated actions](#delegated-actions).

Subject type is decided by the SDK's `is_service_account()`, from the token's own
markers (a `service-account-` username, a client-credentials grant recorded in
`jti`, no person's claims). A realm group is never what decides it.

## Two levels of grant

There are exactly two, and nothing in between (REQ-0030):

- **The realm role `platform-admin`**, read from the access token's
  `realm_access.roles` (`JwtUser.realm_roles`). It is the only platform-wide grant:
  every capability on every community, `recs.write` and `recs.drift` included, and
  the only one for a person.
- **An organization's own groups**, `admins > managers > editors > viewers`, read from
  the `Organization` that `JwtUser.get_organization` returns for the organization the
  request concerns. They are valid inside that organization only.

**A realm group grants nothing.** The top-level `groups` claim is not read for
authorization at all, whatever it holds — `/admins` included, which was the
platform administrator before the role. No other realm role grants anything either,
`admin` and `manager` included, and no client role does. The reason is the names:
an organization's groups are `/admins`, `/managers`, … too, so a reader that took
both levels together would let a community's own `admins` act as the platform's.
The role has a name no organization group carries.

The policy input keeps the levels apart: the realm roles in `input.subject.roles`,
`input.subject.groups` always empty, and only the matched organization's groups in
`input.subject.claims.org_groups`. `security/policy.py` builds that input through
the SDK's `PolicyInput`, passing the realm roles as `Subject.roles`, and serialises
it with `PolicyEngine.build_input_dict`, which emits `subject.roles` beside
`subject.groups` (celine-sdk 2.0.0).

A service account is never granted by the role: it is authorised by its scopes, which
are not organization-scoped. A service holding `onboarding.recs.write` (or
`onboarding.admin`) therefore reaches `recs.write` on every community: see below.

| Group | May |
|---|---|
| `viewers` | read submissions (fiscal code and POD masked), read the audit trail |
| `editors` | + take in charge, edit fields and notes, unmask identifiers |
| `managers` | + approve, reject, reopen, correct a POD, name or email by revision, retry a failed enablement step, export, see whether the registry's areas match the template (organization level only; see below) |
| `admins` | + GDPR erasure, reverse enablement |

An organization group grants those for that community's RECs only. The
`platform-admin` role grants all of them, and `recs.write`, across every community.

`submissions.purge` and `enablement.revoke` are deliberately not reachable from
`submissions.review`: rejecting somebody is recoverable, erasing them or revoking
their credential is not, and a deployment must be able to grant one without the
other.

### `recs.write` is the platform operator's only

The registry sync (`POST /api/admin/recs/{rec}/registry-sync`) writes a whole community's
areas and topology in the REC registry and sets its Keycloak organization up. **A sync is a
platform operator's decision, made as a `platform-admin` person or through the operator's
client `celine-cli`.** So its capability, `recs.write`, is granted by the realm role
`platform-admin` and by the scope `onboarding.recs.write`, which `onboarding.admin` covers:
never by an organization's own `admins` (`platform_only_actions` in the rego), and never by
a realm group. It is not delegated: a forwarded operator token lends a service nothing here.

`celine-cli` holds `onboarding.admin`, and so does `svc-onboarding-cli`, the CLI's default
client: either can start a sync, and the audit names which. `onboarding-cli registry-sync`
uses its client-credentials identity unless given a person's `--token`, or runs `--local`.
See [ADR-0012](decisions/ADR-0012-areas-are-primary-substation-boundaries-owned-by-the-template.md)
and [ADR-0017](decisions/ADR-0017-the-platform-operators-client-may-start-a-registry-sync.md).

### `recs.drift` is the platform admin's and the REC's own managers and admins

The console's drift check (`GET /api/admin/recs/{rec}/registry-drift`, the *Areas* page)
shows whether the registry's areas match the template. Its capability, `recs.drift`, is
granted to the REC's own `managers` and `admins` (organization level) and to the
`platform-admin` role. The REC's `editors` and `viewers` do not reach it, and no scope grants it (`people_only_actions`), so
`onboarding.admin` does not satisfy it (D55).

### An organization grant requires a REC

`granted_by_org_group` also checks the **type** of the matched organization: it
must be `rec`. An organization typed `dso`, typed anything else, or carrying no
type at all grants nothing here, whatever its members are called.

The attribute is written by `celine-policies keycloak sync-orgs` from the owner's
`organization.role`, or, for an organization the provisioning service's reconcile
creates, from the registry community's type (default `rec`). Reading it is the SDK's job: `Organization.type` takes the
flattened `type` key a real token carries
(`"organization": {"my-rec": {"type": ["rec"], "groups": [...]}}`) and falls back
to the nested `attributes.type`, so a policy written against `attributes.type`
alone would match nothing. **A realm that has never been synced
has no typed organization and its operators are refused** — visibly, with a reason
naming the type, rather than silently. That is deliberate: tolerating a missing
attribute would make the check bypassable by leaving it off.

## Delegated actions

`members.invite` and `members.release` are the capabilities that neither subject type
reaches alone. `members.release` follows the same rules with its own scope,
`onboarding.members.release`. Its operator must hold `admins` (not `managers`) or the
`platform-admin` role: releasing a member is `enablement.revoke`'s grant. The rest of this
section describes `members.invite`, which covers
the two member-keyed routes that email a registry member
([api-reference.md](api-reference.md)). It is allowed only when **both** of these hold:

1. **The caller is a service holding `onboarding.members.invite`**, presented in
   `Authorization`.
2. **It forwards a verified operator token** in `X-Acting-User-Token`, and that operator
   holds `admins` or `managers` on the REC's organization, or holds the `platform-admin`
   role. The rules are exactly the ones above, applied to the operator instead of the
   caller.

What follows from that:
- **A manager's own token is refused**, even a platform admin's. The community dashboard is the
  one path, so its own audit row always exists.
- **No service can send alone.** That includes `onboarding.admin`, which otherwise satisfies
  every scope. An email to a member only ever follows a person's decision.
- **`/api/admin/me` never lists `members.invite`.** Nobody holds it alone, so the console has
  no button for it.

**The operator's identity is a token, not a header naming them.** Onboarding verifies it
with the same JWKS, issuer and `svc-onboarding` audience check as a console request. A
manager's oauth2-proxy token already carries that audience. So the policy judges real
claims, and the audit row records a signed `sub`, not a string some caller asserted.

**The delegated dependency never reads `x-auth-request-access-token`, and it refuses a
request that carries it.** Everywhere else that header is read first. Here it would
authenticate the request as the manager and skip the scope check.

In the rego the operator is `input.environment.actor`. The SDK's engine serialises a fixed
input shape, and `environment` is its free-form slot. `security/policy.py` builds it with
the same code that builds `input.subject`. So the platform role and the organization groups
stay apart, and only the organization matching this REC is passed.

Two policies check the same manager: `celine-community`'s on its community, and this one on
the REC's organization. Both read the same Keycloak organization membership. That is defence
in depth, not a second source of truth.

## Tenancy

A REC's manifest names the Keycloak organization that owns it:

```yaml
organization: my-community      # = KC org alias = identity-registry owner id
```

One identifier across the platform — no mapping table. When the manifest also has
a `dataspace:` block, `organization` is resolved from there if not stated
separately, and `onboarding-cli import-templates` **refuses** a manifest where the
two disagree.

The key is optional. A REC without one is administrable only by a platform admin
(the realm role `platform-admin`), which is a coherent setup for a
single-community deployment. It fails closed — no organization means no organization-scoped grant can match —
and startup logs a warning naming every affected REC.

## Scopes

For service accounts and `onboarding-cli`. Defined in `celine-policies`'
`clients.yaml`; `onboarding.admin` satisfies all of them. `onboarding.recs.write` grants the
registry sync (`recs.write`) and is meant for the platform operator's client; `celine-cli`
holds it through `onboarding.admin`. None grants `recs.drift` (the drift check), which is for
people only.

```
onboarding.recs.read            onboarding.enablement.retry
onboarding.submissions.read     onboarding.enablement.revoke
onboarding.submissions.reveal   onboarding.audit.read
onboarding.submissions.write    onboarding.export
onboarding.submissions.review   onboarding.submissions.purge
onboarding.submissions.revise   onboarding.members.invite
onboarding.recs.write           onboarding.members.release
```

`onboarding.submissions.revise` (correcting a POD, name or email by revision,
[ADR-0015](decisions/ADR-0015-a-correction-is-a-revision.md)) is declared and granted to no
client: no service holds `onboarding.submissions.review` either.

`onboarding.members.invite` is for `celine-community` alone, and it is useless without a
manager's token. `onboarding.members.release` is the same, with a REC admin's token. See
[Delegated actions](#delegated-actions).

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
| `ALLOW_PERMISSIVE_POLICY` | **Allows everything** when the bundle fails to load. Development only: startup refuses it unless `CELINE_ENV=dev`. |

Startup refuses four configurations in which the console would *appear* guarded
and not be: `ADMIN_TOKEN` still set, no OIDC issuer, unloadable policies without
the permissive flag, and a REC whose slug collides with a literal admin path
(`recs`, `me`, `ping`, `communities`).

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

Set `ALLOW_LOCAL_ADMIN=true` on that one CLI invocation, not in the service's
environment: outside `CELINE_ENV=dev` the API refuses to start with it set.
