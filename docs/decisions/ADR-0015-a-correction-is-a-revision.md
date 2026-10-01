# ADR-0015 — A correction of declared data is a revision, and it is propagated step by step

**Date:** 2026-10-01
**Status:** accepted

Reopens the "frozen after approval" rule for a member's declared data: the POD, first name,
last name and email may now change after submission, but only as described here.

## Context

People do not always know their POD and type it wrong. Names and email addresses are
mistyped too. An operator has to confirm and validate what the person declared anyway, and
without an attachment that check happens by hand.

Before this decision the admin `PATCH` set any field in any status, approved included. It
recorded only the field names in the audit trail, kept no previous value and no evidence,
and propagated nothing. A corrected POD stayed wrong in the REC registry and at the grid
operator's connector, and a corrected email stayed wrong on the Keycloak account and in the
identity registry, so the member could lose their Data sharing page. The wizard's `PATCH`
had no status guard at all.

The fiscal code is not part of this decision. Changing a person's username is not either:
it is the stable identity in the registry and the identity registry.

## Decision

1. **From `submitted` on, the POD, first name, last name and email change only through a
   revision.** The admin `PATCH` answers `409` for these four fields from `submitted` on.
   The wizard's `PATCH` refuses every field once the application is submitted.
2. **The operator validates every revision.** The method is `offline` (checked by hand, the
   usual case) or `uploaded-document` (a document stored on this submission), as for a
   verification. A note saying how the new value was checked is required.
3. **Revisions are append-only.** `submission_revisions` holds the field, the previous and
   the new value (both encrypted), the method, the document, the note (encrypted) and the
   actor. The `Submission` column changes in the same transaction. Per field the newest
   revision is in force, and the first one's previous value is what the person declared.
   Rows are never edited or deleted, like `submission_verifications`.
4. **Recording needs `submissions.revise`, granted where `submissions.review` is.** The
   audit row is `revise` with the field and the revision id, never values.
5. **After approval a revision is propagated, one step per system holding a copy.** Each
   step is a `submission_revision_steps` row with a state (`pending`, `done`, `failed`,
   `skipped`), an attempt count, an error code and a fixed sentence, never a value. It is
   shown on the submission and retried from there, like the enablement steps:
   - first or last name: the Keycloak account (through the provisioning service), then the
     REC registry member's name;
   - email: the Keycloak account (the username is kept), then a fresh invitation for a
     member who never set a password, then the identity registry's mapping (same DID, new
     address);
   - POD: one registry write that replaces the old POD and relinks the member's meters
     (registry ADR-0012, "a delivery point has one active holder across the registry, and
     is corrected in one write"), then every standing grant at a holder re-sent with the new
     keys. The holder records the key change in its own ledger (ds ADR-0022, "A holder reads
     the data keys it serves, and their history").

   A step always sends what the submission holds now, so a retry never puts an older value
   back. A later revision of the same field marks the earlier one's unfinished steps
   `skipped`. Before approval nothing is propagated: approval reads the corrected columns.
6. **Revisions are accepted on `submitted`, `under_review` and `approved` submissions.** A
   `rejected` submission is reopened first. On an approved member whose enablement was
   revoked, the revision is recorded and every step is `skipped` ("enablement revoked").
7. **The revision service takes an actor and an evidence policy, so the same machinery
   serves the member's own corrections later.** The operator's policy covers all four
   fields. The member's policy covers names and email only, with the evidence of their own
   signed-in session. No route uses the member's policy yet.

## Rejected

- **Editing the POD in the community dashboard.** The link to the dataspace and to
  provisioning is open here, not there.
- **Making the existing `PATCH` propagate.** It records no evidence, no before and after,
  and no actor beyond an audit line.
- **Changing the Keycloak username on an email change.** It is the stable identity in the
  registry and the identity registry, and a re-run of the upsert would create a second
  account.
- **Putting the changes in the credential.** The credential carries no personal data by
  design, so a correction re-issues nothing. The trail is the revision rows here and, for
  the POD, the holder's key ledger.
- **Re-minting DIDs derived from an email address.** A DID change re-keys credentials,
  consents, the registry member and provenance. Not worth it for a value that cannot be
  reversed without the registry key.

## Consequences

- Revisions hold previous values. They are encrypted like the columns they come from, and
  they are erased with the submission.
- The registry writes need the narrower per-field grants of registry ADR-0011 ("a member's
  fields are written through per-field routes, each with its own grant"); onboarding still
  holds `rec-registry.members.write`, which covers them.
- A POD the registry refuses as another active member's (`delivery_point_held`) fails its
  step with the sentence "this POD is already recorded for another member — check it with
  the member". The registry's own message may name that member and is not shown.
- The consent texts, the data-sharing agreement and the privacy notices still say "the POD
  code I provided when I joined". Rewording them to "the POD referring to my membership" is
  the partners' decision and waits for their approval.
- A service account reaches `submissions.revise` through `onboarding.submissions.revise`;
  celine-policies declares the scope and grants it to no client.
