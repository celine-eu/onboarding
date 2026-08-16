# Community Templates

A template defines what one community's onboarding wizard asks, validates and produces.
Templates are imported from `TEMPLATES_DIR` (default `./templates`).


Each community gets a `templates/<slug>/` folder:

```yaml
# manifest.yaml
slug: my-rec
name: "My Energy Community"
branding:
  primary_color: "#2d6a4f"
  logo: assets/logo.svg
fields:
  extra:
    - key: has_pv
      label: "Ho un impianto fotovoltaico"
      "label:en": "I have a photovoltaic system"
      type: boolean          # boolean | text | number | select
      step: energy            # which wizard step this appears in
    - key: pv_kwp
      label: "Potenza impianto (kWp)"
      type: number
      step: energy
      suffix: kWp
      show_if: { key: has_pv, value: true }  # conditional visibility
    - key: has_battery
      label: "Ho un sistema di accumulo"
      type: boolean
      step: energy
      show_if: { key: has_pv, value: true }
    - key: battery_kwh
      label: "Capacita' batteria (kWh)"
      type: number
      step: energy
      suffix: kWh
      show_if: { key: has_battery, value: true }
    - key: has_ev
      label: "Ho un'auto elettrica"
      type: boolean
      step: energy
    - key: has_heat_pump
      label: "Ho una pompa di calore"
      type: boolean
      step: energy
    - key: cassa_rurale_member       # community-specific extra question
      label: "Sono socio della cassa rurale"
      type: boolean
      step: personal
  hidden: []
consent:
  gdpr: { version: "1.0", url: "https://..." }      # external URL
  policy: { version: "1.0", file: consent/policy.pdf } # local file
  statute: { version: "1.0", url: "https://..." }
  data_sharing:                                     # optional; collected in the statute step
    required: false                                 # GDPR Art. 7(4): NEVER required, never blocks submission
    offers: [household-energy-flexibility]          # optional allow-list; omit to offer every consent-based offer the connector publishes
    # No version/file here — the version comes from each offer's consent_text_version served by the connector.
coverage:
  rules:
    - type: municipality
      values: [Town A, Town B]
    - type: postal_code
      values: ["12345", "12346"]
steps: [consents, utility, personal, energy, eligibility, statute, review]
notifications:
  from: "noreply@my-rec.org"
  notify: [admin@my-rec.org]
  base_url: "https://my-rec.example.com"   # base URL for download links in emails
  email: true                               # set false to disable email notifications
  storage:                                  # optional: upload submissions to external storage
    backend: s3                             # s3 | gdrive
    bucket: "${S3_BUCKET}"                  # env var interpolation with ${VAR}
    access_key_id: "${S3_ACCESS_KEY_ID}"
    secret_access_key: "${S3_SECRET_ACCESS_KEY}"
    region: eu-south-1
    prefix: submissions
    url_expiry_seconds: 604800
  webhook:                                  # optional: POST on submission
    url: "https://hooks.example.com/onboarding"
    secret: "${WEBHOOK_SECRET}"             # HMAC-SHA256 signature in X-Signature-256
content:
  welcome: content/welcome.md
  consent_intro: content/consent_intro.md
  success: content/success.md
```

Imported into the `Rec` table with `task import-templates`, then served per community at `/{rec}` — one deployment hosts several.

### REC registry binding (optional, per community)

```yaml
rec_registry:
  community: example-community       # community key in the REC registry
  default_area: north                # where a member goes when nothing matches
  areas:                             # optional, coarse stand-in for geofences
    valley-north: [Springfield, Shelbyville]
    valley-south: [Ogdenville]
```

`areas` maps each registry area key to the municipalities it covers, authored the
same way `coverage.rules` already is. **This is the design, not a stand-in for
geoshapes** — the platform does not use them, so municipality is the unit an area
is defined in.

The member's municipality comes from the **eligibility geocoder**, persisted as
`supply_municipality` when the address is checked, falling back to the bill
extraction's discrete `comune`. A geocoder returns the municipality as its own
field; a bill states a full address as free text and OCR of it is a guess.
Matching is case- and whitespace-insensitive.

`default_area` is **required**: a member with no area cannot be registered at
all, and one whose municipality is not listed still has to go somewhere a REC
manager can find them.

Two authoring rules, both enforced at `task import-templates`: a municipality
claimed by two areas is refused, since a member's area would otherwise depend on
declaration order; and `areas` values must be lists.

The address is deliberately **not** substring-matched — Italian street names
routinely contain other municipalities' names, so "Via Roma 1, Lavarone" would
file the member under Roma.

Omit the block and approved participants are not registered; the wizard still
works. Requires `REC_REGISTRY_URL`, and startup refuses a REC that declares the
block without one.

### Dataspace binding (optional, per community)

```yaml
dataspace:
  organization: example-community          # = KC org alias = IR owner id
  organization_did: did:web:example-community.dataspaces.localhost
  linked_participant_did: did:web:consumer.dataspaces.localhost
  membership_role: member                  # optional
```

`organization` is **one identifier** across the platform: the owner `id` in the
deployment's `owners.yaml`, the Keycloak organization alias, and the owner id in
the identity registry. No mapping table. `task import-templates` validates it,
and it is **required** whenever the block is present — a credential with no
membership is an identity the consent endpoints will not act on.

Omit the block and the community is not in the dataspace: the full wizard runs,
no sharing consent is collected, no identity is provisioned. Supported, not
degraded — onboarding works with no dataspace infrastructure at all.

The organization must already exist and be promoted in the registry. **Onboarding
never creates one**: an organization minted from an approval carries no
verification and no agreement, so it declares no capacity — and capacity is what
decides whether a recipient is disclosed or must be consented to separately. See
[dataspace-integration.md](dataspace-integration.md).

