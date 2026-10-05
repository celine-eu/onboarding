# Data protection

How the applicant's documents reach the people who review them, how personal data is
encrypted at rest, and which values the applicant can change.

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
