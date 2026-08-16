# The Wizard Flow

1. **Consents** — GDPR + policy + keep-me-updated. Creates the submission (UUID, IP, timestamps).
2. **Bill Upload** — Optional multi-page upload. AI extraction produces editable prefilled data.
3. **Personal Data** — Name, email, phone, CF, POD (validated, prefilled from extraction) + manifest extra fields. Optional ID card upload with cross-validation against bill data.
4. **Energy System** — PV, battery, EV, heat pump questions (manifest-driven, with conditional visibility).
5. **Eligibility** — Address geocoded and checked against coverage rules (if configured).
6. **Statute** — Separate consent for community statute. Also collects **optional data-sharing consent** when the manifest declares `consent.data_sharing`: offers are rendered from `GET {DS_NS_URL}/ns/sharing-offers` (consent-based offers show a toggle; contract-based offers are disclosed without one), and the SHA-256 of the exact consent text shown is recorded. Placed here — not in the consents step, which runs before any data is collected and would be uninformed consent.
7. **Review** — Summary of all entered data. Submit triggers PDF generation + email notification.

Data-sharing consent is **optional** (GDPR Art. 7(4)): it is never required and never blocks `can_submit()`. On the submission it records `data_sharing_consent`, `data_sharing_consent_at`, `data_sharing_consent_offer_ids`, `data_sharing_consent_text_version`, `data_sharing_consent_locale`, `data_sharing_consent_text_sha256`, and `share_provisioned` (whether the consent was pushed to the connector). On approval these offers are provisioned to the dataspace connector — see [data-sharing.md](data-sharing.md) and [dataspace-integration.md](dataspace-integration.md).

