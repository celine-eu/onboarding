# Operator console

Where a REC manager reviews submissions, approves participants, and repairs what
approval did when part of it fails.

Served at `/admin` on the onboarding host, alongside the participant wizard.
Authorization is described in [authorization.md](authorization.md); this document
is about what the console does.

## Before approving: the REC's verification

Approval is refused until an operator has recorded **how the community verified
the participant**: that they are who they declare, and that they hold the declared
POD. The community verifies on its own responsibility, and onboarding does not
require documents for it:

| Method | Means | Needs |
|---|---|---|
| `offline` | The community checked the person's documents outside the platform | nothing stored here; refused where the community shares PODs with a grid operator (below) |
| `offline-with-evidence` | The same, and the operator attached the evidence the community checked | the file, which is hashed and **not stored**: only its fingerprint (sha256) is kept |
| `uploaded-document` | The operator checked a document the participant uploaded | the document, which must belong to this submission; its fingerprint is kept |

An uploaded document that turns out to be the wrong one is recorded as `offline`
once the community has checked outside the platform.

- **Who may record one:** the same capability as approving, `submissions.review`.
  Vouching for a person is part of the decision.
- **When:** while the submission is `submitted` or `under_review`. After approval,
  only a renewal with evidence (see below); after a rejection the API answers 409.
- **Corrections:** a verification is never edited. Recording another supersedes it;
  both stay in the submission's history, the newest is the one in force, and the
  trail shows `verification_recorded` then `verification_superseded`. The optional
  note is stored encrypted and is not copied into the trail.
- **What travels:** approval sends the method to the dataspace credential as
  `verificationMethod` (`submission-review:offline` or
  `submission-review:uploaded-document`) and to the registry member as
  `extra.identity_verification` (`method`, `verified_at`). Who recorded it stays in
  this service's audit trail.

### When the community shares supply points with a grid operator

A community whose manifest names a holder connector (a grid operator holding its members'
readings) sends, with every grant carrying a member's POD, its own **declaration that the
member holds that POD**: under which statement (`REC_ASSERTION_TERMS_FILE`), how and when
it verified, a code for who verified, and the fingerprints of the evidence. The grid
operator does not check who holds a POD; the community answers for the declaration. The
console explains this to the operator where the verification is recorded, in these words:

> This community shares its members' supply points (POD) with the grid operator. Each time
> it does, the community declares, on its own responsibility, that the member holds the
> POD, and names the evidence it checked. That is why a check done outside the platform
> counts here only with the evidence you checked attached, for example the bill or the
> identity document.
>
> The file is not stored on the platform. Only its fingerprint is kept: a code computed
> from the file, which later shows exactly which file was checked. Keep the file in the
> community's own records for the retention period agreed with the grid operator. The
> applicant is not asked for anything more.

What that means for the operator:

- **Plain `offline` is not offered.** Attach the evidence file, or choose a document the
  applicant uploaded. The applicant's form is unchanged.
- **Keep the evidence.** The platform keeps fingerprints, not files. An uploaded document
  is deleted once the member's account is active, so download it if it is your evidence.
- **Why the file is hashed and not stored:** the community keeps its evidence anyway
  (the terms say so), and this service does not want every member's identity document.
  A fingerprint is enough to show later that the file produced is the one that was
  checked; it reveals nothing without the file.
- **The grid operator's answers**, on the `dataspace_share` step:
  - *already registered for another person* — the same POD is shared for someone else at
    the grid operator. Check who holds it now.
  - *suspended* — the grid operator suspended the POD, usually after a change of contract
    holder. It is released again only after a **new verification** (with evidence)
    recorded after the suspension: record one, then retry the step. The step's report
    also lists suspended PODs (by count, never the code).
  - *assertion not accepted* — record a verification with evidence, then retry.
- **Renewal after approval.** An approved member can be verified anew, with evidence only,
  for the cases above (`verification_renewed` in the trail).
- **Retention.** A verification the grid operator accepted a declaration from is kept,
  without its note, for `ASSERTION_RETENTION_YEARS` (default 10, pending legal counsel)
  after the member's erasure.

The rule applies while `DS_KEY_ASSERTION` is on: by default everywhere but
`CELINE_ENV=dev`. See [specifications/pod-ownership-assertion.md](specifications/pod-ownership-assertion.md).

