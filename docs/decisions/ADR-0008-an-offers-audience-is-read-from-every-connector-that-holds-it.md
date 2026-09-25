# ADR-0008 — An offer's audience is read from every connector that holds it, and exported only where they agree

**Date:** 2026-09-25
**Status:** accepted — builds on ADR-0007; decision 5 and the disclosure consequence superseded by ADR-0010

## Context

ADR-0006 records a member's decision at the connector that holds the data, and
ADR-0007 at every connector holding a dataset bound to the offer. The supply-point
export — `POST /api/admin/{rec}/exports/pod-list` — was older than both and had not
followed them. It made two assumptions the rest of the service had stopped making:

- **One connector per community.** It asked `DS_CONNECTOR_URL` who consents, and
  recorded the disclosure there, whatever the offer. An offer whose data sits
  entirely at another participant's connector was answered `422` by the community's
  own, which the export reported as the caller naming the wrong offer. The offer was
  right; the connector asked was wrong. The disclosure had the same fault: it would
  have been recorded at a connector with no dataset to record it against.
- **One dataset per offer.** It refused every offer resolving to more than one
  dataset: *"one file cannot honestly carry two audiences."* The rule was written
  before ADR-0007 and not revisited, and it refused the ordinary case — several
  datasets bound to one offer, every one with the same audience — where one file is
  exactly honest.

The hazard that rule guarded against is real. ds keys a consent row on
`(dataset_id, offer_id)` and expands a member's decision into per-dataset rows at the
moment they decide, so two datasets under one offer can have different audiences: a
dataset bound to the offer after a member decided has no row for them, and a subject
may decide on one dataset alone. The same service also held the opposite position in
the same file — the disclosure is *"scoped to one sharing offer, never to a
dataset"* — and the refusal won only because it ran first.

The question was settled by what a member is actually asked (the maintainer,
2026-09-25): whether a named party may share or use their data, for a purpose, over a
period, under a consent text. Every clause of that sentence is an attribute of the
**offer** — its holders, measures, coverage, legal basis and text. No clause names a
dataset; a dataset is the operator's decomposition of the offer.

What the export is for was settled the same day. It is **the collector's own dated
evidence** of which supply points stood authorised under an offer. It is not how a
holder learns the list: ADR-0007 sends the supply points with each decision, and the
holder's data plane filters on them. There is no out-of-band handover.

## Decision

**The check is per dataset; the file is per offer.**

1. **The audience is read wherever the offer is held.** The export asks every
   connector `consent_routes(binding, DS_CONNECTOR_URL, [offer])` names — the call
   the grant, withdrawal and retry paths already make — and never `DS_CONNECTOR_URL`
   by default. A routed connector that answers `422` holds no dataset for the offer:
   it contributes nothing, and is logged. If no routed connector holds one, the
   export is refused, pointing at `dataspace.connectors` rather than at the offer.
   Any other failure at any connector refuses the export: an audience that could not
   be read is unknown, not empty.
2. **Agreement is checked across every connector and every dataset.** One distinct
   subject set is the offer's audience. More than one means the offer's statement is
   no longer true of all its datasets, and the export is refused, naming which
   datasets split (with their holders, when more than one holds the offer) and by how
   many subjects — never which subjects.
3. **Neither union nor intersection.** A union over-discloses: someone who withdrew
   from one dataset appears in a list a reader takes as authorisation. An
   intersection under-discloses silently: one newly bound dataset nobody has been
   asked about empties the file, and an empty list reads as "nobody consented" rather
   than "nobody was asked".
4. **The file names what it was computed from.** The header lists every dataset and
   its holder. A reader cannot otherwise tell the audience of the whole offer from
   the audience of one dataset.
5. **The disclosure is recorded where the datasets are.** `POST /admin/disclosure`
   goes to each connector the audience was read from, before the file is written. The
   first refusal stops the rest and names the connectors that had already recorded.
6. **No dataset argument.** The export is not narrowed to one dataset to get past a
   split. A split is resolved where it arose — a member's per-dataset decision, or a
   dataset bound after members decided — not by an operator choosing an audience for
   a question the offer was supposed to answer.

## Consequences

- **More offers export, and the refusal that remains means one thing.** Offers held
  at another participant's connector, at several, or over several datasets, export
  when their audiences agree. A refusal now says the offer's datasets disagree — a
  fact about consent that somebody can act on — and not that the export could not
  cope with the shape.
- **An export reads every holder, and records at every holder.** One connector down
  refuses the whole export, where it used to refuse only when that connector was the
  community's. A disclosure that lands at one holder and is refused at the next is
  recorded at the first with no file written; the error names it, and a retry with
  the same event id is idempotent there. A fresh export carries a fresh event id,
  because the id is derived from the generation time.
- **A split is visible only when somebody exports.** Nothing watches for datasets
  whose audiences drift apart; the export is where it surfaces, with a message
  naming the datasets.
- **Withdrawals are not in the file yet.** The evidence this export is for would be
  more complete with who withdrew, and ds cannot yet answer that in list shape. The
  per-dataset, per-connector audience is the unit a withdrawal column would be read
  at too, so adding it is a field beside the subject set, not a new model.
- **What will tempt someone to undo it**: a split looks like something a union — or
  a dataset argument — could route around. Both answer a question nobody was asked:
  the member agreed to an offer, and a list true of only some of its datasets is not
  evidence of that agreement.
