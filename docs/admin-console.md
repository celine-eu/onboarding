# Operator console

Where a REC manager reviews submissions, approves participants, and repairs what
approval did when part of it fails.

Served at `/admin` on the onboarding host, alongside the participant wizard.
Authorization is described in [authorization.md](authorization.md); this document
is about what the console does.

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

Step 1 also **asks for an invitation**: Keycloak emails the participant a link to set
their password, valid for 7 days, in the language they last used in the wizard (or
the community manifest's `locale`, or the realm default). It asks every time,
including on a retry, and the provisioning service decides whether an email goes out,
because only it can see whether the account already has a password. The step row
records what it decided, and **every outcome is a success**: the account exists, and
an invitation that did not go out is not a failed login.

| Code | Console says | Meaning |
|---|---|---|
| `sent` | invitation sent | a new account, or one without a password |
| `has_password` | already has a password | nothing to invite to |
| `not_on_dev_list` | not sent, recipient list | the provisioning service is in dev email mode and the address is not on its list |
| `account_disabled` | not sent, account disabled | a participant approved again after revocation: revocation disables the account, and re-approval does not re-enable it |
| `not_requested` | no invitation requested | not produced by approval, which always asks |

The console shows the code translated; the CLI prints the code beside an English
sentence (`[invitation=account_disabled]`). Rows from before invitations existed
carry no code and show their old detail. There is no re-send or reset button here:
that is a community manager's action in `celine-community`.

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

`retry` only re-runs steps that are not already `succeeded` or `skipped`. It never
fails the request: the operator asked to repair, and the step table is the answer.

### Reversal

`Revoca abilitazione` (admins only) undoes enablement in reverse: revoke the
credential, delete the membership, deactivate the registry member, disable the
Keycloak login. Best-effort and recorded per step — a revocation that fails half
way must leave a record of what is still out there, because that record is the
only way anybody finds the rest.

The standing sharing consent is deliberately **not** withdrawn. Withdrawal is the
data subject's own act, authenticated with their own credential, and it lives in
the participant webapp. Onboarding holds no credential and must not make that
decision on somebody's behalf.

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
all four consents with version and timestamp, the uploaded documents, the geocoded
municipality, the energy answers, phone verification, operator notes, the
transition buttons, the enablement panel, and this submission's own history.

**`/admin/{rec}/audit`** — the community's trail. Scoped to this community only:
rows written before the trail recorded a community, and not recoverable by the
0009 backfill, are excluded rather than shown under an arbitrary one.

**`/admin/{rec}/exports`** — CSV of every submission, and the supply-point list for
a distributor. Both stream and leave nothing on disk.

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
action. The point is not that an operator must never see a fiscal code — sometimes
they must — but that doing so is a deliberate act with their name on it. Reveal is
per record; a list-wide one would be a single audit row covering a hundred
identifiers, which records nothing.

## From the terminal

The same flow, over the same API, so the two cannot answer differently:

```bash
onboarding-cli admin whoami
onboarding-cli admin review list --rec my-rec --status submitted
onboarding-cli admin review take 20260730-a1b2 --rec my-rec
onboarding-cli admin review approve 20260730-a1b2 --rec my-rec
onboarding-cli admin review reject 20260730-a1b2 --rec my-rec --reason "POD di un'altra fornitura"
onboarding-cli admin enablement status 20260730-a1b2 --rec my-rec
onboarding-cli admin enablement retry 20260730-a1b2 --rec my-rec --step rec_registry_member
onboarding-cli admin audit --rec my-rec --action transition_failed
```

Every read takes `--json`. `enablement retry` exits non-zero while the state is
still `failed`, so a repair loop can branch on it. Submissions are addressed by
reference; an ambiguous partial is refused with the candidates listed rather than
guessed at.

Authentication is a `client_credentials` token for `svc-onboarding-cli`. `--local`
talks to the database directly for a deployment with no Keycloak — see
[authorization.md](authorization.md#break-glass).

## Audit trail

Every admin action records **who**: `actor_type` (`user`, `service`, `cli`,
`system`, or `token` for the pre-authorization era), the Keycloak subject, the
email, and which client they came through.

Reading the trail is not itself audited: it is granted to every tier, and logging
each view would bury the actions worth finding under the act of looking for them.
Downloading a *document* is audited, because a utility bill carries the address,
supply point and consumption history; listing filenames is not.

An attempted approval that a blocking step refused is recorded as
`transition_failed`. The step rows say what broke; only the trail says who tried.
