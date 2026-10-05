# The community's assertion that a member holds their supply points

A holder (a grid operator) releases a member's readings by supply point (POD), on the
community's word: it does not check who holds a POD. These requirements make that word
reconstructable by audit (what was asserted, by whom, on what evidence, under which
responsibility statement) and put the responsibility on the community that gave it. The
connector side (the assertion's checks, one holder per POD, the holder's suspension) is
ds's; this page is onboarding's half.

Names are generic: example-rec collects consent, example-dso holds the data.

---

### REQ-0041 — every evidence file has a digest, and the verification keeps it

- **At upload** a document records `documents.sha256`: the sha256, in hex, of the file as
  uploaded (the plaintext, before encryption).
- **A verification copies the digests it rests on** into its own row (`evidence`:
  `[{kind, sha256}]`, kinds `utility_bill`, `id_document`, `other`). An `uploaded-document`
  verification copies the document's digest, computing it from the stored file when the
  document predates the column. The digest stays on the verification when the document is
  discarded (on activation) or erased.
- **The audit trail records the count, never a digest** (`evidence=1`).

### REQ-0042 — an offline check counts only with a digest of its evidence where grants carry `pod:` keys

Where a community registers members' grants at another participant's connector (its
manifest names a holder connector) and the assertion is on (REQ-0043), a verification must
carry at least one evidence digest:

- **`offline` alone is refused** (422), in the API, the CLI and the console, which does not
  offer it there.
- **`offline-with-evidence`** records an offline check together with the evidence the
  community keeps. The console sends the file to
  `POST /api/admin/{rec}/submissions/{id}/verifications/evidence` (multipart: `files`,
  `kinds`, optional `note`), which hashes it in memory and **stores nothing of it**.
  `onboarding-cli admin review verify --method offline-with-evidence --evidence FILE`
  hashes the file on the operator's machine and sends only the digest. At most five files.
- **`uploaded-document`** names a document stored on the submission; its digest is copied.
- **The applicant's wizard is unchanged.** The rule and its explanation are the operator's
  only: the console explains it in plain words where the verification is recorded.

A community with no holder connector, or a deployment with the assertion off, keeps
`offline` as before.

### REQ-0043 — a grant at a holder carries the community's assertion, and the holder's answers are explained

When a grant carrying `pod:` keys goes to a holder's connector
(`POST /consent/admin/shares`, as the community's collector client), its `legal_basis`
carries `key_assertion` (R4 contract v1):

```
{terms, terms_sha256, method, verification_ref, verified_by, verified_at,
 evidence: [{kind, sha256}, ...]}
```

- `terms` is `REC_ASSERTION_TERMS_ID` (default `rec-pod-assertion/1`) and `terms_sha256` the
  sha256 of the exact bytes of `REC_ASSERTION_TERMS_FILE` (default: the generic draft shipped
  with the service, marked "draft for legal review", which startup warns about).
- `method` is the verification in force (`uploaded-document` or `offline-with-evidence`),
  `verification_ref` its id, `verified_at` its time (UTC).
- `verified_by` is HMAC-SHA256 of the recording operator's subject under
  `REC_ASSERTION_HMAC_KEY`: a pseudonym only this deployment can resolve.
- **Codes and hashes only:** no fiscal code, POD, hash of either, name or email.
- **No assertion can be built** (no verification, or one without a digest): the grant is
  refused here, with a sentence telling the operator to record a verification with
  evidence, and nothing is sent. A grant with no `pod:` key, and the community's own
  connector, carry none.
- **The holder's refusals become operator sentences:** 409 "key already held by another
  subject at this holder" (the POD is registered for someone else at the grid operator),
  409 "key suspended by the holder" (record a new verification, then retry), 422 naming
  `key_assertion` (missing or not accepted; an `extra_forbidden` answer means the connector
  predates the field).
- **A suspension is shown:** the keys a holder lists in `suspended_keys` are reported on the
  `dataspace_share` step (offer, holder and count, never a key). The member's own page never
  receives `keys` or `suspended_keys`.
- **The switch:** `DS_KEY_ASSERTION`. Unset, it is on everywhere but `CELINE_ENV=dev`.
  Outside dev, startup refuses `DS_KEY_ASSERTION=false`, and a missing
  `REC_ASSERTION_HMAC_KEY` (or one equal to an `ENCRYPTION_KEY` or `OTP_HMAC_KEY`), while
  a bound community grants at a holder.

### REQ-0044 — an approved member can be verified anew, only with evidence

After approval a verification is still recorded, as a renewal (`verification_renewed` in the
trail), and only with evidence (`offline-with-evidence`, or a document still stored). It backs
the next grant's assertion: after a holder suspended a POD (released again only on a
verification newer than the suspension), or for a member verified before digests were kept.
A rejected submission takes none (409).

### REQ-0045 — a verification that backs a grant outlives an erasure for the retention period

- A verification is marked when a holder accepts an assertion citing it
  (`last_asserted_at`).
- **An erasure** (`DELETE /api/admin/{rec}/submissions/{id}`, `onboarding-cli admin purge`)
  keeps every marked row: detached from the submission, its note cleared, its evidence
  digests, method, times, operator fields, `rec_slug` and `submission_ref` kept, with
  `retain_until` = the later of the erasure and the last assertion, plus
  `ASSERTION_RETENTION_YEARS` (default 10: the Italian ordinary limitation period, art.
  2946 c.c.; kept under GDPR Art. 17(3)(e); pending legal counsel). Unmarked rows go with the
  submission, as before.
- **Nothing removes a kept row before `retain_until`.** Each erasure deletes the kept rows
  whose period has ended.

### REQ-0046 — a grant is refused when a configured registry cannot be read

When a community has a member registry and it cannot be read, the member's supply points are
unknown: a grant at a holder (new, re-sent or relayed from the member's page) is refused with
a sentence saying so, and nothing is sent. The declared POD on the application never stands
in for the registry. A community with no registry still uses the declared POD.
