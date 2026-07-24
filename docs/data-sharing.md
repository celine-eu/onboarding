# Data Sharing

How energy-data sharing works for onboarded REC participants. There are two
paths: an **offline export** available today, and a **governed dataspace** path.
The governed path now collects the participant's data-sharing consent in the
onboarding wizard and provisions it to the dataspace connector automatically on
approval; a portal for ongoing self-management remains future work.

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
5. **Log** the disclosure (who, what subset, which purpose, when) for the
   accountability trail.

> The export contains personal data (fiscal code, POD, contact details). Treat
> the file as sensitive: store it encrypted, restrict access, and delete it when
> the purpose is fulfilled.

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
