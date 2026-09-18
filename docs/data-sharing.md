# Data Sharing

How energy-data sharing works for onboarded REC participants. There are two
paths: an **offline export** available today, and a **governed dataspace** path.
The governed path collects the participant's data-sharing consent in the
onboarding wizard and provisions it to the dataspace connector on approval.

**Where a participant manages the decision afterwards.** Not here — onboarding
holds no session after approval, and no credential. The dataspace portal's
`/my-data` already serves it: current decisions, the evidence record behind each,
and a plain-language history, authenticated by the person's own credential. A
participant-facing surface in the community webapp is planned; until it ships,
link members there.

## Two legal acts, not one

Worth separating before reading the rest, because they are easy to conflate:

| Act | Basis | Enforced by |
|---|---|---|
| **The mandate** — the community may obtain a member's metering data from the distributor | necessary to the membership contract; **not optional** | the distributor's own process, out of band |
| **The sharing consent** — who may use that data, for which purpose, once it is in the platform | consent; optional, revocable, never a condition of membership | the provider's policy enforcement point, at query time |

Keeping them apart is not pedantry. If the mandate were bundled into the optional
sharing toggle, either the toggle becomes effectively mandatory — which
invalidates it as consent — or withdrawing the sharing consent would look like
revoking the mandate and with it the membership. Wrong in opposite directions.

**The dataspace carries the second and cannot supply the first.** It records,
enforces and proves; it makes lawfulness demonstrable and withdrawal effective.
It cannot make processing lawful that is not. The information notice, the
mandate, the processing agreement with any technical provider, the records of
processing and any impact assessment remain paperwork.

## Phase A — Offline export (available now)

Two exports, and only one of them gives anything to another organisation.

- **The register** is the community's own copy of its applications — for review,
  backup and its own records. It names no recipient.
- **The supply-point list** is the only way a list of members leaves the
  community. It is filtered by consent, minimised to POD codes, addressed to the
  organisation the offer names, and recorded in ds-provenance before the file
  exists.

Both commands are clients of the admin API: the terminal and the console call the
same routes, pass the same authorization and write the same audit row. They
authenticate as `svc-onboarding-cli` (`ONBOARDING_CLI_CLIENT_SECRET`,
`ONBOARDING_API_URL`); `--local` is the break-glass for a deployment with no
Keycloak and needs `ALLOW_LOCAL_ADMIN=true`.

### The register

```bash
task export-csv -- --rec my-rec   # → data/exports/my-rec/submissions-<timestamp>.csv
```

The CSV includes (see `src/celine/onboarding/outputs/csv_export.py`):

- identity: `ref`, `first_name`, `last_name`, `email`, `phone`, `fiscal_code`, `pod_code`
- phone verification: `phone_verified`, `phone_verified_at`
- **consent status with timestamps and versions**: `gdpr_consent[_at][_version]`,
  `policy_consent[_at][_version]`, `statute_consent[_at][_version]`
- **data-sharing consent** (see below): `data_sharing_consent`,
  `data_sharing_consent_at`, `data_sharing_consent_offer_ids`,
  `data_sharing_consent_text_version`, `data_sharing_consent_locale`,
  `data_sharing_consent_text_sha256`, `share_provisioned`
- dataspace identity: `dataspace_did`, `dataspace_subject_id`
- per-REC manifest extra fields (PV, battery, …)

Null cells are empty (not the literal `None`), so the file is safe to load into
a spreadsheet or a downstream pipeline.

**It is not a way to share data.** It carries every application, every field and
no consent filter, and it names no recipient — so there is no basis in this
system for handing it to another organisation, and no filtering of it by hand
makes one. Each export is recorded in the admin audit log (who, from where, how
many rows).

> The export contains personal data (fiscal code, POD, contact details). Treat
> the file as sensitive: store it encrypted, restrict access, and delete it when
> the purpose is fulfilled.

### The supply-point list for a distributor

A distributor asking which supply points it may release does not need the
register. It needs the PODs. `export-pod-list` produces exactly that and nothing
else — minimisation is the shape of the command rather than step 3 of a procedure
someone skips:

```bash
task export-pod-list -- --rec my-rec \
  --offer household-energy-flexibility \
  --recipient grid-operator \
  --purpose FlexibilityResearch \
  --agreement-ref dpa-participation-1.0
```

- **The connector decides who is in the list.** When `DS_CONNECTOR_URL` is set,
  the export asks `GET /consent/admin/shares` who currently consents to that
  offer and joins the answer to members by their dataspace DID. Consent is
  purpose-scoped: agreeing to a different offer is not agreeing to this
  handover, and the connector enforces that server-side by keying its answer on
  the offer.
