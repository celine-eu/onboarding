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

### The supply-point list

`export-pod-list` is the community's own dated evidence of which supply points
stood authorised under one sharing offer — for the collector that took the
decisions and has to be able to demonstrate them. It is not how a holder learns
the list: the supply points travel with each decision to the holder's connector
([ADR-0007](decisions/ADR-0007-an-offer-is-recorded-at-every-connector-that-holds-its-data.md)),
whose data plane filters on them. The file carries the PODs and nothing else —
minimisation is the shape of the command rather than step 3 of a procedure
someone skips:

```bash
task export-pod-list -- --rec my-rec --offer household-energy-flexibility
```

- **The connectors holding the offer decide who is in the list.** When
  `DS_CONNECTOR_URL` is set, the export asks `GET /consent/admin/shares` who
  currently consents to that offer at **every connector the REC's
  `dataspace.connectors` routes it to** — the same routing a member's decision
  is written with, so an offer whose data sits at another participant's
  connector is read there — and joins the answer to members by their dataspace
  DID. A routed connector answering `422` holds no dataset for the offer and
  contributes nothing. Consent is purpose-scoped: agreeing to a different offer
  is not agreeing to this one, and the connector enforces that server-side by
  keying its answer on the offer.
- **Checked per dataset, exported per offer**
  ([ADR-0008](decisions/ADR-0008-an-offers-audience-is-read-from-every-connector-that-holds-it.md)).
  Each connector answers one subject set per dataset bound to the offer. When
  every dataset, at every holder, has the same set, that set is the offer's
  audience and the file's header names the datasets and holders it was computed
  from. When they differ the export is refused, naming which datasets split and
  by how much — a union would list someone who withdrew from one dataset, and an
  intersection would empty silently the moment a dataset nobody was asked about
  is bound.
- **The party the consent is read for comes from the offer, and only from it.**
  Its `recipients.recipient` is an owner alias; the identity registry resolves
  that to the DID the consent plane is keyed by, and the file's header names it.
  The caller names nobody — the export takes an offer and nothing else, because
  the file goes to nobody (ADR-0010) — so a manifest binding, the community's grid
  operator or a caller's guess cannot stand in for it. (The field was called
  `controller` and meant three things at once; the old spelling is still read, so
  a connector that has not been upgraded keeps working.) A request body that
  still carries `recipient_ref` is answered as if it did not: unknown fields are
  ignored.
- **The registry says which supply points they hold.** The DIDs the connector
  returned go to `POST /admin/lookup/members-by-dids` on the rec-registry, and
  the PODs come from `Member.delivery_points` plus any commissioned meter's
  `properties.pod` — the two are unioned, because an imported member may hold
  either one alone. Only **active** members of **this** community are disclosed:
  `did` is globally unique and the lookup is cross-community, and `pending`,
  `suspended` and `inactive` are all states in which the REC has said this person
  is not participating. Requires the `rec-registry.lookup` scope.
- **Who withdrew is reported as withdrawn**
  ([ADR-0009](decisions/ADR-0009-a-withdrawal-is-reported-in-its-own-column.md)).
  The audience lists standing grants only, so a member who withdrew would
  otherwise look like one nobody asked. The export also reads
  `GET /consent/admin/decisions` at every connector holding the offer — as the
  community, with its own organisation client (`DS_ORG_CLIENT_SECRET`), every page
  — and lists a member whose every decision is withdrawn. Their supply points come
  from the registry like everybody else's.
- The file has four columns and nothing else: `authorised_pod_code`, and
  `withdrawn_pod_code` with `withdrawn_at` and `withdrawn_by` (`subject`,
  `collector`, `operator` or `service`). Each row fills one side. A withdrawn
  supply point is never in the authorised column, and a reader of the old single
  `pod_code` column fails on the missing name rather than reading a withdrawal as
  an authorisation. No names, no hashes, no DIDs, no evidence bundle — that
  material lives in the dataspace, where it is verifiable and revocable, and a
  second copy is how two records of the same consent start to disagree.
- A connector that serves no decisions list (an older ds) does not stop the
  export: the authorised column is still true, and the header names that holder
  under `Withdrawals NOT reported`. Without a connector at all the header says
  withdrawals are not reported.
