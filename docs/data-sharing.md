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

Once participants are approved, an operator can export the submission register
to CSV and share the relevant subset with a third party (e.g. the DSO or an
analytics provider) under a Data Processing Agreement.

### Export

```bash
task export-csv                 # all RECs → data/exports/<timestamp>/submissions.csv
task export-csv -- --rec my-rec # single REC
```

The CSV includes (see `outputs/csv_export.py`):

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

### Procedure (export → filter by consent → share under DPA)

1. **Export** the register for the target REC.
2. **Filter by consent.** Only share rows where the relevant consent is `True`
   *and* the recorded `*_consent_version` matches the consent document that
   actually authorises the sharing purpose. A blank `*_consent_at` means the
   consent was never given — exclude the row.
3. **Minimise.** Drop columns the recipient does not need for the agreed
   purpose (data minimisation, GDPR Art. 5(1)(c)). For a DSO grid-analysis
   feed, that is typically `pod_code` + energy attributes, not name/email.
4. **Share under a signed DPA.** The recipient must be bound by a Data
   Processing Agreement (GDPR Art. 28) covering the purpose, retention, and
   sub-processing. Do not transmit the file over unencrypted channels.
5. **Record the disclosure** by naming the recipient on the export — this emits
   a `DataDisclosed` provenance event to ds-provenance (who, what columns, which
   purpose, how many subjects, when), the accountability trail (GDPR Art. 30):

   ```bash
   task export-csv -- --rec my-rec \
     --recipient dso-org \
     --purpose GridMonitoring \
     --agreement-ref dpa-participation-1.0
   ```

   The event carries **codes, DIDs and hashes only, never PII** — `columns` are
   field *names*, not values, and a `consent_snapshot_hash` fingerprints the
   consent state without storing it. Requires `DS_PROVENANCE_URL` and the
   `svc-ds-onboarding` `provenance.write` scope; without a `--recipient` the
   export runs but records nothing.

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
  --recipient dso-org \
  --purpose FlexibilityResearch \
  --agreement-ref dpa-participation-1.0
```

- Rows are members with a **current consent covering that offer**, provisioned to
  the dataspace. Consent is purpose-scoped: agreeing to a different offer is not
  agreeing to this handover.
- The file carries one column. No names, no hashes, no DIDs, no evidence bundle —
  that material lives in the dataspace, where it is verifiable and revocable, and
  a second copy is how two records of the same consent start to disagree.
- A `DataDisclosed` event records the handover.

**The file is a snapshot, so the re-export cadence is the revocation latency.**
Somebody who withdraws stays on the recipient's copy until the next run. The
header states when it was generated and that it goes stale; agree a cadence, tell
members what it is, and hold to it. This is inherent to an offline handover — it
disappears if the distributor ever reads consent directly.

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

Remaining future work: a portal redirect for the participant to manage
preferences over time, and consumer/policy registration in ds. Until that is in
place, Phase A remains available for ad-hoc, DPA-governed disclosures.