- **The recipient comes from the offer.** Its `recipients.controller` is an owner
  alias; the identity registry resolves that to the DID the consent plane is
  keyed by. Nothing else names the recipient — the person consented to
  disclosure to the controller *that offer* names, so a manifest binding or the
  community's grid operator must not stand in for it.
- **And the recipient has to be that controller — the organisation, not an
  alias.** `--recipient` (the console's `recipient_ref`) is accepted only when it
  is the offer's controller, named by its identity-registry `id` or its DID. An
  alias is refused even when it resolves to the controller: aliases exist so
  governance files written for other deployments resolve to this one's
  organisations, and a disclosure is addressed to an organisation. Anyone else is
  refused before anything is recorded: a 422 in the console, `Refused: …` and
  exit 1 here. The `DataDisclosed` event names the controller's DID — the same
  recipient the file's header names. To send a list to a different organisation,
  publish an offer that names it as its controller and let people consent to it.
- **The registry says which supply points they hold.** The DIDs the connector
  returned go to `POST /admin/lookup/members-by-dids` on the rec-registry, and
  the PODs come from `Member.delivery_points` plus any commissioned meter's
  `properties.pod` — the two are unioned, because an imported member may hold
  either one alone. Only **active** members of **this** community are disclosed:
  `did` is globally unique and the lookup is cross-community, and `pending`,
  `suspended` and `inactive` are all states in which the REC has said this person
  is not participating. Requires the `rec-registry.lookup` scope.
- The file carries one column. No names, no hashes, no DIDs, no evidence bundle —
  that material lives in the dataspace, where it is verifiable and revocable, and
  a second copy is how two records of the same consent start to disagree.
- The header carries the offer's own terms — controller, purpose, coverage,
  resolution, measures, retention — read from the published vocabulary. They
  describe what was consented to and are uniform across everyone who accepted
  the offer, which is why they are not collected from each person.
- A `DataDisclosed` event records the handover, before the file is written. A
  refusal means no file.

**Why not the intake form.** A submission records what somebody agreed to on one
afternoon, and the export asks two questions of which it answers neither well.

*Who consents* — the participant webapp owns the ongoing decision and writes it
to the connector, and nothing writes back here, so reading the local columns left
a person who granted afterwards **out** of the export and a person who withdrew
afterwards **in**. The second is a disclosure of personal data against a
withdrawn consent, and no re-export cadence fixes it, because the staleness is in
the source rather than in the snapshot.

*What they hold* — `Member.delivery_points` is what the community records now, so
a POD an operator corrected or retired in the registry never reached
`submissions.pod_code`. Reading the registry also answers for a participant this
service never registered: a member the REC manager imported consents through the
same offer and was silently absent from every export.

**The file is a snapshot, so the re-export cadence is the revocation latency.**
Somebody who withdraws stays on the recipient's copy until the next run. The
header states when it was generated and that it goes stale; agree a cadence, tell
members what it is, and hold to it. This is inherent to an offline handover — it
disappears if the distributor ever reads consent directly.

**Where there is nothing to ask, the local record still decides.** A deployment
with no dataspace has no connector holding a consent decision; one with no
`REC_REGISTRY_URL`, or a community whose manifest declares no `rec_registry`
block, has nowhere to ask what a member holds. The two are independent, and the
header names a source for each — so a reader can tell a file whose supply points
were read back from the running system from one that repeats what was declared at
onboarding. Without a connector the header also drops the promise that
re-exporting picks up a later change, because against the local columns it does
not.

**What the export refuses, and why each refusal is safe.** All of these leave no
file:

| Refusal | Reason |
|---|---|
| the REC does not publish that offer | an offer this community does not offer is not one it may export under |
| the offer names no controller | there is nothing to resolve a recipient from, and guessing one is undetectable in the answer |
| the controller is unknown to the registry | register the owner first |
| the controller holds no DID | registered but not onboarded into the dataspace — the consent plane has no key for it |
| the offer resolves to more than one dataset | one file cannot honestly carry two audiences |
| the connector or identity registry is unreachable | who consents is unknown, which is not the same as nobody consenting |
| the rec-registry refuses the supply-point lookup | a denial is not "these people hold nothing"; treating it as one exports fewer supply points than were authorised, and says nothing about it |

## Phase B — Governed sharing via the dataspace

The dataspace identity provisioned on approval (DID + DataSubjectCredential +
REC membership — see [dataspace-integration.md](dataspace-integration.md)) is
what enables *governed* sharing: the participant's sharing preferences live in
the ds connector, authorising specific sharing offers for specific purposes,
enforced by ODRL policies. Onboarding now seeds those preferences from consent
collected during the wizard.

### Wizard consent step

When the manifest declares a `consent.data_sharing` block, the **statute step**
of the wizard also collects optional data-sharing consent. It is placed here —
not in the consents step, which runs before any data is collected and would be
uninformed consent.

- Offers are rendered from `GET {DS_NS_URL}/ns/sharing-offers` (or the
  connector's `/ns` path when `DS_NS_URL` is unset). A **toggle** is shown only
  for consent-based offers; contract-based offers are disclosed without a
  toggle.
- `consent.data_sharing.offers` is an optional allow-list of offer ids. Omit it
  to offer every consent-based offer the connector publishes.
- No version or file is stored in the manifest: the text version comes from each
  offer's `consent_text_version` served by the connector, so it cannot drift
  from what the connector enforces.
- The wizard records the SHA-256 of the exact consent text shown
  (`data_sharing_consent_text_sha256`) alongside the accepted offer ids, text
  version, locale, and timestamp on the submission.

#### The community's own wording — `consent.data_sharing.texts`

The connector serves codes and an English fallback, never prose. A community whose
consent must name its parties in its members' language adds its own text per offer:

```yaml
consent:
  data_sharing:
    texts:
      <offer-id>:
        version: "1.0"          # the offer's consent_text_version this text was written for
        it: { title: "…", body: "…" }
        en: { title: "…", body: "…" }
```

- **Validated at import and at startup.** `import-templates` and the API's boot
  check refuse a `texts` block that is not a mapping of offer ids, an entry without a string `version`, an entry with no
  locale, a key that is neither `version` nor a two-letter locale code, or a
  locale without a non-empty `title` and `body`.
- **Tied to the offer's version.** `get_sharing_offers` attaches the entry to an
  offer as `text` only when `version` equals the offer's `consent_text_version`.
  A text written for another version is **not shown** and is logged as an error:
  wording that describes a different offer is worse than the generic fallback.
  Changing the words therefore means raising the offer's version in the
  connector's offer file and the text's `version` together.
- **Shown wherever the offer is.** The wizard (`GET /api/{rec}/sharing-offers`)
  and the member's view (`GET /api/me/data-sharing`, forwarded by the web app)
  both carry `text`. A frontend shows `title` and `body` for the member's locale,
  then the community's `locale`, then the first one given; with no `text` it
  falls back to the connector's label and definition. The offer's facts
  (measures, resolution, coverage, retention, recipients) are still rendered from
  the codes.
- **Part of the evidence.** The wizard's hashed rendering includes the title and
  body it showed, so `data_sharing_consent_text_sha256` changes with the words.
- The text is not checked against the codes. Keeping the two consistent is the
  deployment's job, beside its offer file.

**Optional by design (GDPR Art. 7(4)).** Data-sharing consent is *never*
required and never blocks submission: REC membership must not be conditioned on
dataspace sharing, so `can_submit()` does not list it and a participant can
complete onboarding with it off.

### Provisioning on approval

When a submission is approved with `DS_CONNECTOR_URL` set and
`data_sharing_consent` true, provisioning runs as the **last step** of identity
provisioning, after the Keycloak DID sync. For each recorded offer id it POSTs
to `{DS_CONNECTOR_URL}/consent/admin/shares` with the participant's dataspace
DID as `subject_id`, `enabled: true`, and a `legal_basis` block carrying the
consent provenance (source, REC slug, `consent_text_version`, locale,
`rendered_text_sha256`, `accepted_at`, submission ref). It names an **offer**,
never a dataset. The call is idempotent and sets `share_provisioned=true` on
success.

Unlike the KC-sync step, share provisioning is **deliberately non-fatal**: a
failed share never rolls back the identity or the approval — it just leaves
`share_provisioned=false` for retry via
`POST /api/admin/submissions/{id}/retry-share` (which re-runs with
`raise_on_error=True`, returning 422 on connector rejection). See
[dataspace-integration.md](dataspace-integration.md) for the full sequence.

Onboarding authenticates to the connector with its `svc-ds-onboarding` service
token (scope `connector.consent.provision`, audience `svc-ds-connector`).

### Changing the decision afterwards

The wizard can only *grant*. GDPR Art. 7(3) requires withdrawal to be as easy as
giving, which for a while nothing provided: onboarding holds no session once
somebody is approved, and the participant webapp had no credential to act with.

`/api/me/data-sharing` is that surface — see
[api-reference.md](api-reference.md) for the routes and the `state` vocabulary.
Three things about it are load-bearing:

- **The member acts as themselves.** The connector authenticates a data subject
  by verifiable credential (`X-Subject-Id` + `X-User-VC`), never by a service
  token. This service resolves *which* credential is theirs and presents it; it
  never returns one, never caches one across requests, and holds no capability
  that would let an operator decide on somebody's behalf. That last part is the
  point: a consent an administrator could give is not a consent.
- **Offers come through the same allow-list the wizard uses.** Resolved with
  `template_service.get_sharing_offers`, so a member is shown exactly what their
  community publishes. `../celine-webapp` previously read
  `/ns/sharing-offers` directly and rendered the whole vocabulary, which meant a
  member could be shown — and could grant — an offer their REC does not publish.
- **A contract-based offer is disclosed, not toggled.** `can_decide` is false for
  it and the write route refuses it by name. Presenting a choice that does not
  exist is what invalidates the consent beside it.

`GET /api/me/data-sharing/history` reads the member's own provenance record
(`GET {DS_PROVENANCE_URL}/prov/my/events`) under the same credential. That
setting is **read-only and for this route alone**: disclosures are written
through the connector's `POST /admin/disclosure`, which computes the
consent-snapshot hash a disclosure record requires. Unset returns an empty list —
the decisions stand without their history.

### The second door — becoming a subject from the wizard

A REC may admit members **offline**: screened on paper, meters installed on
signature. They hold no submission and never will, so the approval path above
cannot reach them — and until they hold a `DataSubjectCredential` there is
nothing for the connector to authenticate, so the page above could only tell them
they had no dataspace identity.

`GET /api/me/data-sharing` provisions one. Where the member's community takes
part and they hold no presentable credential, it issues on the strength of that
preregistration and re-resolves. The credential records `verification_method:
submission-review` and `verified_by` the REC's `organization_did`. The approval path
adds the method the operator recorded (`submission-review:offline`,
`submission-review:uploaded-document`); this door has no recorded verification and
sends the bare value.

Four things constrain it:

- **It is guarded by the resolve that precedes it.** ds is idempotent per role —
  a repeat call for a member who already holds an active credential in the same
  role returns that credential rather than minting a second one — but that is a
  floor, not the guard. The resolve asks the stronger question: whether this
  service can *read the credential back*, which is what the member's own consent
  calls then present. It was the only guard when every call minted and burned a
  revocation slot, and it is still the right one.
- **The DID is bound to the realm that authenticated the member**, taken from
  their token's issuer — not `DATASPACE_KEYCLOAK_REALM`, which names the realm
  the approval funnel's accounts are *provisioned into*. A member with no realm in their issuer is not
  provisioned at all: the connector resolves subjects to data-plane identities
  through that mapping, and a DID bound to nothing is an identity that cannot be
  used and cannot be explained.
- **A community outside the dataspace is never provisioned.** That is
  `no_dataspace`, and it is distinct from `no_identity` precisely so this cannot
  happen.
- **The history route never provisions.** A member with no identity has no
  history, and minting a credential to discover that would also race the read
  that already provisions.

**The DID reaches the registry, or the consent does nothing.** The POD export
above asks the connector *who consented* — in DIDs — and the registry *what they
hold*, joined on `Member.did`. Enablement writes that column for a member the
funnel approved. A member provisioned from the wizard has no submission and no
enablement run, so `GET /api/me/data-sharing` reconciles it directly: it looks the
member up by their Keycloak username (which is what `Member.user_id` holds) and
writes the DID onto their row if it is absent.

It runs on **every** read, not only after provisioning, so a write that failed
once — or a member provisioned before this existed — is healed by them opening the
page. A row that already holds the DID costs one lookup and no write. Three cases
are refused rather than forced, each logged for an operator:

| Case | Why not |
|---|---|
| the row already holds a **different** DID | one person with two dataspace identities; overwriting silently moves which consent record their supply points answer to |
| the row belongs to **another community** | the lookup is global by design; writing across the boundary would attribute a person's supply points to a REC that may not disclose them |
| there is **no member row** | they hold an identity and no membership to disclose anything about — logged as a warning, because it is the reason an export will not carry them |

None of it can fail the member's page: they came to manage their consent, and the
join being wrong is an operator's problem to fix, not a reason to refuse them the
withdrawal Art. 7(3) requires.

A `role` change — `consumer` to `prosumer` when production equipment is
commissioned — is a **reissue** in ds, not a second identity and not a
delete-and-recreate. Nothing here works around that.

Remaining future work: consumer/policy registration in ds. Phase A remains
available for ad-hoc, DPA-governed disclosures.
