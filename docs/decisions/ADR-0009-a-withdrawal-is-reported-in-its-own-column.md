# ADR-0009 — A withdrawal is reported in its own column, never as an absence and never as an authorisation

**Date:** 2026-09-25
**Status:** accepted — builds on ADR-0008; the disclosure-record consequence superseded by ADR-0010

## Context

The supply-point export is the collector's own dated evidence of who stood authorised
under an offer (ADR-0008). It read one thing from the dataspace: the offer's audience,
`GET /consent/admin/shares`, which lists standing grants only. ds keeps it that way on
purpose, so that a reader of the audience can never take a withdrawn subject for a present
one. The consequence was that a member who withdrew was simply absent from the file, and
absent is also what a member nobody asked looks like. A controller has to be able to show
that a withdrawal was honoured, and a list that is shorter than last month's is a
difference, not a record of that.

ds now lists an organisation's own members' decisions on an offer, granted and withdrawn,
at `GET /consent/admin/decisions` (ds ADR-0021). The route is a holder route, bounded like
the per-subject read-back: the collecting organisation's own client, its current members
only, paged by subject with an opaque cursor that only a `null` ends.

## Decision

**The export reads who withdrew and reports it in columns of its own.**

1. **Read where the audience was read, as the community.** The decisions list is asked of
   every connector the audience came from (ADR-0008), with the community's organisation
   client (`svc-ds-connector-<alias>`) — the service client the audience read uses is
   refused there — and followed to a `null` cursor. A cursor handed back twice is refused
   rather than read as the end.
2. **Withdrawn means withdrawn everywhere the member decided.** A member whose every
   decision on the offer is `withdrawn` is listed as withdrawn. A member who never decided
   is in neither column, as ds reports them. A member `granted` in one dataset, or at one
   holder, and `withdrawn` in another is the ADR-0008 split and refuses the export, naming
   the datasets and counts, never the member.
3. **The two reads must agree.** A member the audience authorises and whose every decision
   is withdrawn is refused: writing them in either column would state one read as fact over
   the other. A member granted everywhere and still absent from the audience — a
   per-recipient opt-out — is no withdrawal and is not listed.
4. **Two columns, not a state flag.** The file is `authorised_pod_code`, `withdrawn_pod_code`,
   `withdrawn_at`, `withdrawn_by`, one side filled per row. With a `state` column, a reader
   who took only the supply-point column would read withdrawals as authorisations — the one
   misreading that is a disclosure. With separate columns that reader gets only one kind,
   and a reader of the old single `pod_code` column fails on the missing name instead of
   succeeding wrongly. `withdrawn_at` is the latest `revoked_at` across the member's cells;
   `withdrawn_by` is ds's code for whose act it was (`subject`, `collector`, `operator`,
   `service`). A supply point another member still authorises is not listed as withdrawn:
   the holder's data plane filters by supply point, so it is served.
5. **A connector with no decisions route is named, not skipped and not fatal.** An older
   connector answers `404`/`405`. The authorised column is still true evidence on its own —
   ADR-0008 stood on the granted audience alone — so the file is written and its header
   names that holder under `Withdrawals NOT reported`. Every other failure of the list,
   `403` (not an accepted collector) and `503` included, refuses the export: the route is
   there and did not answer, so who withdrew is unknown. Without a connector at all, the
   header says withdrawals are not reported.
6. **Supply points of a withdrawn member come from the registry**, in the same lookup as the
   authorised ones: one owner for "which supply points", whichever column they land in.

## Consequences

- **The export now needs the community's organisation secret** (`DS_ORG_CLIENT_SECRET`),
  which until now only consent registration did. Without it the export fails rather than
  reading as "nobody withdrew".
- **Members who have left are not listed.** ds lists current members only, and the registry
  returns active members only. A member whose membership ended — and whose grants the
  community withdrew as collector — drops out of both columns. The header says so.
- **The file changes shape.** Anything that read `pod_code` has to choose a column, which is
  the point.
- **The disclosure record describes the file as written**: all four columns, and every row.
  The file is recorded as a `DataDisclosed` to the offer's recipient before it is written
  (ADR-0008), so a file carrying withdrawn members' supply points is recorded as disclosed
  with them. Whether this collector's evidence should still be recorded, and addressed, as a
  disclosure to the offer's recipient now that there is no handover is not settled here.
- **What will tempt someone to undo it**: one supply-point column with a `state` beside it
  looks tidier. It is tidier for the reader who reads every column, and wrong for the one
  who does not.