The console shows the verification panel on the submission and keeps Approve
disabled until one is recorded. The API is
`GET|POST /api/admin/{rec}/submissions/{id}/verifications`, and the submission's
`verification` field carries the one in force.

## Correcting declared data: revisions

From `submitted` on, a participant's **POD, first name, last name, email, fiscal
code and supply address** are corrected by **revision**, never edited:

- the admin `PATCH` answers 409 for them, and sending one of them is enough, whether
  or not it changes;
- the wizard's `PATCH` answers 409 for every field once the application is
  submitted.

People may not know their POD and enter it wrong; the operator checks it and fixes
it here. The Keycloak username never changes. Why:
[ADR-0015](decisions/ADR-0015-a-correction-is-a-revision.md).

Two of the fields have their own rules
([specifications/existing-members.md](specifications/existing-members.md), REQ-0026):

- **The fiscal code** is revisable after approval too, and propagates nowhere,
  since it never leaves this service. It is masked in the history like the POD.
- **The supply address** is revisable before approval only. It re-resolves the
  recorded boundary.

**A declared existing member** (the applicant ticked "I am already a member") may
arrive without a POD. Where the template skips the coverage step, they also arrive
without a checked supply address. The operator completes both from the community's
member register with the revisions above. Approval stays refused until the
submission holds a POD and, where the address was deferred, an operator has
revised it. The admin read lists what is still missing in
`existing_member_pending`.

- **Who:** `submissions.revise`, granted where `submissions.review` is (`managers`,
  `admins`). The operator vouches for the new value. A service account would need
  `onboarding.submissions.revise`, which `celine-policies` declares and grants to no
  client.
- **Evidence:** the method, as for a verification (`offline`, or `uploaded-document`
  naming a document on this submission), and a **required** note saying how the new
  value was checked. The POD is format-checked and upper-cased; a value cannot be
  emptied, and the value already held is refused.
- **When:** `submitted`, `under_review` or `approved` (otherwise 409). A `rejected`
  submission is reopened first; a draft is still the participant's.
- **What is kept:** one row per correction (`submission_revisions`), never edited:
  the value replaced and the new one, the method, the note (the two values and the
  note encrypted) and who (`actor_type` `operator`, the subject, email and client).
  Erased with the submission.
  The submission's column changes in the same transaction. Per field the newest is
  in force, and the oldest one's previous value is what the person declared. The
  trail records `revise` with the field and the revision id, never the values.
- **What reads it:** everything that reads the submission, approval included, so a
  correction before approval is what the login, the registry member and the
  dataspace identity are created with.
- **After approval:** the correction is then **propagated** to every system that
  holds a copy, one step per target, each recorded on the revision with its state
  (`pending`, `done`, `failed`, `skipped`), its attempts, an error code and a
  sentence. Never a value. A failed step is retried from the console.

### Propagation

| Field | Steps, in order | What each does |
|---|---|---|
| first / last name | `account_profile` | the Keycloak account's name, through the provisioning service (`PATCH /participants/{community}/{key}`) |
| | `registry_name` | the REC registry member's `name`, built as approval builds it (first and last name) |
| email | `account_profile` | the account's address. The **username never changes**; `emailVerified` is reset and a verification link goes to the new address only |
| | `invitation` | for a member who never set a password, the invitation to set one, again, now to the new address. The provisioning service checks: an account with a password answers `has_password` and the step is `skipped` |
| | `identity_mapping` | the identity registry's Keycloak mapping: **the same DID**, the new address, the username kept. What keeps the member reaching their Data sharing page, which finds them by their token's email |
| POD | `registry_delivery_point` | one registry write, `PUT …/delivery-points/{new}?replaces={old}`: the new POD added, the old one removed and the member's meters relinked to it, in one transaction. `{old}` is the POD the registry holds, so a correction that never reached it is not assumed; with none, a plain `PUT`. Body as approval's |
| | `consent_keys` | waits for the registry, then re-sends every grant the member holds at a holder with the new POD as keys, and grants one refused earlier for lack of a POD ([data sharing](data-sharing.md)). The holder records the key change (ds ADR-0022). The outcome is the count of grants re-sent and the reason names offers and holders, never keys |