- The header carries the offer's own terms — controller, purpose, coverage,
  resolution, measures, retention — read from the published vocabulary. They
  describe what was consented to and are uniform across everyone who accepted
  the offer, which is why they are not collected from each person.
- **Evidence, not a disclosure**
  ([ADR-0010](decisions/ADR-0010-the-supply-point-list-is-evidence-not-a-disclosure.md)).
  The file is kept by the community and handed to nobody, so nothing is recorded
  as a `DataDisclosed` and nothing is posted to any connector — a disclosure record
  would assert a release that never happens. A refusal means no file.

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

**The file is a snapshot.** It records who stood authorised, and who had
withdrawn, at the moment it was generated, and the header says when that was. A
decision taken afterwards is in the next export, not this one. The holder does not
depend on it: its data plane reads the decisions themselves.

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
| no connector the REC routes the offer to holds a dataset for it | there is no audience to read; check `dataspace.connectors` |
| the offer's datasets do not agree on who consents — in the audience, or a member granted in one dataset (or at one holder) and withdrawn in another | the offer's statement is no longer true of all its datasets, so no one list is its audience |
| a member the audience authorises is withdrawn in every decision | the two reads disagree, and either column would state one as fact |
| a connector's decisions list refuses or fails (anything but "no such route") | who withdrew is unknown, which is not the same as nobody withdrawing |
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

### Where a decision is recorded

**A consent is enforced at the connector that serves the data.** Most of what a
member decides is about their own community's datasets and is recorded on its
connector. One is not: whether the grid operator may release their meter
readings. Those rows are served by the grid operator's data plane, which reads
the grid operator's consent registry — so a release decision recorded anywhere
else enforces nothing at all.

So the community is the **collector**: the member's relationship is with it, it
collects the decision, and it registers it at the holder. The holder admits that
only because it has recorded this community as an accepted consent collector; it
answers `403` otherwise.

Which offer goes where is the REC manifest's `dataspace.connectors` — a holder, a
connector URL, and the offers it holds. An offer named by no entry stays at
`DS_CONNECTOR_URL`, which is every offer in a community whose data is its own.

**An offer whose data sits in several places is recorded at every one of them**
([ADR-0007](decisions/ADR-0007-an-offer-is-recorded-at-every-connector-that-holds-its-data.md)).
A research offer covering the grid operator's readings and the community's own
meter datasets is named in the grid operator's entry *and* in an entry for the
community itself — `holder` set to the community's own alias, and no `url`,
because that connector is `DS_CONNECTOR_URL`:

```yaml
dataspace:
  organization: example-rec
  connectors:
    - holder: example-dso
      url: http://connector.example-dso.localhost
      offers: [meter-data-release, forecasting-and-research]
    - holder: example-rec
      offers: [forecasting-and-research]
```

Every write reaches every connector holding the offer, and so does every
withdrawal — the member's own toggle and a revocation alike, each connector
attempted whatever the others answer.

**Provisioning brings every connector to the member's newest decision.** It
first reads what each connector holding the member's offers records for them.
Per offer, the member's newest decision — the form's acceptance, a grant or
withdrawal any of those connectors records as theirs, or the decision the member
last took on their sharing page as this service recorded it — is relayed
(`decided_by: subject`) to every connector that disagrees, and nothing is written
where they agree. Then it reads again, and applies anything that changed
meanwhile. A connector that cannot be read fails the run, and **nothing is
written**: without it the newest decision is not known.

- *Newest* is the time the decision was taken as the connector records it —
  `revoked_at` for a withdrawal, `decided_at` for a grant — except that a
  relayed grant is dated by its evidence's `accepted_at`, the moment the member
  accepted rather than the moment it was relayed. That is why a retry that read
  a grant just before the member withdrew cannot undo the withdrawal: the grant
  it writes is dated before it, and the next read withdraws it again. On a tie
  the withdrawal wins. Connector clocks are compared as they are, which assumes
  they agree to well within the time between two decisions of one person.
