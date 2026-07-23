# Regulatory Field Coverage (Italian CER)

> Engineering gap analysis, not legal advice. Maps the data the onboarding
> platform collects to the data a Renewable Energy Community (CER / *comunità
> energetica rinnovabile*) needs for GSE registration under the Italian
> framework. Confirm the definitive requirements and citations with a
> regulatory/legal reviewer and the current GSE technical rules before relying
> on this for a production community.

## Regulatory context

| Instrument | Role |
|---|---|
| **D.Lgs. 199/2021** | Transposes RED II; defines CER and diffuse self-consumption in Italian law. |
| **D.M. MASE "CACER" (D.M. 7/12/2023 n. 414)** | Incentive framework for diffuse self-consumption configurations. |
| **ARERA 727/2022/R/eel (TIAD)** | Regulates the economic valuation of shared energy; defines the reference perimeter. |
| **GSE — Regole Tecniche per l'autoconsumo diffuso** | Operational rules for accessing the service; the concrete data set GSE ingests. |

**Perimeter rule (important):** under the current CACER framework the members
and plants of a CER must sit under the **same primary substation** (*cabina
primaria*) — GSE publishes the zone map. This is a change from the earlier
art. 42-bis regime, which used the low-voltage secondary substation. See
[Gap G1](#g1-perimeter-model).

## What onboarding collects today

| Field | Where | Validated | Required for submit |
|---|---|---|---|
| First / last name | `submissions.first_name/last_name` | length only | ✅ `can_submit` |
| Fiscal code (CF) | `submissions.fiscal_code` | ✅ checksum (`validators/fiscal_code.py`) | ✅ |
| POD code | `submissions.pod_code` | ✅ format (`validators/pod_code.py`) | ✅ |
| Email / phone | `submissions.email/phone` | format; phone optionally SMS-verified | ✅ (one of) |
| Supply address | `extracted_data.indirizzo` (bill OCR) | ❌ unstructured, optional | ❌ |
| Energy assets (PV, kWp, battery, EV, heat pump) | `extra_data` (manifest fields) | type only | only if manifest marks `required` |
| Property type | `extra_data.property_type` | enum | ❌ |
| GDPR / policy / statute consent | `submissions.*_consent` + timestamp + version | ✅ | ✅ |
| Dataspace DID / VC / membership | `submissions.dataspace_*` | provisioned on approval | n/a |

## Coverage assessment

### ✅ Adequately covered

- **Member identification (natural person).** CF with checksum uniquely
  identifies an Italian natural person; combined with POD format validation,
  optional bill extraction, ID-card cross-validation, and (Block 2) SMS
  verification, this reaches *reasonable assurance* — consistent with utility
  onboarding practice. See [phone-verification.md](phone-verification.md).
- **Point of delivery.** POD format is validated (`IT` + 3 digits + `E` + 8).
- **Consent trail.** Versioned, timestamped, IP-stamped, audit-logged — strong
  for GDPR Art. 7 and traceability.

### ⚠️ Gaps against GSE registration

#### G1. Perimeter model
The eligibility checker matches on **municipality / postal code / geographic
rules** (`services/eligibility.py`), which is a *proxy* for the CER perimeter.
The regulatory perimeter is the **primary-substation (cabina primaria) zone**.
A member can be in the right municipality but the wrong CP zone, or vice-versa.
**Recommendation:** support a CP-zone coverage rule type (GSE publishes the CP
map / an API), or clearly document municipality matching as a pre-screen that
GSE's own CP check supersedes.

#### G2. Supply address is not a first-class field
The POD's supply address is only captured as free text inside
`extracted_data.indirizzo`, populated **only if** the applicant uploads a bill
(an optional step). It is not structured, not validated, and **not required by
`can_submit`**. The default `example` template's `steps` do not even include
the `eligibility` step. GSE registration ties each POD to its supply address
within the CP perimeter, so the platform should persist a structured,
required address for the POD.
**Recommendation:** add structured address fields (street, house number,
municipality, postal code, province) as required submission fields; persist the
geocoded eligibility result (lat/lng, matched CP/municipality, outcome) rather
than discarding it.

#### G3. Member role not explicit
GSE distinguishes **consumer / producer / prosumer**. Today this is only
inferable from `has_pv`. **Recommendation:** add an explicit role field; for
producers, require plant data (see G4).

#### G4. Producer plant data incomplete
For members with a production plant, GSE needs the **production POD, plant
power, technology, commissioning/entry date, and location/coordinates**. The
manifest captures `has_pv` / `pv_kwp` / `has_battery` but not the production
POD, commissioning date, or plant location.
**Recommendation:** when role = producer/prosumer, gate on a plant sub-form.

#### G5. Legal-entity members not modeled
Only natural-person fields exist (name + CF). A CER admits legal entities
(*partita IVA*, *ragione sociale*, legal representative) and condominiums.
**Recommendation:** support an entity member type with VAT number and legal
representative, if the community admits non-natural-person members.

#### G6. Payout data out of scope (confirm ownership)
IBAN / payout data for incentive disbursement is **not** collected. This may be
intentionally handled by the CER *referente* / GSE portal downstream.
**Recommendation:** confirm the boundary; document that onboarding stops before
payout setup.

## Summary

Onboarding is solid for **member identity and consent**. The material gaps for
GSE registration are **perimeter accuracy (G1)** and **structured, required
supply-address capture (G2)** — both P0 if the platform is to feed GSE
registration directly rather than act as a pre-screen. G3–G5 depend on whether
the community admits producers and legal entities. G6 is likely out of scope.

None of these block the dataspace identity / consent flow this repository
primarily implements; they are about completeness of the CER enrolment record.
