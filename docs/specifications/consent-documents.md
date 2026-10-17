# The terms, their clauses, and a member's own acceptances

What an applicant is asked to accept about the terms of use, and what a member can read back
of what they accepted. How every consent slot resolves is in
[templates.md](../templates.md#consent-documents-and-the-legal-host).

---

### REQ-0047 — the specific approval of the terms' clauses is its own act, asked only with the terms and in the text's own words

`consent.terms_clauses` is the specific approval of the terms' onerous clauses (Art. 1341(2)
c.c.). It has no document of its own: it is given on the terms' text.

- **Asked only with the terms, and only with the words for it.** It is asked when `terms` is
  asked and the legal host's text, in the language shown, carries
  `declarations.specific_approval`. Otherwise — the terms from a manifest `url` or `file`,
  the host not yet answered, no host, or a text without those words — it is not asked,
  whatever the manifest declares.
- **The words are the text's.** The configuration carries the text's `declarations` as the
  legal host publishes them; the wizard labels the terms' box with `general` and the clauses'
  box with `specific_approval.text`, unchanged. Onboarding composes no legal wording for
  either.
- **A separate act.** Its own box, never ticked in advance and never ticked by the terms' box;
  its own `terms_clauses_consent` and `terms_clauses_consent_at`, with the terms' version, url
  and sha256. The terms accepted do not stand for it.
- **Required when asked.** A submission, and its submit, without it is refused, as without the
  terms. A version the service no longer shows is refused with 409 (a stale page), and a box
  sent where it is not asked is not recorded.

### REQ-0048 — a template declaring the clauses without the terms is refused

A manifest whose `consent:` declares `terms_clauses` without `terms`, or declares either as
something other than a mapping, is refused by `import-templates` and at startup: there is no
text to approve the clauses on, so it is a mistake rather than a community without terms.

### REQ-0049 — a member reads back their own application's acceptances, and nobody else's

`GET /api/me/acceptances` returns, for the member calling it, the documents accepted on their
application: per slot its `slot`, `legal_slot`, `version`, `url`, `sha256` and
`accepted_at`, with the `submission_id` and `rec_slug`. Nothing else of the application.

- **Their application** is the one whose `keycloak_user` enablement step **succeeded** with
  the token's `sub` as its Keycloak uuid, is **approved**, and is in the community the
  token's organization resolves to; the latest, if several. The email and the username are
  never consulted. A revoked login (whose step no longer holds the uuid), another step's
  reference, an application in another community, or one not approved does not answer.
- **No application is an answer**: `submission_id: null` and an empty list, for a token
  naming no community served here as for a member who never applied.
- A slot not accepted, or accepted without a known version, is left out: there is nothing to
  match it against. `terms_clauses` is its own entry, never implied by `terms`.

### REQ-0050 — the member's acceptances are read with the member's own verified token only

`/api/me/acceptances` is authorised by the member's token and nothing else, and consults no
operator capability.

- The token is verified as a console request's is — signature, issuer, `svc-onboarding`
  audience, expiry — and read from the `Authorization` header only. Without a valid one:
  401.
- A service account is refused with 403, whatever scope it holds: a client acting for a
  member forwards the member's token.
- The route reads no parameter: neither a query string nor a path names the application, the
  member or the community.
- A token whose organizations resolve to more than one community served here is refused
  with 409, and nothing is read.
