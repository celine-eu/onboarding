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
    primary: household-energy-flexibility         # optional; the offer the others depend on — shown first, the others inactive until it is accepted, and refused without it
    # No version/file here — the version comes from each offer's consent_text_version served by the connector.
    texts:                                          # optional; this community's own wording, per offer and locale (docs/data-sharing.md)
      household-energy-flexibility:
        version: "1.0"                              # must equal the offer's consent_text_version, or the text is not shown
        it: { title: "...", body: "..." }
        en: { title: "...", body: "..." }
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

A template declares its registry areas in one of two ways, never both in one
template.

#### Areas as primary-substation boundaries

```yaml
rec_registry:
  community: example-community       # community key in the REC registry
  areas:
    north:
      name: North valley             # optional display name; the key when absent
      boundary: {source: gse_cabine_primarie, id: AC000E00001}
    south:
      boundary: {source: gse_cabine_primarie, id: AC000E00002}
steps: [consents, personal, eligibility, review]   # `eligibility` is required, after `consents`
```

Each area is one GSE primary-substation boundary (*cabina primaria*): `source`
is a closed set, today `gse_cabine_primarie` alone, and `id` is the boundary's id
in it (the substation's `cod_ac`, 1 to 64 characters). The area key stays a
hand-managed, readable name matching `^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$`:
letters, digits, `-` and `_`, starting with a letter or digit, at most 128
characters (it goes into the registry's `PUT …/areas/{area_key}` path). An area may
give an optional display `name`, 1 to 128 characters with no surrounding spaces; the
registry sync writes it as the registry area's and its topology node's name, and an
area without one is named by its key ([REQ-0021](specifications/registry-sync.md)). The
key, not the name, is the member's area. Renaming an area's key keeps its members: the
registry sync moves the registry area, members and all
([REQ-0011](specifications/registry-sync.md)). No shape is held here; the shapes are
read through the Digital Twin
([ADR-0012](decisions/ADR-0012-areas-are-primary-substation-boundaries-owned-by-the-template.md),
[specifications/eligibility-and-areas.md](specifications/eligibility-and-areas.md)).

For such a template **the boundary decides both eligibility and the area**
([ADR-0013](decisions/ADR-0013-eligibility-and-area-are-decided-by-boundary-through-the-digital-twin.md)):
the eligibility step geocodes the supply address, asks the Digital Twin's
`boundary_at_point` which boundary covers the point (an edge counts as inside; a
point in two shapes goes to the lowest id), and admits the applicant only if
that boundary is one of the template's areas. That area is the one the member is
registered into. There is no fallback to a municipality, to `coverage.rules` or to
a default area, and a Digital Twin that does not answer fails the check closed
(503). The submission keeps the boundary id and source, never the coordinates;
see [wizard-flow.md](wizard-flow.md).

`task import-templates` refuses, before anything is stored:

- a `source` outside the set, an empty or over-long `id`, an unknown key in an
  area or its `boundary`, an area key the registry could not take;
- **an id the Digital Twin does not know** — one `boundary_shape` answers no
  shape for — naming it; and the whole import when the Digital Twin cannot be
  asked, rather than storing an unvalidated template;
- **two areas on one boundary**, since two names for one substation are
  indistinguishable downstream;
- a `default_area`, or municipality lists mixed with boundaries;
- `coverage.rules` (or `coverage.municipalities`) beside boundaries, and a
  template whose `steps` omit `eligibility`;
- an `eligibility` step before `consents`, or no `consents` step: the address the
  eligibility step checks is saved on the submission, which `consents` creates, and
  the boundary is resolved from that saved address at submit and at approval.

Startup applies the structural checks again to every stored template, and refuses
to start while a template with boundaries exists and `DIGITAL_TWIN_URL` is empty.
The import calls the Digital Twin as this service (`OIDC_CLIENT_ID`, scope
`digital-twin.values.read`), so it needs `DIGITAL_TWIN_URL` and
`OIDC_CLIENT_SECRET` wherever it runs.

The template's areas reach the registry only through an explicit sync by a realm
admin — `onboarding-cli registry-sync --rec <slug> --token <their token>`, or `POST
/api/admin/recs/{rec}/registry-sync` ([specifications/registry-sync.md](specifications/registry-sync.md),
[admin-console.md](admin-console.md#the-registry-sync)). Loading or reloading a template never
does. Each area becomes one registry area `{name: <name>, boundary: {source, id},
topology: [<id>]}`, and one topology node `{id, type: primary_substation, name: <name>}`,
where `<name>` is the area's display `name`, or its key when it gives none. The sync applies the import checks above again before it writes; it is
additive unless `--prune` is given. The console's *Areas* page shows whether the registry
still matches the template.

#### Areas as municipality lists

```yaml
rec_registry:
  community: example-community       # community key in the REC registry
  default_area: valley-north         # where a member goes when nothing matches
  areas:                             # optional; municipality lists per area
    valley-north: [Springfield, Shelbyville]
    valley-south: [Ogdenville]
```

For a template that declares no boundaries. `areas` maps each registry area key
to the municipalities it covers, authored the same way `coverage.rules` already
is. Municipality is a stand-in for the substation an area models: a municipality
can straddle two primary substations. Eligibility comes from `coverage.rules`,
and no Digital Twin call is made.

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
routinely contain other municipalities' names, so "Via Roma 1, Springfield" would
file the member under Roma.

#### Either way

Omit the block and approved participants are not registered; the wizard still
works. Requires `REC_REGISTRY_URL`, and startup refuses a REC that declares the
block without one.

### Dataspace binding (optional, per community)

```yaml
dataspace:
  organization: example-community          # = KC org alias = IR owner id
  organization_did: did:web:example-community.dataspaces.localhost
  linked_participant_did: did:web:consumer.dataspaces.localhost
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

