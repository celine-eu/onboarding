# Data Sharing

How energy-data sharing works for onboarded REC participants. There are two
paths: an **offline export** available today, and a **governed dataspace** path
that is future work.

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

## Phase B — Governed sharing via the dataspace (future)

The dataspace identity provisioned on approval (DID + DataSubjectCredential +
REC membership — see [dataspace-integration.md](dataspace-integration.md)) is
what enables *governed* sharing: the participant manages consent directly in the
ds connector (`/consent/my/shares`), authorising specific datasets to specific
consumers for specific purposes, enforced by ODRL policies.

This path is not wired up in onboarding yet. What exists today is the identity
and membership foundation it depends on. See Block 3B in the integration plan
for the remaining work (setting initial sharing preferences server-side,
portal redirect for ongoing management, consumer/policy registration in ds).

Until then, Phase A is the operative mechanism.
