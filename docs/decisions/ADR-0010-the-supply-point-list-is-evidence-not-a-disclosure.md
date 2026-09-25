# ADR-0010 — The supply-point list is the collector's evidence, not a disclosure

**Date:** 2026-09-25
**Status:** accepted — supersedes ADR-0008 decision 5 and ADR-0009's disclosure-record consequence

## Context

The supply-point export began as a handover. A distributor was given the supply points it
could release, so the export recorded a `DataDisclosed` event with the connector
(`POST /admin/disclosure`) before writing the file. ADR-0008 kept that, and recorded the
disclosure at every connector holding the offer.

Two things have since changed. The maintainer confirmed on 2026-09-25 that **there is no
handover**. The holder already has the list, because the supply points travel with each
decision to its connector (ADR-0007) and its data plane filters on them. So the file is the
collector's own dated evidence of which supply points stood authorised under an offer.
Then ADR-0009 added the members who withdrew, which meant the disclosure record now named
withdrawn members' supply points as disclosed to the offer's recipient.

## Decision

**The export records no disclosure** (the maintainer, 2026-09-25). It does not call
`POST /admin/disclosure` at any connector, and nothing about it is recorded as a
`DataDisclosed`. Recording one would assert a release that never happens.

1. **The file is evidence.** Its header says so. It is this community's own record, as the
   collector of the decisions, of which supply points stood authorised and which members
   had withdrawn under the offer at the generation time, and from which holders and
   datasets. It does not describe itself as a release or a disclosure to anyone.
2. **The withdrawn columns stay as ADR-0009 built them.** So does the `Withdrawals NOT
   reported for <holder>` line.
3. **What only served the disclosure is gone from the export path.** That is the
   `purpose` and `agreement_ref` inputs (API body, CLI `--purpose` and `--agreement-ref`)
   and the disclosure call itself.
4. **Amended the same day, before this record was first published (the maintainer,
   2026-09-25): the unused code goes too.** `dataspace_identity.record_disclosure` is
   removed, along with its tests, its contract-inventory row and the contract check for
   its route. `recipient_ref` is removed as well, from the API body, the CLI
   (`--recipient`), both CLI transports, the console's input, and the check that compared
   it with the offer's controller. The party the consent is read for comes only from the
   offer (`recipients.recipient`, resolved to a DID), and the header names it. A body that
   still sends `recipient_ref` is **ignored, not refused**, because unknown fields are
   ignored. An older caller keeps working and names nobody. Decision 3 first kept
   `recipient_ref` and `record_disclosure`; this amends that.

## Consequences

- **The partial disclosure across holders is gone.** ADR-0008 described a disclosure
  recorded at one holder and refused at the next, with no file written. Nothing is
  recorded now, so that cannot happen.
- **A deployment with no dataspace connector can now export.** The disclosure call used
  to refuse when there was no connector to record at, so the intake-records fallback was
  documented but never reached. It now produces a file, and the header says its consent
  source is intake records and that withdrawals are not reported.
- **The audit row names the offer only** (`pods=<n> offer=<id>`). There is no recipient
  left to record.
- **The file gives nobody access to anything.** A holder's data plane serves from the
  decisions themselves, never from this file.
- **What will tempt someone to undo it**: an export that leaves the community looks like it
  should be recorded as a disclosure. It should be, if a real handover is ever planned. That
  would be a new decision with its own recipient and consent, not this file relabelled.
