# Data protection

How the applicant's documents reach the people who review them, how personal data is
encrypted at rest, which values the applicant can change, and when an identity document is
collected at all.

---

### REQ-0031 — a submission's emails carry no link to its documents, and each goes to one address

When a submission arrives, email (when `SMTP_HOST` is set and the template does not switch it
off) is sent as separate messages, each with one address in `To:` and none in `Cc:` or `Bcc:`:

- **The applicant's receipt**: the reference, the status and the date. No link of any kind. The
  address is whatever the applicant typed and is not verified.
- **One message per operator** (`notifications.notify`, else `SMTP_NOTIFY`): the same summary
  and a link to the submission's page in the admin console,
  `<base_url>/admin/<rec>/submissions/<id>`. The console requires a sign-in, checks the operator
  may read this community's submissions, and records every document and summary PDF opened
  (`download_document`, `download_pdf`) with who, when and which submission.

No email carries a link that opens the documents: not a token link, and not a storage
backend's link (an S3 link is presigned). There is no route that serves a submission's files
without an operator's sign-in.

### REQ-0032 — encryption keys rotate, and a value no key decrypts is an error

- **`ENCRYPTION_KEY` is a list.** One Fernet key, or several separated by commas. The first
  encrypts; every one decrypts. A malformed entry stops startup, named by its position, never
  by its value.
- **`onboarding-cli rotate-encryption-key`** re-encrypts every encrypted column and every stored
  document with the first key, encrypts a value stored without a key, and leaves alone what is
  already under the first key. It reports a value no configured key opens, leaves it unchanged
  and exits 1, so the old key is not dropped while something still needs it.
- **A Fernet token that no configured key opens raises**, and an error is logged without the
  value. It is never returned as if it were the plaintext. The same holds for a token read with
  no key configured.
- **A value that is not a Fernet token** was written while no key was configured
  (`REQUIRE_ENCRYPTION=false`, development only). It is read as it is, with a warning naming
  the rotation command.

### REQ-0033 — the OTP hashes have their own key

The phone-number and OTP-code hashes are HMAC-SHA256 keyed by `OTP_HMAC_KEY`, never by
`ENCRYPTION_KEY`, so rotating the encryption keys does not change them and one key does not
both encrypt and sign. Outside `CELINE_ENV=dev`, startup refuses phone verification without
`OTP_HMAC_KEY`, and an `OTP_HMAC_KEY` that is one of the `ENCRYPTION_KEY` keys (REQ-0028). In
dev an unset key leaves the hashes unkeyed.

### REQ-0034 — what a scan read is the service's record, not the applicant's

`extracted_data` (bill) and `id_extracted_data` (identity document) hold what scanning read.
The service writes them from the extraction provider's answer to the applicant's
`/extract` and `/extract-id`, on a draft only, keeping a non-empty value and replacing it only
with a longer one, as the wizard shows them.

- **The applicant's `PATCH` cannot set them.** Both keys are dropped from the applicant's
  update, which otherwise proceeds: the wizard sends its copy back with the form. The
  applicant corrects the declared fields instead (`first_name`, `fiscal_code`, `pod_code`,
  `supply_address`, ...), and the operator compares the two.
- **Confirming a stored extraction is not an edit**: a value that differs from what was read
  is refused with `422`, and nothing is confirmed.
- **An operator may change them** through the admin `PATCH`, recorded in the audit trail as
  `update` with the field names and the operator.

### REQ-0051 — the identity document has its own switch, at the deployment and per community, apart from the bill

A participant may upload an identity document and have it scanned only where all three hold:

- **document processing is on** (`EXTRACTION_ENABLED`, `LLM_BASE_URL` and `LLM_VISION_MODEL`);
- **the deployment has not switched it off**: `ID_DOCUMENT_ENABLED`, default `true`. `false`
  turns it off on every community, whatever a template says;
- **the community's template has not switched it off**: `id_document: {enabled: false}`.
  Absent, or `enabled: true`, means offered. A template can only drop it. Import and boot
  refuse a block that is not a mapping, a non-boolean `enabled`, and any other key.

Off for a community:

- `GET /api/{rec}/config` reports `features.id_document_upload` and
  `features.id_document_scan` as `false`. `features.document_upload` and
  `features.document_scan` keep meaning the bill, and do not change.
- `POST /api/{rec}/extract-id`, an upload with `doc_type` `id_card` to
  `/submissions/{id}/documents`, and `/documents/{id}/extract` or `/extractions/{id}/confirm`
  on a stored ID card answer `403` with `detail.code` `id_document_disabled`. With document
  processing off they answer `document_processing_disabled`, as every document route does.
- The bill's upload and scan are unaffected.
- The wizard offers no ID-card upload, no ID scan, and no comparison of the bill with an
  ID card. It reads a missing `id_document_*` feature as off.
