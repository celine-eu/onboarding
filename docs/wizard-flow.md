# The Wizard Flow

1. **Consents** — GDPR + policy + keep-me-updated. Creates the submission (UUID, IP, timestamps).
2. **Bill Upload** — Optional multi-page upload. AI extraction produces editable prefilled data. Dropped from the steps when document upload is off.
3. **Personal Data** — Name, email, phone, CF, POD (validated, prefilled from extraction) + manifest extra fields. Optional ID card upload with cross-validation against bill data. With document upload off, the manual fields only.

Document upload and scanning are what `GET /api/{rec}/config` reports in `features.document_upload` and `features.document_scan`; the wizard reads them and never infers availability. They are off unless the deployment sets both `EXTRACTION_ENABLED` and `EXTRACTION_API_KEY`, because scanning sends identity documents to the extraction endpoint. Off, the feature is absent rather than disabled: no upload control, no placeholder, and no step requires a document.
4. **Energy System** — PV, battery, EV, heat pump questions (manifest-driven, with conditional visibility).
5. **Eligibility** — Address geocoded and checked against coverage rules (if configured).
6. **Statute** — Separate consent for community statute. Also collects **optional data-sharing consent** when the manifest declares `consent.data_sharing`: offers are rendered from `GET {DS_NS_URL}/ns/sharing-offers` (consent-based offers show a toggle; contract-based offers are disclosed without one), and the SHA-256 of the exact consent text shown is recorded. Placed here — not in the consents step, which runs before any data is collected and would be uninformed consent.
7. **Review** — Summary of all entered data. Submit triggers PDF generation + email notification.

Data-sharing consent is **optional** (GDPR Art. 7(4)): it is never required and never blocks `can_submit()`. On the submission it records `data_sharing_consent`, `data_sharing_consent_at`, `data_sharing_consent_offer_ids`, `data_sharing_consent_text_version`, `data_sharing_consent_locale`, `data_sharing_consent_text_sha256`, and `share_provisioned` (whether the consent was pushed to the connector). On approval these offers are provisioned to the dataspace connector — see [data-sharing.md](data-sharing.md) and [dataspace-integration.md](dataspace-integration.md).

