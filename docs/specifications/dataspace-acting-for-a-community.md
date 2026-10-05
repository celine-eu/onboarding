# Acting for a community in the dataspace

Who this service is when it calls the dataspace (ds), and with what. The narrative,
the sequence and the configuration are in [dataspace-integration](../dataspace-integration.md);
this page states the behaviour a test can name.

Three principals reach ds: **this service** (`svc-ds-onboarding`) for what it does as
itself, **the community** (`svc-ds-collector-<alias>`) for everything done for it, and
**the member** for their own decisions and history. The collector client is ds's (collector
contract v1): `sub` is the organisation's DID, it has no default ds scopes, and each scope
it may ask for adds exactly one audience.

### REQ-0035 — every act for a community is made as that community's collector client, with its own secret

Issuing and revoking a member's data-subject credential, registering and removing their
membership, writing the DID <-> Keycloak mapping that binds their login to their DID,
registering a consent decision, reading decisions back (per subject and per offer), and
reading an offer's audience are made as `svc-ds-collector-<alias>`, where
`<alias>` is the REC manifest's `dataspace.organization`. Its secret is read from
`SVC_DS_COLLECTOR_<ALIAS>_SECRET` — the alias upper-cased, `-` replaced by `_` — in the
process environment first, then in `.env` / `.env.local`, where such a name does not refuse
boot as an unknown setting. A deployment serving several communities holds one secret per
community, and no community's act is made with another's.

Resolving an organisation or a subject stays this service's own (`svc-ds-onboarding`).
The mapping moved to the collector client with ds ADR-0026's amendment (2026-10-05): ds
accepts it from a community only for a DID holding a credential linked to that community,
and only rebinding the same login. Approval asks for its token before issuing anything,
so a realm that does not grant the scope yet stops the approval before a credential exists.

### REQ-0036 — each collector token asks for exactly one scope, the one its route requires, and is cached per community and scope

| Call | Scope |
|---|---|
| `POST /admin/credentials/data-subject`, `DELETE /admin/credentials/{id}` | `identity-registry.credentials.write` |
| `POST /admin/memberships`, `DELETE /admin/memberships/{did}/{alias}` | `identity-registry.memberships.write` |
| `POST /admin/keycloak/sync` (approval, and an email correction's re-sync) | `identity-registry.keycloak.sync` |
| `POST /consent/admin/shares` | `connector.consent.provision` |
| `GET /consent/admin/subject-shares`, `GET /consent/admin/decisions` | `connector.consent.collector.read` |
| `GET /consent/admin/shares` | `connector.consent.audience` |

A token request names exactly one of `identity-registry.memberships.write`,
`identity-registry.credentials.write`, `identity-registry.keycloak.sync`, `connector.consent.provision`,
`connector.consent.collector.read`, `connector.consent.audience`,
`connector.disclosure.record`, `provenance.write`; anything else — a space- or
comma-joined list included — is refused before a token is requested. The contract
inventory (`tests/contract/inventory.py`) declares the same principal and scope per call.

### REQ-0037 — outside `CELINE_ENV=dev` a bound community without its collector secret refuses boot; in dev it is the transition, with a warning

At startup, after the manifests are loaded, every community bound to a dataspace
organisation (with `DATASPACE_ENABLED`) whose `SVC_DS_COLLECTOR_<ALIAS>_SECRET` is unset,
or equal to the client id, is a posture violation: all of them refused in one message
anywhere but `CELINE_ENV=dev` (the rule of [REQ-0028](deployment-posture.md)). A call made
for such a community outside dev is refused too.

Under `CELINE_ENV=dev` the same list is one warning and startup proceeds; a community
without a collector secret keeps the pre-collector clients — the consent write and
read-backs as its connector client `svc-ds-connector-<alias>` (`DS_ORG_CLIENT_SECRET`),
every other call as `svc-ds-onboarding` — warned once per community and scope. A community
whose collector secret is set uses the collector client in dev as anywhere else.

### REQ-0038 — a membership delete the registry refuses is a failure, and a rollback hides neither failure

`DELETE /admin/memberships/{did}/{alias}` answered with 400 or more (other than 404, which
means the row is gone), or not answered, raises. Revocation then stops before the
credential is deleted and the submission keeps its identity columns. In the rollback after
a failed Keycloak sync, the credential is still revoked when the membership delete fails;
the error names every undo that failed, and the Keycloak failure remains its cause.

### REQ-0039 — the member's own calls carry their credential and their own login token, never this service's

`GET` and `POST /consent/my/shares` and `GET /prov/my/events` are sent with the member's
credential (`X-Subject-Id`, `X-User-VC`) and, as `Authorization: Bearer`, the token the
member called this service with (ds ADR-0024). This service's own token is never sent to
these routes; a request carrying no member token sends none.

### REQ-0040 — a 404 on a membership delete or a credential revocation succeeds, and is logged as a misalignment

`DELETE /admin/memberships/{did}/{alias}` and `DELETE /admin/credentials/{id}` answered 404
leave the state a delete wants, so revocation (and the rollback after a failed Keycloak
sync) carries on. Each such 404 is one `WARNING` starting `MISALIGNMENT`: this service
recorded a membership or a credential that the identity registry does not hold — removed
behind its back, by an operator cleaning up a suspended organisation say, or never
written. The line names the community alias and the submission reference (in the rollback,
the credential id), never the member's email or DID.