- **The member's page decision is recorded here first.** The toggle
  (`POST /api/me/data-sharing/{offer}`) writes the decision — granted or
  withdrawn, when, and the evidence of what was served — to
  `member_sharing_intents`, one row per member and offer, **before** it relays
  anything, and keeps it whatever the connectors answer. If it cannot be
  written, nothing is relayed and the member gets a 503. It ranks like any other
  of the member's decisions, dated when they pressed (this service's clock), and
  it is what a connector may not keep: ds stamps nothing when a withdrawal meets
  one that already stands, and a relayed grant can replace the one row a
  withdrawal left. The form's acceptance is not copied there — the submission
  already records it. Beyond the form's offers and those held in several places,
  every offer the member decided on their page is examined.
- An `operator` row — ds's evidenced override, at the member's request — is the
  member's too, and is dated by the row: it is itself the act, so the
  `accepted_at` of the consent it carries does not date it. An override newer
  than the member's recorded withdrawal wins; a press after it wins back.
- A **collector's** withdrawal — the community's, when a membership ended — is
  not the member's decision and never outranks one. It is undone by
  re-approval, which is the community deciding again.
- A re-driven grant carries the evidence of the decision it relays: the form's,
  or the relayed evidence stored on the connector that holds the grant. A grant
  the member made at their own connector as themselves carries ds's evidence,
  with no rendering in it; the holder then gets the evidence the member's page
  relays for the same act.

A partial success fails the `dataspace_share` step. **The retry is the
operator's** (the console, `onboarding-cli admin enablement retry`,
`POST …/enablement/retry`); nothing runs it on a schedule. A withdrawal the
member's toggle got to only one connector leaves the step `succeeded` — nothing
about approval failed — so the retry has to **name the step** to reach it:
`--step dataspace_share`, `{"step": "dataspace_share"}`, or the console's
*Re-check every connector* on a succeeded consent step. A named retry of a
succeeded step writes nothing where every connector already agrees. Until
someone runs it, the member's page shows the offer `pending`, and the member
withdrawing again converges it too. A member who declined everything on the
form has the step `skipped` — nothing to write at approval — and a named retry
(or *Re-check every connector* on the skipped step) re-examines them as well, so
a split they made on their page afterwards is reached.

The recorded page decision closes the two cases the connectors alone cannot
show: a withdrawal the member makes at one connector between the retry's read
and its write *there*, while it failed everywhere else (the retry reads the
record again after writing, and withdraws what it just granted), and a
withdrawal that failed at the only connector still granting while another
already showed one (ds stamps nothing there; the record is dated). The member's
page still shows what the connectors record: an offer is `pending` only while
connectors disagree, not when the recorded decision disagrees with all of them —
a withdrawal every connector refused reads as granted, which is what is still
enforced, and the member was told it failed.

**It is configuration and never inferred from the offer.** An offer's
`recipients.recipient` names who the data goes *to*, which is not who holds it:
the release offer's recipient is the community itself, and the rows are at the
grid operator. Routing by the recipient would send every release decision to the
connector that does not serve them.

**Registered as the community, not as this service.** The connector decides what
a caller may do from the organisation its token names; a plain service client
names none, so one that could register a consent could register it at any
connector for anybody's members. `svc-ds-onboarding` is refused. The call is made
as `svc-ds-connector-<alias>` — the community's own client, alias from
`dataspace.organization`, secret `DS_ORG_CLIENT_SECRET`.

**Whose decision it is, is stated.** `decided_by: subject` relays a decision the
member took — the form, or their own toggle later — and a relayed *withdrawal* is
then theirs, which nothing else can lift. `decided_by: collector` is the
community deciding itself, which is what a withdrawal on revoked membership is:
nobody withdrew, a membership was revoked and the consent went with it.

**Revoking a membership withdraws every grant the community collected for the
member** — the form's and any they made on their sharing page since, including
for a member who declined the form. What stands is read, not remembered: every
connector the manifest names is read for the member, and each offer with a
standing grant the community collected there is withdrawn there, with
`decided_by: collector` and a `reason` ("Membership revoked in <community>"), which
ds records on the row and in provenance and returns to no reader. Nothing else is
written. A refusal the member made stays theirs; a grant another organisation
collected — ds's `collector` names a different DID — is not the community's to
withdraw; and the member's recorded page decisions are left as they are, so a
re-approval restores what they last decided, as it does the form. The reason is
one line of at most 200 characters with no `@`, as ds requires; this service
trims it to that and never sends an address. A connector that cannot be read, or
that refuses, fails the revocation — the others are still withdrawn at — and the
member's dataspace identity is **kept** until the withdrawal has succeeded: the
withdrawal is keyed on it, and ds admits the community only for its own members.
Revoking again is the retry.

