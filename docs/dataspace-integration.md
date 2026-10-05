# Dataspace Identity Integration

## Overview

Dataspace identity provisioning needs **two gates open**: `DATASPACE_ENABLED` for the deployment, and a `dataspace:` block in that community's manifest. A REC without a block gets no credential even when the deployment is enabled — issuing one would hand somebody an identity belonging to no organization, which the consent endpoints refuse to act on anyway.

Provisioning a participant's **login** is a separate gate (`PROVISIONING_URL`), so it can be used on its own: participants get a login, and no dataspace is involved. It is not a Keycloak call from here -- see [Participant login settings](#participant-login-settings).

When both gates are open and a submission is approved, the system provisions a dataspace identity for that user. This includes:

- A **DID** (Decentralized Identifier) for the user as a data subject
- A **Verifiable Credential** (VC) binding the user to a participant organization
- An **organization membership** registering the DID as a member of the REC in the identity-registry
- A **Keycloak `dataspace_did` attribute** linking the user's login identity to their DID (written by the identity registry, not from here)
- **Data-sharing shares** provisioned to the dataspace connector for the offers the user consented to during onboarding (optional; runs last and is non-fatal)

The membership matters as much as the credential: ds consent endpoints (`/consent/my/shares`) check membership before allowing a data subject to manage sharing preferences. A user with a VC but no membership holds a valid identity that cannot do anything.

Identity provisioning is triggered when an admin changes a submission's status to `approved`. The onboarding service communicates with the **identity-registry** HTTP API using machine-to-machine (M2M) authentication. If provisioning fails, the approval is rejected -- no partial state is left behind.

Onboarding stores only the subject ID, DID, credential ID, and issuance timestamp. The credential itself lives in the identity-registry's credential store.

## Flow

When a submission is approved, `DATASPACE_ENABLED` is true and the REC declares a `dataspace:` block:

1. `provision_participant()` asks the provisioning service to ensure the account, **without** inviting the participant yet, and returns its Keycloak `user_id` and `username`. The invitation is sent by `invite_participant()` once every fail-closed step has succeeded; see [The invitation](#the-invitation).
2. `provision_user_identity()` calls the identity-registry to issue a credential and sync the DID to Keycloak, then provisions any data-sharing shares to the connector as its last step.

```mermaid
sequenceDiagram
    participant Admin
    participant Onboarding
    participant Prov as provisioning
    participant IdRegistry as identity-registry
    participant KC as Keycloak
    participant Connector as ds-connector

    Admin->>Onboarding: PATCH /api/admin/submissions/{id}<br/>status: approved

    Note over Onboarding: provision_participant()
    Onboarding->>Prov: PUT /participants/{community}/{key}<br/>{email, first_name, last_name}
    Prov->>KC: Ensure account, organization, org group
    Prov-->>Onboarding: {user_id, username, created}

    Note over Onboarding: provision_user_identity()

    Note over Onboarding: as svc-ds-onboarding (this service)
    Onboarding->>IdRegistry: GET /users/resolve?email=…&derive=false
    IdRegistry-->>Onboarding: {subject_id, did} or 404 (no mapping)
    Note over Onboarding: no mapping: reuse the id the submission recorded,<br/>else mint uuid4, and record it before issuing

    Note over Onboarding: as svc-ds-collector-&lt;alias&gt;,<br/>one token per scope
    Onboarding->>IdRegistry: POST /admin/credentials/data-subject<br/>{subject_id, role, allowedActions, ttlDays,<br/>linkedParticipantDid, verifiedBy, verificationMethod}
    IdRegistry-->>Onboarding: {subjectDid, credentialId, generatedAt}

    Onboarding->>IdRegistry: POST /admin/memberships<br/>{user_did, organization_alias, role}
    IdRegistry-->>Onboarding: 201 Created (or 409 already exists)

    Note over Onboarding: as svc-ds-collector-&lt;alias&gt;<br/>(scope identity-registry.keycloak.sync)
    Onboarding->>IdRegistry: POST /admin/keycloak/sync<br/>{did, keycloak_user_id, keycloak_realm, username}
    Note over IdRegistry: records (realm, user id) -> DID,<br/>only for a DID linked to this community
    IdRegistry-->>Onboarding: 200 OK

    Note over Onboarding: provision_user_shares() — last step,<br/>only if DS_CONNECTOR_URL set + consent given
    loop each consented offer id
        Note over Onboarding: as svc-ds-collector-&lt;alias&gt;, at the<br/>connector that holds this offer's data
        Onboarding->>Connector: POST /consent/admin/shares<br/>{subject_id: DID, offer_id, enabled: true,<br/>decided_by: subject, legal_basis, keys?}
        Connector-->>Onboarding: 200 OK
    end
    Note over Onboarding: On share failure: non-fatal —<br/>leave share_provisioned=false, do NOT roll back

    Onboarding-->>Admin: 200 OK (approved + identity provisioned)

    Note over Onboarding: On KC sync failure:
    rect rgb(255, 240, 240)
        Onboarding->>IdRegistry: POST /admin/keycloak/sync (retry, up to 3x)
        IdRegistry--xOnboarding: failure
        Onboarding->>IdRegistry: DELETE /admin/memberships/{did}/{alias}
        Onboarding->>IdRegistry: DELETE /admin/credentials/{credentialId}
        Note over Onboarding: Membership removed, credential revoked,<br/>approval rejected
    end
```

Provisioning takes **facts, not a database row**. `provision_subject(access, facts, binding)` is the whole of it, and `provision_user_identity(submission, ...)` is the approval path's caller: it reads a `SubjectFacts` off the submission and writes the resulting `SubjectIdentity` back. A person who was admitted some other way -- screened offline, with no submission and never one -- is provisioned by filling in the same facts from wherever their admission is recorded. On a deployed realm nobody is admitted another way ([ADR-0011](decisions/ADR-0011-on-a-deployed-realm-every-member-enters-through-onboarding.md)); that path serves local stacks seeded from a bundle. `RegistryAccess` carries the registry URL and one token through the whole flow, so resolve, check and issue cannot address different instances.

### Step-by-step

1. **Login provisioning** -- `provision_participant()` calls `PUT /participants/{community}/{key}` on the provisioning service, which ensures the account, its REC organization and its org group, and returns the Keycloak `user_id` and the `username` the account authenticates as. The body always carries `invite: false` and, when there is one, the participant's `locale`, which the account keeps for the invitation sent later (see [The invitation](#the-invitation)). Nothing here touches Keycloak; see [Participant login settings](#participant-login-settings). This runs before identity provisioning so the user id is available for the sync step.

2. **Subject resolution** -- `GET /users/resolve?email=…&derive=false` asks the identity-registry whether it already maps this person. If it does, that `subject_id` is reused: one human keeps one DID, and minting beside it would split their consent records and provenance in two. A `404` is the registry's answer for *no mapping* and is not an error. Then onboarding reuses the id the submission already recorded, if it has one, and otherwise mints a random **UUIDv4**. The id is written onto the submission **before** issuance. The registry is never asked to derive one.

    **Why a random id, and why it is recorded first.** The subject id becomes the `<id>` of the person's DID verbatim, and ds's rule `D-22c` puts the obligation on whoever generates it: it must not reveal the person. A UUID is derived from nothing, so it reveals nothing. It also cannot be derived again, so an id that is minted and not recorded is lost, and the next attempt gives the same person a second DID. Issuance creates the DID but no mapping; only the Keycloak sync in step 6 writes the mapping. So if the step fails between the two, enablement commits the failed step together with the submission, and the retry finds the id there. A revocation clears the credential columns and keeps this one.

    A **`409`** means the identifier matches a mapping carrying a different Keycloak user id. The registry quarantines rather than reconciles, because "account re-created" and "address recycled to a different human" are indistinguishable from there. It surfaces as `SubjectIdentifierConflictError` and is logged at error level with the identifiers: only an operator can resolve it, and retrying never will.

3. **Credential issuance** -- `POST /admin/credentials/data-subject` sends the subject id, role, allowed actions, TTL and the REC's `linked_participant_did` to the identity-registry. The registry derives the user's DID, issues a Verifiable Credential, and returns `{subjectDid, credentialId, generatedAt}`.

    It also sends **`verified_by` and `verification_method`** -- who established this person's identity, and how. The dataspace layer does no KYC by design; whoever runs onboarding does, and the credential records it rather than implying an assurance level nobody established. `verified_by` is the REC's `dataspace.organization_did`, omitted when the manifest declares none, because naming an empty authority is worse than naming none. `verification_method` is `submission-review` followed by the method the operator recorded before approving — `submission-review:offline` or `submission-review:uploaded-document` (see [Operator console](admin-console.md#before-approving-the-recs-verification)). It is never a setting: a deployment able to edit it could make the credential claim a check that never happened. A submission approved before verifications were recorded, whose enablement is retried, sends the bare `submission-review`. The value is plain text in `credentialSubject.verificationMethod`; the word is also the W3C DID-document term, with a different meaning.

    **Issuance is idempotent per role.** The registry reuses the subject DID -- one human keeps one identifier across organisations -- and a repeat call for somebody who already holds an active credential *in the same role* returns that credential's id and re-delivers it to the custodian, rather than minting a second one and spending a status-list index that is never recovered. A different role does mint, because roles are additive. This was **not** true when the two doors below were written -- every call minted -- and both guards remain correct for reasons that have moved.

    The **approval path** does not guard: a manager has just approved this submission, and the row needs a `dataspace_vc_id` of its own to stay revocable, which it gets whether the registry minted or matched, since the response names the credential either way. The **member's wizard** guards by resolving first and provisioning only when `resolve_subject_and_credential` answers `None` -- a stronger question than `GET /credentials/check` (it asks whether the member holds an *active, unexpired and presentable* credential, which is what they actually need) and asked with a call that path already makes. The registry's own per-role match is a floor and not a substitute for it: it says a credential exists, not that this service can resolve one to present. `/credentials/check` and the `identity-registry.credentials.read` grant it would have needed are therefore not used here.

4. **Organization** -- onboarding **does not create one**, by design. See "Why onboarding never creates an organization" below. The organization named by the REC's manifest must already exist and be promoted in the identity registry; a `404` from the membership call means it was never seeded, and says so.

5. **Membership registration** -- `POST /admin/memberships` registers the user's DID as a member of the REC organization. A `409 Conflict` is treated as success; a `404` means the organization does not exist. Without this step the user cannot use the ds consent endpoints, which gate on `GET /memberships/check`.

    **No role is sent.** A membership says *where* somebody belongs; what they are there is a `communityRole` claim on their data-subject credential, changed by reissuing it. The registry once accepted a role here and stored it in a column nothing read, and has since dropped the column -- so a manifest's `dataspace.membership_role` recorded nothing while reading as though it did. The key is gone; a manifest that still carries it is ignored rather than rejected.

6. **Keycloak DID sync** -- `POST /admin/keycloak/sync` writes the identity registry's mapping from the user's Keycloak login (realm, user id) to their dataspace DID. ds person routes read that mapping to decide which login acts as which person (ds ADR-0024), so it is the **community's act**, made as its collector client with `identity-registry.keycloak.sync` alone (ds ADR-0026, amended 2026-10-05). ds accepts it only for a DID holding a credential, not revoked, linked to that community -- the one issued at step 3 -- and never to rebind the DID to another login. The token is asked for before step 3, so a realm that does not grant the scope yet stops the approval before anything is issued.

    It also sends the **username** Keycloak returned at step 1 -- the same value step 2 of enablement wrote into `Member.user_id`. That is what lets a dataspace decision be applied to rows: the connector translates consenting subject DIDs into usernames through this registry (`POST /users/identities`, which reads `KeycloakMapping.username` and falls back to `email`) and hands them to the celine `dataset-api`, which resolves them against `Member.user_id`. Sending it keeps both ends naming one person the same way. Omitting it leaves the email fallback standing, which is correct only while username == email -- the convention the provisioning service uses for an account it *creates*, and **not** the platform's: it also adopts an account whose username is something else, and for them the row filter would resolve nobody and the data plane would deny rows a person had consented to. The value is optional because a retry of step 3 alone has no provisioning result to read it from; the key is then omitted rather than sent as null, so a good value already in the registry is never overwritten with nothing.

7. **Rollback on failure** -- If the Keycloak sync fails after 3 retries, the membership is removed via `DELETE /admin/memberships/{did}/{alias}`, the credential is revoked via `DELETE /admin/credentials/{credentialId}`, and the approval is rejected. A `404` on either delete is the state the delete wants and is not a failure, but it is logged as a `MISALIGNMENT` warning naming the community and the submission reference (never the email or the DID): this service recorded something the registry does not hold, which is what an operator's clean-up of a suspended organisation looks like from here ([REQ-0040](specifications/dataspace-acting-for-a-community.md)). This prevents orphaned credentials and memberships that have no corresponding Keycloak mapping. **The rollback never hides why it ran**: each undo is attempted whatever the other answered, an undo the registry refuses (any answer of 400 or more but 404) is logged and named in the error, and the Keycloak failure stays the error's cause. The same strictness applies on revocation: a refused membership delete stops it before the credential is touched and the submission keeps its identity columns, rather than reporting success over a row that still counts the person as a member. The DID itself stays at the registry with no mapping. A retry reissues under it, because the subject id was recorded on the submission in step 2.

8. **Data-sharing share provisioning** -- `provision_user_shares()` runs as the last step, after the Keycloak DID sync. When `DS_CONNECTOR_URL` is set and the submission's `data_sharing_consent` is true, it POSTs once per recorded offer id to `{connector}/consent/admin/shares` with body `{subject_id: <dataspace DID>, offer_id, enabled: true, decided_by: "subject", legal_basis: {source: "onboarding", rec_slug, consent_text_version, locale, rendered_text_sha256, accepted_at, submission_ref}}`. It names an offer, never a dataset. The call is idempotent and sets `share_provisioned=true` on success. Unlike step 7, it is **deliberately non-fatal**: a failed share never rolls back the identity or rejects the approval -- it leaves `share_provisioned=false` for retry. It reads every connector holding the member's offers first and writes only where one disagrees with the member's newest decision, withdrawals included, or where a holder's standing grant carries other keys than the member's supply points in the registry now -- see [data-sharing](data-sharing.md).

   **Registered as the community, not as this service.** The connector decides what a caller may do from the organisation its token names, and a plain service client names none -- so one could write a consent at any connector for anybody's members. ds refuses `svc-ds-onboarding` here. The call is made as the community's own collector client, `svc-ds-collector-<alias>`, with `connector.consent.provision` alone -- see [Acting for a community](#acting-for-a-community).

   **`decided_by` says whose decision it is**, and it is not a detail. `subject` relays a decision somebody took -- which is what a form is -- and a relayed *withdrawal* is then theirs, so no later provisioning run lifts it. `collector` records one the organisation took itself, which is what a withdrawal on revoked membership is: nobody withdrew, the community revoked a membership and the consent went with it.

   **Each offer goes to the connector that holds the data it reaches.** A consent is enforced where the rows are served, so a decision about readings a grid operator holds is recorded on the grid operator's connector or it enforces nothing. The routing is the manifest's `dataspace.connectors` -- holder, url, the offers it holds -- and an offer nobody routes stays at `DS_CONNECTOR_URL`. An offer whose data sits in several places is named by each holder — the community's own connector by an entry for its own alias, with no url — and is recorded and withdrawn at every one ([ADR-0007](decisions/ADR-0007-an-offer-is-recorded-at-every-connector-that-holds-its-data.md)). It is configuration and never inferred from the offer: `recipients.recipient` names who the data goes *to*, which is not who holds it. Writing at another participant's connector also needs that participant to have recorded this community as an accepted consent collector; it answers `403` otherwise.

   **The member's supply points travel with a registration at a holder**, as typed keys (`keys: ["pod:…"]`), read from the rec-registry. That connector's data plane keys its rows by supply point and knows nothing about this community's members, so without them the consent is recorded and can never yield a row -- which is why a member with no supply point is refused here rather than registered. They are not sent to the community's own connector, which resolves its members without them, and ds refuses them on a withdrawal. Withdrawal follows the route the grant took, before the credential is deleted.

Step 5 is skipped entirely when the REC's manifest declares no `dataspace.organization`. Step 8 is skipped when `DS_CONNECTOR_URL` is unset.

### Retrying a failed share

`POST /api/admin/submissions/{id}/retry-share` re-runs `provision_user_shares()` with `raise_on_error=True`, returning `422` if the connector rejects the request. Use it to complete provisioning for a submission left at `share_provisioned=false`.

## Authentication

Every token is an OIDC client-credentials token (`celine.sdk.auth.OidcClientCredentialsProvider`), cached in memory per client **and per requested scope**, and renewed before expiry. Three principals reach the dataspace, and which one a call uses is decided by **whose act it is**:

| Principal | Client | For |
|---|---|---|
| this service | `svc-ds-onboarding` (`DS_ONBOARDING_CLIENT_ID` / `_SECRET`) | what it does as itself: resolve an organisation (`/owners/resolve`), resolve a subject (`/users/resolve`); and the REC registry member client when `DATASPACE_ENABLED` |
| the community | `svc-ds-collector-<alias>` (`SVC_DS_COLLECTOR_<ALIAS>_SECRET`) | everything done **for** a community: its members' credentials and memberships, the DID <-> Keycloak mapping (`/admin/keycloak/sync`), their consent registrations and read-backs, the audience read |
| the member | their credential (`X-Subject-Id` + `X-User-VC`) **and** their own login token | their own decisions and history (`/consent/my/shares`, `/prov/my/events`) |

### Acting for a community

An act for a community is that organisation's act: the receiver binds it to the organisation the token's `sub` names (the collector client's `sub` is the organisation's DID), and a shared service client -- whose secret every onboarding operator would hold -- names none. So each community bound to the dataspace has its own collector client, `svc-ds-collector-<alias>`, provisioned by ds for an organisation that collects consent, where the alias is the manifest's `dataspace.organization`. A deployment serving several communities holds one secret per community:

```text
SVC_DS_COLLECTOR_<ALIAS>_SECRET    alias upper-cased, '-' -> '_'   e.g. SVC_DS_COLLECTOR_EXAMPLE_REC_SECRET
```

Set in the environment, or in `.env` / `.env.local` (the environment wins).

**One scope per token.** The collector client has no default ds scopes; each token asks for exactly the one optional scope its route requires, and each scope adds exactly one audience. A receiver requires its own audience and refuses a collector token naming another ds service's, so a token replayed anywhere else is useless, and a read never travels with a write's token:

| Call | Scope | Audience |
|---|---|---|
| `POST /admin/credentials/data-subject`, `DELETE /admin/credentials/{id}` (IR) | `identity-registry.credentials.write` | `svc-ds-identity-registry` |
| `POST /admin/memberships`, `DELETE /admin/memberships/{did}/{alias}` (IR) | `identity-registry.memberships.write` | `svc-ds-identity-registry` |
| `POST /admin/keycloak/sync` (IR) -- at approval, and an email correction's re-sync | `identity-registry.keycloak.sync` | `svc-ds-identity-registry` |
| `POST /consent/admin/shares` (connector) | `connector.consent.provision` | `svc-ds-connector` |
| `GET /consent/admin/subject-shares`, `GET /consent/admin/decisions` (connector) | `connector.consent.collector.read` | `svc-ds-connector` |
| `GET /consent/admin/shares` -- the POD export's audience read (connector) | `connector.consent.audience` | `svc-ds-connector` |

`connector.disclosure.record` and `provenance.write` are the collector's too, and nothing here calls them: this service records no disclosure (ADR-0010) and only reads provenance, as the member. `tests/contract/inventory.py` declares each call's principal and scope.

**Outside `CELINE_ENV=dev`, a community without its collector secret refuses boot** -- every community bound to a dataspace organisation, with `DATASPACE_ENABLED`, all of them in one message -- and so does a secret equal to the client id. See [REQ-0037](specifications/dataspace-acting-for-a-community.md).

**The development transition.** A local stack whose ds and realm predate the collector client has none to give. Under `CELINE_ENV=dev` only, a community without a collector secret keeps the pre-collector clients, with a warning naming the variable: the consent write and read-backs as its connector client `svc-ds-connector-<alias>` (`DS_ORG_CLIENT_SECRET`, `DS_ORG_CLIENT_ID` overriding the derived id), every other call as `svc-ds-onboarding`. Never outside dev, and never for a community whose collector secret is set.

### The member's own calls

The member's own decisions and history are read and written **as the member**: the credential this service resolved for them (`X-Subject-Id` + `X-User-VC`) says who the subject is, and the member's own Keycloak token -- the one they called this service with, forwarded as `Authorization: Bearer` -- says it is them; ds binds the two by realm and `sub` (ds ADR-0024). This service's own token never goes to a person route.

### The REC registry lookup

This service's token carries **`rec-registry.lookup`** for the other half of the POD export (whose audience read is the collector's, above): the DIDs the connector returns are resolved to supply points through `POST /admin/lookup/members-by-dids` on the rec-registry, which is also where `set_member_did` wrote the DID at step 6. One grant covers both lookup actions -- `rec_registry/access.rego` grants `lookup` and `assets.lookup` from it -- so there is no `rec-registry.assets.lookup` to declare. It is granted in `celine-policies/clients.ds-host.yaml`, the host overlay, because `rec-registry.*` is celine's vocabulary added on top of a client ds declares.

## Configuration

### Identity provisioning settings

| Variable | Default | Description |
|---|---|---|
| `DATASPACE_ENABLED` | `false` | Deployment-wide gate. When `false`, no dataspace identity provisioning happens anywhere. Participant logins are gated separately by `PROVISIONING_URL`, so they can be used on their own to give participants a login without any dataspace. |
| `IDENTITY_REGISTRY_URL` | *(none)* | Base URL of the identity-registry service (e.g. `http://identity-registry:8000`). Required when `DATASPACE_ENABLED=true`. |
| `OIDC_BASE_URL` | *(none)* | OIDC issuer for M2M token acquisition — the **`celine` realm** (e.g. `http://keycloak.celine.localhost/realms/celine`). One realm for every outbound call this app makes; realm alignment converges there, so do not point it at the dataspaces realm. Required when `DATASPACE_ENABLED=true`. |
| `DS_ONBOARDING_CLIENT_ID` | `svc-ds-onboarding` | Keycloak client ID for M2M authentication. |
| `DS_ONBOARDING_CLIENT_SECRET` | *(none)* | Keycloak client secret for M2M authentication. Required when `DATASPACE_ENABLED=true`. |
| `SVC_DS_COLLECTOR_<ALIAS>_SECRET` | *(none)* | One per dataspace-bound community: the secret of `svc-ds-collector-<alias>`, which every act for that community is made as. Required outside `CELINE_ENV=dev` (boot refuses a missing one, or one equal to the client id). |
| `DS_ORG_CLIENT_ID` | *(derived)* | **Development transition only.** The community's connector client, used under `CELINE_ENV=dev` for a community without a collector secret. Empty derives `svc-ds-connector-<alias>`. |
| `DS_ORG_CLIENT_SECRET` | *(none)* | Its secret, for the same transition. Read nowhere else. |

### Participant login settings

This service **holds no Keycloak grant at all**, and a deployment that gives it one is
refused at boot. Approval asks `celine-policies`' **provisioning service** for the login
instead -- one service, the only writer of participant accounts in the realm, reachable
only from inside the network.

It used to administer the realm itself, under a fine-grained admin permission over one
group (`/participants`). That was the narrowest grant Keycloak can express and it was
wrong twice over: a service facing the public wizard held admin rights over accounts, and
it could not finish the job anyway -- a participant's community membership is a Keycloak
**organization**, which the Organizations API owns and no fine-grained permission reaches.
Accounts landed in a group and in no organization, which is the claim every org-scoped
policy resolves them by. See
[ADR-0004](decisions/ADR-0004-ask-the-provisioning-service-instead-of-administering-the-realm.md).

The identities this service presents are granted by different people for different
things, and asking for a login in celine's realm is celine's business. One of them is
not this service's at all -- everything done for a community is that organisation's act,
so it is done under the community's own collector client:

| Identity | Is | Used for |
|---|---|---|
| `OIDC_CLIENT_ID` (`svc-onboarding`) | celine's own client | Asking the provisioning service for a login -- a **scope**, not a Keycloak grant; the Digital Twin's boundary lookups; the registry sync; and the REC registry member client **when `DATASPACE_ENABLED` is false** |
| `DS_ONBOARDING_CLIENT_ID` (`svc-ds-onboarding`) | the dataspace's client | Resolving organisations and subjects, the DID <-> Keycloak mapping; and the REC registry member client (registration, DID write, supply-point lookups) **only when `DATASPACE_ENABLED` is true** |
| `svc-ds-collector-<alias>` (`SVC_DS_COLLECTOR_<ALIAS>_SECRET`) | **the community's** client, not this service's | Its members' credentials and memberships, their consent registrations and read-backs, the audience read -- one scope per token |

### The two calls, and what they are keyed on

| Call | When |
|---|---|
| `PUT /participants/{community}/{key}` | enablement step 1, on approval |
| `POST /participants/{community}/{key}/disable` | revocation |

`community` is the REC manifest's `rec_registry.community` and `key` is `submission.ref`
-- exactly the pair this service already registers the member under. One alias from one
place, because the provisioning service files the account into that community's Keycloak
organization, and the sweep that later checks the filing reads the community's own id from
the registry export.

The scope is `provisioning.participants.write`, declared for `svc-onboarding` in
`celine-policies`' `clients.yaml` together with the audience mapper onto
`svc-provisioning`. It is not granted by hand; `keycloak sync` applies it.

**`username` comes back from the call and is never computed here.** The provisioning
service reads it off the account, which may authenticate under a convention this platform
never chose -- a participant seeded from a registry file, or an account made by hand. That
value is what becomes the registry's `Member.user_id`; the response's `user_id` is the
Keycloak uuid, which is a different thing with an unfortunately similar name.

**A REC that declares no `rec_registry` block gets no login**, and step 1 says so.
Revocation resolves `(community, key)` through the registry export, so a login provisioned
for such a REC could never be revoked through this seam -- and the community alias would
have to be invented, creating an organization no reconcile looks at. An unrevokable login
is worse than an absent one.

**Revocation closes the login before it deactivates the member.** The registry export
carries only `active` members, so the reverse order would make the disable a `404`: the
login survives, the participant can still sign in, and the step row reports success
because nothing failed. `enablement.REVOKE_ORDER` declares the order for that reason.

**Revocation withdraws the sharing grants before the identity, and the identity waits
for it.** The withdrawal is keyed on the DID the identity step clears, and the connector
admits the community's read and write only for a member of its organisation — the
membership that step deletes. So when the withdrawal fails, the identity is not revoked
in that pass; revoking again runs both, in order.

### The invitation

**Step 1's upsert carries `invite: false`**, including on a retry. The invitation is
sent after the steps, by `POST /participants/{community}/{key}/invitation` with intent
`invitation`, and only once every fail-closed step (1–3) is `succeeded` or `skipped`
and step 1 still reads `not_requested`. An approval that fails at the registry or the
dataspace identity has therefore emailed nobody (celine-eu/onboarding#8). The account
step 1 created cannot be disabled at that point: revocation resolves the member through
the registry export, and there is no member yet. It has no credential, and nobody is
told it exists.

This service cannot see credentials, so it does not decide whether an account needs
an invitation: the provisioning service sends one only to an account without a
password, and at most one email per account within its cooldown. Keycloak writes the
email; nothing here sends one about the login. The route is the one the community
dashboard's button reaches through [the member-keyed routes](api-reference.md), under
the same scope.

`locale` is the submission's own (the language the person last used in the wizard),
then the REC manifest's `locale`, then absent, which leaves the realm default. Both
are **narrowed to `it|en|es`, and anything else is sent as absent**: the service
answers `422` for any other value, and a `422` would fail step 1 closed and block an
approval over a language tag. A manifest saying `it-IT` therefore gets the realm
default, which is the email Keycloak would have sent anyway.

The recorded `invitation` is `sent` or `not_on_dev_list` from the answer, or the
refusal's code — `has_password`, `account_disabled`, `no_email` (`409`), `cooldown`
(`429`), `send_failed` (`502`) — and `not_requested` until it is sent. Any other
refusal, or no answer, is recorded as `send_failed` and logged. None of them fails the
step or the approval. It is stored on the step row beside `detail`, and the console
translates it ([Operator console](admin-console.md#the-invitation-for-the-operator)).
`account_disabled` is a participant approved again after revocation: revocation
disables the account, and neither answer re-enables it. `send_failed` is the one
outcome a retry that names `keycloak_user` re-runs although the step succeeded;
`cooldown` and `no_email` are not re-run.

The provisioning service is also called a second way, from outside approval. A community
manager's "Send invitation" and "Reset password" on the `celine-community` dashboard reach
`POST /participants/{community}/{key}/invitation` through this service's member-keyed routes.
Those calls name the intent, pass every code through, and touch no step row. See
[api-reference.md](api-reference.md) and
[ADR-0005](decisions/ADR-0005-onboarding-is-the-one-caller-of-the-provisioning-service.md).

### What each refusal means

| | Meaning |
|---|---|
| `401` | The credential did not verify. Check `OIDC_CLIENT_SECRET`. Reported as a misconfiguration -- a platform operator's to fix, not the reviewing operator's to read |
| `403` | It verified and does not carry `provisioning.participants.write`. The `clients.yaml` declaration has not been synced. Also a misconfiguration |
| `404` `member_not_found`, `account_not_found` | On a revocation: no member under that key, or no account for one. Counted as done, because there is nothing left to revoke |
| `404` `community_not_found` | The REC manifest's `rec_registry.community` names a community the registry does not hold. Reported as a misconfiguration, and on a revocation it **fails the step**: the service could not look the member up, so a login may still be enabled |
| `404` with any other code, or none | Fails the step. A codeless `404` is as likely a wrong `PROVISIONING_URL` as a missing member, so it is never read as "nothing to revoke" |
| `502` `registry_unavailable`, `provisioning_failed`, `send_failed` | The registry or Keycloak failed behind the provisioning service. **A dependency, not a refusal** -- the retryable case the step row exists for, and the step error names which |

Every refusal carries `{"detail": {"code", "message"}}` since the provisioning service's
1.2.0 contract, and this service branches on the `code` (`ProvisioningApiError.code`),
never on the message. The message goes to the log.

Enablement sees no `409` any more, and nothing to adopt by hand: the provisioning
service has realm-wide reach, so an account created by anything else is found by address
and adopted. ADR-0003's unreachable-duplicate case is gone with the grant that caused it.

| Variable | Default | Description |
|---|---|---|
| `PROVISIONING_URL` | *(none)* | The provisioning service's **internal** address (`http://provisioning:8010` under compose). Unset onboards participants and gives them no login, which is a supported deployment. It has no public route and must not be given one: it holds realm-wide Keycloak administration and is safe to hold it only because nothing outside the network can reach it. |
| `OIDC_CLIENT_SECRET` | *(none)* | The secret for `OIDC_CLIENT_ID`, presented to the provisioning service. Required when `PROVISIONING_URL` is set. |
| `DATASPACE_KEYCLOAK_REALM` | *(the realm `OIDC_BASE_URL` names)* | The realm the account **lives in**, which the dataspace step hands to the identity registry. Not an administration setting -- nothing here administers a realm. Set it only where the issuer URL names no realm; startup refuses a pair that disagrees, because the provisioning service writes into the realm this deployment's own issuer names. |

`DATASPACE_KEYCLOAK_ADMIN_USERNAME`, `_ADMIN_PASSWORD` and `_ADMIN_CLIENT_SECRET` were the
password-grant login as a realm administrator. `DATASPACE_KEYCLOAK_ENABLED`, `_BASE_URL`,
`_PARTICIPANTS_GROUP` and `_UPDATE_EXISTING` configured the group-scoped grant and are gone
with it. **Startup refuses to run with any of them set** -- remove them, rotate what the
credentials held, and keep `_REALM`. `ENABLED=true` is the one that has to be refused
rather than warned about: it reads as "participants are being given logins" while nothing
reads it, so none would be, one approval at a time. See
[ADR-0001](decisions/ADR-0001-provision-logins-as-the-service.md) through
[ADR-0004](decisions/ADR-0004-ask-the-provisioning-service-instead-of-administering-the-realm.md).

### Dataspace policy settings

These settings control what goes into the issued credential:

| Variable | Default | Description |
|---|---|---|
| `DATASPACE_USER_ROLE` | *(none)* | Role assigned in the credential (e.g. `member`). |
| `DATASPACE_ALLOWED_ACTIONS` | *(none)* | Comma-separated actions the user is authorized for. |
| `DATASPACE_VC_TTL_DAYS` | *(none)* | Credential validity period in days. |

### The per-community binding lives in the manifest

Which dataspace organization a community's members belong to is **not** a
deployment setting. It is per community, in `templates/<slug>/manifest.yaml`:

```yaml
dataspace:
  organization: example-community          # = KC org alias = IR owner id
  organization_did: did:web:example-community.dataspaces.localhost
  linked_participant_did: did:web:consumer.dataspaces.localhost
```

| Key | Notes |
|---|---|
| `organization` | Owner `id` in the identity registry. Must match `^[a-z0-9][a-z0-9-]*[a-z0-9]$` and an owner that already exists there. Validated by `task import-templates`. |
| `organization_did` | Optional DID for the organization; used as the disclosing agent on provenance events. |
| `linked_participant_did` | Optional participant the issued credential is linked to. |

`organization` is **required** when the block is present. There is no "in the
dataspace but a member of nothing" state: a credential without a membership is an
identity that cannot do anything, since the consent endpoints gate on membership.

**Omit the whole block and the community is not in the dataspace**: the full
wizard runs, no sharing consent is collected and no identity is provisioned. That
is a supported configuration — onboarding works with no dataspace infrastructure
at all — not a degraded one.

The binding is per community because this platform is multi-tenant: manifests
live in the `Rec` table, every wizard route is `/api/{rec}/…` and every submission
carries a `rec_slug`. As deployment-wide settings, these filed **every** approved
member into one organization, silently — the wrong membership is still a
successful `201`.

There is deliberately **no deployment-wide equivalent**. A global alias is what
produced the defect above, and leaving one as a fallback would let it come back
the first time a manifest was written without a block.

### Why onboarding never creates an organization

`POST /admin/owners` is not called, and the capability was removed rather than
defaulted off.

An organization created from an approval carries **no verification, no agreement
and therefore no declared capacity** — and the registry's `status` column
defaults to `verified`, so such a row *reads* as verified while nothing verified
it. Capacity is what the connector's circle check reads to decide whether a party
requesting data is a processor of the controller (disclosed under a DPA) or an
independent controller (a new consent question for the member). With none
declared, the check resolves "outside the circle": the safe direction, for the
wrong reason, invisibly.

Organizations arrive through the registry's **verify → agreement → credential →
promote** chain, seeded by an operator from the deployment's `owners.yaml`. A
missing organization is a deployment error:

- at startup, the service **refuses to boot** when a bound community's
  organization is unknown to the registry;
- a registry that cannot be reached does *not* block boot — "no such owner" is a
  configuration error worth refusing on, "I could not ask" is not;
- at approval, a `404` from the membership call says the organization was never
  seeded.

### Data-sharing share settings

These control provisioning of data-sharing consent to the dataspace connector (step 7).

| Variable | Default | Description |
|---|---|---|
| `DS_CONNECTOR_URL` | *(none)* | The **community's own** connector: its members' decisions about its own data, and the member's `/consent/my/*` surface. When unset, share provisioning is skipped. A decision about data another participant holds goes to that participant instead — see the manifest's `dataspace.connectors`. |
| `SVC_DS_COLLECTOR_<ALIAS>_SECRET` | *(none)* | The community's collector client, which registers and reads back a consent at either connector (one per community). `DS_ORG_CLIENT_*` only in the development transition. |
| `DS_NS_URL` | *(none)* | Public vocabulary base (`GET /ns/sharing-offers`) the wizard renders offers from. When unset, falls back to the connector's `/ns` path. |

## Relation to REC registry registration

Approval provisions three things in order — the participant's login, REC registry member,
dataspace identity — and the order is not cosmetic. The registry keys a member on
`(community, user_id)`, so the login exists first; the dataspace identity
is last because it is the step that can be retried afterwards.

Registry registration **fails closed**, so a dataspace identity is never issued
to somebody who is not a community member. What the member is registered with — its
community, and its area from the template's boundaries or municipality lists — is in
[templates.md](templates.md#rec-registry-binding-optional-per-community); its role is
`prosumer` when the wizard's `has_pv` answer is yes and `consumer` otherwise. No meter is
registered: a meter's id is known only once it is installed, and a REC manager attaches it,
and corrects the role or area, afterwards on the `celine-community` dashboard. The POD,
names and email are corrected here instead, by revision, and carried to the registry
member, the Keycloak account, the identity registry and the holders' consent keys
([admin console](admin-console.md#propagation),
[ADR-0015](decisions/ADR-0015-a-correction-is-a-revision.md)).

A `409` on the member create is read by its reason, not its status. A taken member
key is this submission's own earlier attempt and counts as registered. Onboarding checks
that with `GET /lookup/member-by-user-id/{user_id}`, covered by the `rec-registry.lookup`
it already holds: if another member of the community holds this `user_id`, the key is not
this person's and the step fails. A taken `user_id`, a taken DID, a delivery point held by
another member, or any conflict it does not recognise also fails the step with the
registry's own message. None of them created a member, and recording success would leave
the participant approved and missing from the registry.

The traffic goes the other way once, too. After the dataspace identity step
succeeds, onboarding writes the minted DID back onto the registry member with
`PATCH /communities/{community}/members/{key}`, sending `did` and nothing else.
This is what lets the rest of the platform join a dataspace consent — which the
connector answers in DIDs — to the member who holds the supply points. It is a
second call rather than a field on the create because the DID does not exist when
the member is registered, and it fails the step when the registry refuses: `did`
is globally unique there, so a `409` means another member already holds it, and
retrying will not clear that.

The member carries two identifiers and they are not interchangeable. `key` is the
registry's own handle on the member and is the submission reference. `user_id` is
**the Keycloak username** — read back from provisioning, falling back to the
normalised email — because the registry resolves a self-service caller by matching
that column against their token's `preferred_username`. A member row holding
anything else looks correct everywhere and its owner is told `403 You are not a
member of any community` on every self-service route.

## Error Handling

The integration follows a **fail-closed** strategy:

- If `DATASPACE_ENABLED` is `true` and identity provisioning fails, the submission status change to `approved` is rejected. The admin sees an error and can retry.
- **Credential revocation on sync failure**: If the credential is issued successfully but the Keycloak sync fails after 3 retries, the membership is deleted and the credential is revoked via `DELETE /admin/credentials/{credentialId}`. This ensures there are no orphaned credentials or memberships without a corresponding Keycloak DID mapping.
- **Idempotent membership calls**: `409 Conflict` from `POST /admin/memberships` is treated as success, so re-approving or retrying a failed approval does not error out. A `404` names the missing organization. Any other 4xx/5xx aborts the approval.
- **Incomplete consent evidence is refused locally**: a data-sharing consent with no `consent_text_version` or no `rendered_text_sha256` is rejected at capture, and `provision_user_shares` pre-flights the same rule rather than sending a record the connector will `422`. That rejection is permanent — you cannot retrospectively prove what somebody was shown — so it is surfaced on the admin view rather than left in a log.
- **No partial state**: Either the full provisioning succeeds (credential + organization + membership + KC sync) or nothing is committed. The submission remains in its previous status.
- **Share provisioning is non-fatal**: Data-sharing share provisioning (step 7) runs after the identity is committed and is exempt from fail-closed. A connector rejection or error leaves `share_provisioned=false` and logs, but never rolls back the identity or the approval. Operators retry via `POST /api/admin/{rec}/submissions/{id}/enablement/retry` with `{"step": "dataspace_share"}` (the deprecated `…/retry-share` alias does the same).

## Dependencies

- `celine-sdk>=1.13.0` -- provides `celine.sdk.auth.OidcClientCredentialsProvider` for M2M token management
- `celine-sdk>=1.18.0` -- provides `celine.sdk.provisioning.ProvisioningClient`, which is the only way this service gives a participant a login; there is no fallback path
- `celine-sdk>=1.21.0` -- provides the `RecRegistryAdminClient` community, topology-node and area calls (area rename included) the registry sync writes through, and `RecRegistryApiError.code`, which it reports refusals by. `pyproject.toml` carries this floor
- `httpx` -- async HTTP client for identity-registry API calls
- **identity-registry** service -- must be deployed and accessible at `IDENTITY_REGISTRY_URL`
- **ds-connector** service -- required only for data-sharing share provisioning; must be accessible at `DS_CONNECTOR_URL`
- **Keycloak** -- must have the `svc-ds-onboarding` client with `identity-registry.organizations.read`, `identity-registry.resolve`, `identity-registry.keycloak.sync` and `rec-registry.lookup`. The grants for acts done for a community (`connector.consent.*`, `identity-registry.memberships.write` / `.credentials.write`, `connector.disclosure.record`, `provenance.write`) belong on the collector client, not here
- **The community's collector client** `svc-ds-collector-<alias>` -- provisioned by ds for an organisation that collects consent (`sub` = the organisation's DID, no default ds scopes, the scopes in [Acting for a community](#acting-for-a-community) optional). Its secret is `SVC_DS_COLLECTOR_<ALIAS>_SECRET`. In the development transition only: the connector client `svc-ds-connector-<alias>`, secret `DS_ORG_CLIENT_SECRET`. A holder's connector additionally has to have recorded this community as an accepted consent collector