- **A step sends what the submission holds now**, so a retry after a later
  correction never puts an older value back. A later correction of the same field
  marks the earlier one's unfinished steps `skipped` (superseded).
- `invitation` and `identity_mapping` wait for `account_profile`. Until the
  account has the new address the member still signs in with the old one, and a
  mapping moved ahead of it would lose them their Data sharing page.
- **Refusals the operator acts on:** `email_taken` (another account uses the
  address: check it with the member; nothing was changed), `account_disabled` (the
  login is revoked), `mapping_conflict` (the identity registry binds the account
  to another DID; an operator resolves it there), `delivery_point_held` ("this
  POD is already recorded for another member — check it with the member"; the
  registry's own sentence, which may name that member, is not shown),
  `previous_pod_not_found` (the registry no longer holds the POD being replaced:
  check the member there), `consent_refused` (a holder did not take every grant;
  details in the server log). `send_failed`,
  `registry_unavailable`, `provisioning_failed` and an unreachable service are
  worth a retry. `http_405` means a provisioning service older than API 1.4.0.
- **A member whose enablement was revoked:** the correction is recorded and
  every step is `skipped` ("enablement revoked"), at recording and at any later
  retry.
- **Retry:** `POST .../revisions/{revision}/retry` with `{"step"}` or nothing
  (every unfinished step). A named step runs whatever its state, then the steps
  that wait for it. Needs `submissions.revise`, and is audited (`revision_retry`).

The console's *Corrections* panel shows the history per field, newest first, with
the declared value marked (POD masked unless the identifiers are revealed), each
revision's steps with a retry on a failed one, and, for an operator holding the
capability, the form: field, correct value, method, document, note. The API is
`GET|POST /api/admin/{rec}/submissions/{id}/revisions` and
`POST …/revisions/{revision}/retry`. There is no CLI command for revisions yet.

## What approval actually does

Approving somebody **enables** them, which means four things landing in this
order:

| # | Step | Where | If it fails |
|---|---|---|---|
| 1 | Login identity | provisioning service | **blocks approval** |
| 2 | Community member | rec-registry | **blocks approval** |
| 3 | Dataspace identity | identity registry | **blocks approval** |
| 4 | Standing sharing consent | dataspace connector | approval stands |

The order is load-bearing: the registry keys a member on `(community, user_id)`,
so the login has to exist first, and the dataspace identity is later because it
is the step that can be retried afterwards.

Step 1 is not a Keycloak call from this service. It asks `celine-policies`'
provisioning service, which is the only writer of participant accounts in the
realm; this service holds no Keycloak grant. A community that declares no
registry binding has no community to key the account on, so step 1 is **skipped**
for it — see [ADR-0004](decisions/ADR-0004-ask-the-provisioning-service-instead-of-administering-the-realm.md).

Approval also **sends an invitation**: Keycloak emails the participant a link to set
their password, valid for 7 days, in the language they last used in the wizard (or
the community manifest's `locale`, or the realm default). **It is sent only after
steps 1–3 have all succeeded**, and recorded on step 1's row. Step 1 creates the
account without inviting anybody to it, so an approval that fails at step 2 or 3 has
emailed nobody: the account exists, with no password and no link to set one, and the
invitation goes out when approval is pressed again and completes. The provisioning
service decides whether an email goes out, because only it can see whether the
account already has a password. The step row records what it decided, and **every
outcome is a success**: the account exists, and an invitation that did not go out is
not a failed login.

| Code | Console says | Meaning |
|---|---|---|
| `sent` | invitation sent | a new account, or one without a password |
| `has_password` | already has a password | nothing to invite to |
| `not_on_dev_list` | not sent, recipient list | the provisioning service is in dev email mode and the address is not on its list |
| `account_disabled` | not sent, account disabled | a participant approved again after revocation: revocation disables the account, and re-approval does not re-enable it |
| `not_requested` | not sent yet | approval has not completed: a later step failed, and the invitation goes out when approval is pressed again and succeeds |
| `cooldown` | not sent, emailed moments ago | the account was sent an email within the provisioning service's per-account cooldown, so nothing was sent again |
| `send_failed` | not sent, the email could not be sent | Keycloak did not accept the send, or the provisioning service could not be asked; nothing went out and no cooldown started |
| `no_email` | not sent, no email address | the account the provisioning service holds for this member has no address, so no invitation can ever be sent to it |

The console shows the code translated; the CLI prints the code beside an English
sentence (`[invitation=account_disabled]`). Rows from before invitations existed
carry no code and show their old detail.

### The invitation, for the operator

**A send only ever follows a person's decision.** From this console that is you
pressing Approve, or you retrying a send that failed. Nothing scheduled or unattended
here asks for an invitation, and the provisioning service applies the same rules to
every request: an account with a password is never invited, and one account is sent
at most one email within its cooldown.

What each outcome leaves you to do:

| Code | What to do |
|---|---|
| `sent` | Nothing. The person has 7 days to set a password, then signs in once. |
| `has_password` | Nothing. They sign in with the password they already have. |
| `send_failed` | **Retry step 1**: the retry button on the login step, or `enablement retry … --step keycloak_user`. It asks again under the same rules, so if the account got a password or an email in the meantime, nothing is sent. |
| `cooldown` | Wait. The person was emailed moments ago, most likely by another request, and has that email. There is no retry for it here: a second email after the cooldown is not something anybody decided. |
| `no_email` | Nothing from this console, and a retry cannot help. Giving such accounts an address is a separate piece of work, planned outside this console. |
| `account_disabled` | Nothing from this console. Re-approval does not re-enable an account that is disabled while still in the community's organization. An account a REC *released* (out of every REC organization) is enabled again by its next approval. |
| `not_on_dev_list` | Nothing: this environment emails only its recipient list. |

Every outcome leaves step 1 `succeeded`, because the account exists. `send_failed` is
the **one exception** to "a succeeded step is not re-run": naming step 1 in a retry
re-runs it. "Retry all" does not, and neither does pressing Approve again, so an email
is never re-sent by accident.

**There is no re-send or password-reset button in this console, by design.** A
community manager sends an invitation or a password reset from the `celine-community`
dashboard. That call reaches the provisioning service through this service's member-keyed
routes, `POST /api/admin/communities/{community}/members/{key}/invitation` and
`…/password-reset` ([api-reference.md](api-reference.md)). Those routes accept only a
service acting for a manager, so no one holds that capability alone, and this console, which
shows only what `/api/admin/me` lists, has no button for it. The only send this console can
cause besides Approve is the retry of a failed one.

A send from the dashboard changes no step row here. A submission whose step 1 says
`send_failed` still says so after a manager has sent from the dashboard. On a deployed
realm every member has a submission, and its reference is the member's key
([ADR-0011](decisions/ADR-0011-on-a-deployed-realm-every-member-enters-through-onboarding.md)), but nothing reads that link: the send does not look the
submission up.

Step 3 does one thing more than its name says: the DID it mints is written back
onto the member step 2 created. That is the key anything else uses to attribute a
dataspace consent to a member, and it cannot be written any earlier because the
DID does not exist until step 3 runs. A refusal there fails the step, so the
member is never left holding no DID while the submission reads as approved.

A step that does not apply — a community with no registry binding, a participant
who gave no sharing consent — is recorded as **skipped**, not silently omitted.
"Nothing to do" and "never ran" are different facts.

### When a blocking step fails

The submission stays **in review**. It is not approved, because the person is not
enabled.

What the pipeline *did* achieve is kept. If a login was provisioned before the
registry call failed, that account exists — forgetting it locally would orphan it
remotely and the next attempt would create a second one. The step row records the
error, the attempt count and the external reference, so the remedy is retrying
that step rather than pressing Approve again and re-running all four.

A registry **conflict** on step 2 is a failure unless it is this participant's own
member. The registry refuses a create with `409` for a taken member key, a login
(`user_id`) another member of the community already holds, a DID another member holds,
and a delivery point another member holds. Only the first is an earlier attempt of this
same step, and it counts as registered. The others leave no member for this person, so
the step fails with the registry's reason. Retrying will not clear it. Resolve the clash in
the registry first, then retry the step.

For a community whose areas are primary-substation boundaries, step 2 **resolves the
boundary again** from the submission's supply address, against the template in force
at approval, records the id it finds on the submission, and registers the member into
the area whose boundary it is. When no area of that template has that boundary — the
template dropped it since the wizard ran, or the address now resolves elsewhere — the
step fails with `boundary_not_in_community` in its error and registers nothing; it
never falls back to a municipality, a default area or the id recorded at submission.
When the geocoder or the Digital Twin does not answer, the step fails with
`BoundaryUnavailableError` and registers nothing. Both are retried like any failed
step, and a retry resolves again: it succeeds once the template (and the registry
community's areas) declare that boundary, or once the Digital Twin answers.

`retry` only re-runs steps that are not already `succeeded` or `skipped`, with two
exceptions, both only when the step is named: `keycloak_user` re-runs a login step
whose invitation is `send_failed` (see [The invitation, for the operator](#the-invitation-for-the-operator)),
and `dataspace_share` re-examines a succeeded consent step — *Re-check every
connector* in the console. A member's withdrawal that reached only one of the
connectors holding an offer leaves that step `succeeded`; re-examining it brings
every connector to the member's newest decision, and writes nothing where they
already agree ([data-sharing](data-sharing.md)). The same button is on a consent
step `skipped` because the member declined everything on the form: they may have
decided on their page since.
It never fails the request: the operator asked to repair, and the step table is the
answer.

### Reversal

`Revoca abilitazione` (admins only) undoes enablement: withdraw the member's
sharing grants, revoke the credential and delete the membership, release the
Keycloak login (disabled, out of the community's organization and its groups,
sessions ended), deactivate the registry member. Best-effort and recorded per
step — a revocation that fails half way must leave a record of what is still out
there, because that record is the only way anybody finds the rest. A step whose
revocation failed is tried again by the next press, and the registry member is
not deactivated while the login release has failed: the login is released
through the registry.

The community dashboard's "Release member" (REC admins) runs the same four acts,
keyed on the registry member rather than on a submission, so members imported
into the registry can be released too (`POST …/communities/{c}/members/{k}/release`,
[api-reference](api-reference.md)). Its outcome is written on these rows when a
submission backs the member.

**Every standing grant the community collected for the member is withdrawn**, at
every connector, whether it came from the form or their sharing page — as the
community's decision, not the member's ([data-sharing](data-sharing.md)). While
that step fails, the dataspace identity is left in place, because the withdrawal
needs it: press `Revoca abilitazione` again once the connector is back, and it
withdraws and then revokes the identity.

A correction recorded on a revoked member stays on the submission, and propagates
nothing: every step is `skipped` ("enablement revoked").

## Screens

**`/admin`** — the communities you may administer, with the number of submissions
waiting and a count of approved participants whose enablement is still failing. A
single community redirects straight through.

**`/admin/{rec}`** — the queue. Filter by status or by reference; paginate. The
count comes from `X-Total-Count`, without which a full last page is
indistinguishable from a page that merely happens to be full.

Search matches the **reference only**. Names, emails, fiscal codes and PODs are
encrypted at rest with a non-deterministic IV, so there is no ciphertext to match
against; searching them would mean decrypting every row in the community on every
keystroke. The reference is printed on the participant's confirmation and quoted
in every email.

**`/admin/{rec}/submissions/{id}`** — everything about one submission: identity,
all four consents with version and timestamp, the uploaded documents, the REC's
verification and its history, the corrections of the POD, names and email with
each one's propagation steps, the geocoded municipality, the energy answers, phone
verification, operator notes, the transition buttons, the enablement panel, and this
submission's own history. For a community whose areas are primary-substation
boundaries it also shows **"Primary substation `<id>`, area `<name>`"**: the boundary
the supply address resolved to, and the area of the template in force whose boundary
it is, by the display name the template gives it (its key when it gives none;
REQ-0023) — or "in no area of this community" when the template no longer declares it,
which is a submission whose approval will fail at step 2. The applicant is never shown
either.

**`/admin/{rec}/audit`** — the community's trail. Scoped to this community only:
rows written before the trail recorded a community, and not recoverable by the
0009 backfill, are excluded rather than shown under an arbitrary one.

**`/admin/{rec}/exports`** — CSV of every submission, and the supply-point evidence
for one offer (who stood authorised, who withdrew). Both stream and leave nothing on
disk.

**`/admin/{rec}/shared-pods`** — the PODs that more than one active member holds, as the
REC registry reports them for this community (`GET /api/admin/{rec}/delivery-points/shared`,
from the registry's `…/delivery-points/duplicates`). A POD is one grid connection, so one
of the holders has the wrong one, usually a mistyped code; the registry refuses to give such
a POD again (`delivery_point_held`) until it is resolved. Per POD: this community's
holders by member key, each linked to the application it came from when it came through
this service ("not onboarded here" otherwise), and how many **other** communities hold
it, as a count only ("also held in N other community(ies)"; the registry never names
them). The guidance on the page: check it with the member, then correct the wrong POD by
revision on their application ([Corrections](#correcting-declared-data-revisions)).

- **Who:** `submissions.revise` (`managers`, `admins`): the list exists to be acted on,
  and the act is a revision. The navigation offers the page only to them.
- **Masking:** the PODs are masked as on a submission; *Show the PODs* asks again with
  `?reveal=true`, which needs `submissions.reveal`. Every read is in the trail
  (`entity_type` `shared_delivery_points`, action `view` or `reveal`, with the number of
  points and never a POD).
- **Errors:** no registry configured or no `rec_registry` block → 409; the registry holds
  no community for this REC → 404; another registry refusal → 502 (its text is not
  passed on); the registry unreachable → 503.

**`/admin/{rec}/areas`** — whether the REC registry's areas match this community's
template (the drift check, [REQ-0015](specifications/registry-sync.md)). Each template area
with its primary substation is shown as *matches*, *missing from the registry* or
*differs*, and each registry area the template does not declare as *not in the template* —
which is how an area reintroduced by a bundle import becomes visible. It reads the registry
and writes nothing. It is shown to a platform admin (the realm role `platform-admin`) and to
the REC's own `managers` and `admins` (`recs.drift`), and the navigation offers the page only
to them; the REC's editors and viewers do not see it. A community whose areas are
municipality lists is not synced, and the page says so. Bringing the registry in line is the
registry sync (below), which the console does not offer: it is the platform operator's act.

## Language

The console is translated into Italian (the default), English and Spanish, with the
strings in `ui/src/lib/i18n/{it,en,es}/admin.json`. The choice is made from the
header and remembered per browser, and it is shared with the wizard.

Status, step, state and invitation outcomes are translated by their **code**, not taken from the
API: the step `label` in the enablement payload stays English because the CLI prints
it. A code with no translation is shown raw. Audit action names are never
translated, because they are what `onboarding-cli admin audit --action` filters by.
Error messages returned by the API are shown as the API wrote them.

## Masking

`fiscal_code` and `pod_code` are encrypted at rest and **masked by default**
(`RSSMRA85T10A562S` → `••••••••••••562S`). Length is preserved, so a malformed
code still looks malformed — which is exactly the kind of thing review exists to
catch.

Unmasking needs `submissions.reveal` and is written to the audit trail as its own
action. The POD values of the corrections are masked the same way, and revealed by
`GET …/revisions?reveal=true` under the same capability, audited as `reveal`. The point is not that an operator must never see a fiscal code — sometimes
they must — but that doing so is a deliberate act with their name on it. Reveal is
per record; a list-wide one would be a single audit row covering a hundred
identifiers, which records nothing.

## From the terminal

The same flow, over the same API, so the two cannot answer differently:

```bash
onboarding-cli admin whoami
onboarding-cli admin review list --rec my-rec --status submitted
onboarding-cli admin review take 20260730-a1b2 --rec my-rec
onboarding-cli admin review verify 20260730-a1b2 --rec my-rec --method offline --note "ID checked at the office"
onboarding-cli admin review verify 20260730-a1b2 --rec my-rec --method offline-with-evidence --evidence ./bill.pdf --evidence-kind utility_bill  # hashed here, never sent
onboarding-cli admin review approve 20260730-a1b2 --rec my-rec
onboarding-cli admin review reject 20260730-a1b2 --rec my-rec --reason "POD di un'altra fornitura"
onboarding-cli admin enablement status 20260730-a1b2 --rec my-rec
onboarding-cli admin enablement retry 20260730-a1b2 --rec my-rec --step rec_registry_member
onboarding-cli admin enablement retry 20260730-a1b2 --rec my-rec --step keycloak_user  # resend a send_failed invitation
onboarding-cli admin enablement retry 20260730-a1b2 --rec my-rec --step dataspace_share  # re-drive a split sharing decision
onboarding-cli admin audit --rec my-rec --action transition_failed
```

Every read takes `--json`. `enablement retry` exits non-zero while the state is
still `failed`, so a repair loop can branch on it. Submissions are addressed by
reference; an ambiguous partial is refused with the candidates listed rather than
guessed at.

Authentication is a `client_credentials` token for the client named by
`ONBOARDING_CLI_CLIENT_ID` (`svc-onboarding-cli` by default; `celine-cli` for the platform
operator). `--local` talks to the database directly for a deployment with no Keycloak — see
[authorization.md](authorization.md#break-glass).

### The registry sync

The platform operator pushes a community's template areas to the REC registry, and sets the
community's Keycloak organization up on the way ([ADR-0012](decisions/ADR-0012-areas-are-primary-substation-boundaries-owned-by-the-template.md),
[ADR-0014](decisions/ADR-0014-registry-sync-sets-up-the-community-through-the-provisioning-reconcile.md)):

```bash
onboarding-cli registry-sync --rec my-rec --dry-run                       # the plan, nothing written
onboarding-cli registry-sync --rec my-rec                                 # set up, then write
onboarding-cli registry-sync --rec my-rec --prune                         # also delete undeclared areas
onboarding-cli registry-sync --rec my-rec --token "$ADMIN_TOKEN"          # as a platform-admin person
onboarding-cli registry-sync --rec my-rec --local                         # in process, break-glass
```

It needs `recs.write`. A sync is a platform operator's decision, made as a `platform-admin`
person or through the operator's client `celine-cli`
([ADR-0017](decisions/ADR-0017-the-platform-operators-client-may-start-a-registry-sync.md)).
Without `--token` the command authenticates as the CLI's client, so set
`ONBOARDING_CLI_CLIENT_ID=celine-cli` and its secret in `ONBOARDING_CLI_CLIENT_SECRET`; its
`onboarding.admin` covers the sync's scope, `onboarding.recs.write`. `svc-onboarding-cli`
holds `onboarding.admin` too and is accepted the same way. `--token` takes a platform admin's
own access token instead. The audit records who it was: the person, or the client
(`actor_type=service`, its client id).

It prints the set-up step and every node and area with its outcome, takes `--json`, and exits
1 when the caller was refused, anything was refused or the set-up step failed. A re-run is
safe: it changes nothing that already matches and completes a set-up that failed.

Removing an area is two steps. `--prune` refuses an area members still reference and says
how many: move them on the `celine-community` dashboard first, then prune.

A renamed area (same substation, new key in the template) needs no prune: the sync asks the
registry to move the old area to the new key, and its members move with it, in one request.
The output lists it as `renamed old-key -> new-key` with the number of members moved; a dry
run lists the same and moves nothing. Only when the new key already names another registry
area is the rename not possible: the new key is refused (`boundary_held`) until the old one
is pruned.

## Audit trail

Every admin action records **who**: `actor_type` (`user`, `service`, `cli`,
`system`, or `token` for the pre-authorization era), the Keycloak subject, the
email, and which client they came through.

Reading the trail is not itself audited: it is granted to every tier, and logging
each view would bury the actions worth finding under the act of looking for them.
Downloading a *document* is audited, because a utility bill carries the address,
supply point and consumption history; listing filenames is not. This is the only
way to the documents: a submission's email sends each operator a link to its page
here, never to the files (REQ-0031).

Changing what a scan read (`extracted_data`, `id_extracted_data`) is an operator's
`PATCH`, recorded as `update` with the field names; the applicant cannot change
them (REQ-0034).

An attempted approval that a blocking step refused is recorded as
`transition_failed`. The step rows say what broke; only the trail says who tried.

Reading the shared PODs is recorded as `view` or `reveal` with `entity_type`
`shared_delivery_points` and the number of points.

A correction is recorded as `revise`, with the field, the revision id, the method
and the document, and the revision it supersedes; a retry of its propagation as
`revision_retry`, with the revision and the step. Neither carries a value or the
note.

A manager's send from the `celine-community` dashboard appears in this REC's trail too, as
`member_invitation` or `member_password_reset`:
- `entity_type` is `registry_member`, and `entity_id` is the member key;
- the actor is the manager, and the client id is the community service they came through;
- `detail` carries the provisioning service's code.

The dashboard keeps its own row for the same press. Each service records its own act.