**The member's supply points travel with a holder's registration.** `keys:
["pod:…"]`, read from the rec-registry. That data plane keys its rows by supply
point and knows nothing about this community's members, so they are what turn the
consent into rows — and a member with no recorded supply point is refused here
rather than registered, because a consent that can never yield a row is worse
than a visible failure. They are not sent to the community's own connector, which
resolves its members without them, and the connector refuses them on a withdrawal
(a withdrawal drops the keys it had).

### Provisioning on approval

When a submission is approved with `DS_CONNECTOR_URL` set and
`data_sharing_consent` true, provisioning runs as the **last step** of identity
provisioning, after the Keycloak DID sync. For each recorded offer id it POSTs
to `{connector}/consent/admin/shares` — the one that offer is routed to — with
the participant's dataspace DID as `subject_id`, `enabled: true`,
`decided_by: "subject"`, and a `legal_basis` block carrying the consent
provenance (source, REC slug, `consent_text_version`, locale,
`rendered_text_sha256`, `accepted_at`, submission ref). It names an **offer**,
never a dataset. The call is idempotent and sets `share_provisioned=true` on
success.

Unlike the KC-sync step, share provisioning is **deliberately non-fatal**: a
failed share never rolls back the identity or the approval — it just leaves
`share_provisioned=false` for retry via
`POST /api/admin/submissions/{id}/retry-share` (which re-runs with
`raise_on_error=True`, returning 422 on connector rejection). See
[dataspace-integration.md](dataspace-integration.md) for the full sequence.

Onboarding reads the offers vocabulary and records the disclosure with its
`svc-ds-onboarding` service token, and registers the consent with the community's
own client, as above.

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
- **A decision held elsewhere is read back and relayed for them.** A member has
  no standing at the grid operator's connector — their credential is linked to
  their own community's participant, and `/consent/my/*` refuses an organisation
  token in the other direction — so for a routed offer the page reads
  `GET /consent/admin/subject-shares?subject_id=…` there, per subject, and a
  toggle is relayed as `decided_by: subject` with their supply points. A
  withdrawal relayed that way is the member's own and nothing else lifts it. The
  read **fails closed**: a holder that cannot be reached would otherwise render a
  granted decision as ungranted, which invites re-granting and hides a withdrawal
  that has not taken effect. The keys the holder returns are dropped before the
  page sees them.
- **An offer whose connectors disagree is `pending`.** Every offer carries
  `state`: `granted` when every connector holding its data records a standing
  grant, `withdrawn` when none does, and `pending` while some do and some do not
  — a grant one connector refused at approval and nobody has retried yet, or a
  withdrawal that reached only one of them. The member is shown the
  disagreement, never either half as the answer. The connectors compared are the
  offer's routes plus any other connector reporting a standing grant for it.
  `granted` keeps its meaning (a standing grant somewhere), so it is `true` while
  pending. A connector that cannot be read is **not** `pending`: the read fails
  closed, as above, because nobody knows that it disagrees.
- **A prerequisite is presented, not enforced.** ds decides admission: an offer
  with `requires_offers` admits a subject only while each required offer is also
  granted at that connector. The page carries `requires_offers` from the
  published vocabulary and `missing_prerequisites` from the holder's answer, so
  "I consented and nothing happened" has an explanation on screen. Onboarding
  does not enforce it, because two enforcers of one rule is how they come to
  disagree.

`GET /api/me/data-sharing/history` reads the member's own provenance record
(`GET {DS_PROVENANCE_URL}/prov/my/events`) under the same credential. That
setting is **read-only and for this route alone**: this service writes nothing
to provenance, and records no disclosure anywhere (ADR-0010). Unset returns an
empty list —
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
