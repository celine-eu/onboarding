# ADR-0007 — An offer is recorded, and withdrawn, at every connector that holds its data

**Date:** 2026-09-19
**Status:** accepted — builds on ADR-0006

## Context

ADR-0006 sends each offer's decision to the connector that holds the data it reaches, and
the manifest's `dataspace.connectors` said which one. The validator refused an offer
named by two entries, on the reasoning that two entries are two answers to "where does
this decision go".

That reasoning assumed an offer's data sits in one place. It need not. A research offer
can cover the grid operator's meter readings *and* the community's own meter datasets:
both are bound to it, both data planes enforce it, and a decision recorded at only one
of them leaves the other either refusing what the member agreed to or — after a
withdrawal — still serving what they took back. One thing a member is asked, two
places it has to land.

## Decision

**An offer's decision is recorded at every connector that holds a dataset bound to it,
and withdrawn at every one of them.**

1. **The manifest names every holder.** An offer may appear in several entries of
   `dataspace.connectors`. The community's own connector is named by an entry whose
   `holder` is the community's own alias (`dataspace.organization`); that entry has no
   `url` — its address is `DS_CONNECTOR_URL`, and a second home for it is refused. An
   offer named by no entry stays at the community's connector alone, exactly as before,
   so a manifest written for ADR-0006 means what it meant. One holder has one entry.
2. **Keys go only to another participant's connector**, whatever the manifest lists.
   The community's own connector resolves its members without them.
3. **Provisioning brings every connector to the member's newest decision.** Before
   writing, every connector holding the member's offers is asked what it records for
   them (`GET /consent/admin/subject-shares`, as the community). Per offer, the
   member's newest decision — the form's acceptance, a grant or withdrawal any of
   those connectors records as theirs, or the decision this service recorded when
   the member last pressed on their page — is written, relayed as `decided_by: subject`,
   wherever a connector disagrees, and nothing is written where they agree. So it
   writes the missing half of a partial grant, carries a withdrawal that landed at
   one connector to the others, and never lifts a withdrawal made after the decision
   it relays. A connector that cannot be read fails the run and nothing is written. A
   partial success fails the enablement step; the retry repeats all of this, and a
   retry that names the step re-examines it even when it succeeded, or was
   skipped because the member declined everything on the form. (Amended the same
   day, before this record was first published: it first left a member's withdrawal
   alone wherever it stood, which could not re-drive one — see Consequences.)
4. **Every connector is attempted before anything is reported.** A member's toggle
   decides the community's part as the member and relays every other holder's part; a
   revocation withdraws at every route. One refusal does not stop the others.
5. **Revocation withdraws every grant the community collected** (the maintainer,
   2026-09-19; amended before this record was first published — it first undid only a
   partial grant of the form's offers). Every connector the manifest names is read,
   and each offer with a standing grant the community collected there is withdrawn
   there, whether it came from the form or the member's sharing page — so a member
   who declined the form but granted on the page is withdrawn too, and the step runs
   whenever the member holds a dataspace identity, whatever its row says. "Collected"
   is ds's `collector` on the row: the community's organisation DID, or — at the
   community's own connector only — none, the member having decided there with their
   own credential. A grant another organisation collected is left alone. The
   withdrawal is `decided_by: collector` and says why in `reason`, which ds records
   and returns to no reader. A connector that cannot be read, or a refused
   withdrawal, fails the revocation instead of reading as "nothing to withdraw"; the
   others are still withdrawn at. **The dataspace identity waits for it**: while the
   withdrawal has failed the identity is not revoked, and revoking again retries both
   in order.

## Consequences

- **Provisioning reads before it writes, and again after** — one read per connector
  before, and one more after any write. It is what lets a retry tell "never recorded"
  from "the member withdrew it since", which no local record can know.
- **A member's page merges one offer from several connectors into one row**, and while
  the halves disagree — one granted, the other failed or not yet retried — it shows
  that offer as `pending`, neither granted nor withdrawn (the maintainer, 2026-09-19;
  added before this record was first published). The merged offer carries
  `state: granted | withdrawn | pending`; `granted` keeps its old meaning, a standing
  grant somewhere. A connector that cannot be read fails the page rather than reading
  as pending.
- **The operator's retry re-drives a split in either direction** (the maintainer,
  2026-09-19; added before this record was first published). It reads the member's
  newest decision across the offer's connectors and applies it at every one,
  withdrawals included; it stays operator-triggered and nothing schedules it. A
  withdrawal that landed at one connector leaves the share step `succeeded`, so a
  retry that *names* `dataspace_share` re-runs it even then; where nothing is split
  it writes nothing. Newest is by the time the decision was taken, as the connector
  records it (`revoked_at` for a withdrawal, `decided_at` for a grant), except that a
  relayed grant is dated by its evidence's `accepted_at` — the moment the member
  accepted, not the moment it was relayed. That is what closes the read-then-write
  race: a retry that read a grant just before the member withdrew writes a grant
  dated before the withdrawal, which is then outranked, and the retry reads every
  connector again after writing and applies it. On a tie, the withdrawal wins. A
  **collector's** withdrawal (a membership ended) is not the member's decision and
  never outranks one: a retry runs for a standing membership, and re-approval is the
  community deciding again. Timestamps from different connectors are compared as
  they are, which assumes their clocks agree to well within the time between two
  decisions of one person.
- **The member's page decision is recorded by this service before it is relayed**
  (the maintainer, 2026-09-19; added before this record was first published). A
  connector does not always keep it: a withdrawal the member makes at a connector
  between this service's read and its write *there*, while it failed at the
  others, is overwritten and leaves no row the next read can see; and a withdrawal
  over one that already stands changes no timestamp in ds, so one that failed at
  the only connector still granting would be recorded nowhere. So the toggle
  writes the decision, its time and its evidence to `member_sharing_intents` (one
  row per member and offer) first, relays nothing if that write fails, and keeps it
  whatever the connectors answer; provisioning ranks it with the connectors' rows,
  re-reading it on every pass. This is local bookkeeping of the member's own act —
  the one thing no connector can be relied on to date — and not of what the
  connectors hold, which is still read, never remembered. The form's acceptance is
  not copied into it; ds's `operator` override ranks with it by the time of the
  override itself. The member's page is unchanged: `pending` still means connectors
  that disagree.
- **A revocation whose withdrawal failed keeps the member's dataspace identity** until
  the withdrawal succeeds. The withdrawal is keyed on the DID the identity step clears,
  and ds admits the community's read and write for a subject only while they are a
  member of its organisation — the membership that step deletes. Revoked anyway, as it
  first was, it left a second revocation nothing to withdraw with. The login is still
  closed and the registry member still deactivated: neither is what the withdrawal
  needs. So a member whose withdrawal is failing keeps a credential until an operator
  revokes again, which the failed step row asks for.
- **Revocation leaves the member's recorded decisions alone.** A collector's
  withdrawal is not the member's decision, so it is not written into their record; a
  re-approval ranks that record as it ranks the form, and restores what the member had
  last decided — the rule a collector's withdrawal already follows (decision 3).
- **A grant another organisation collected, or a consumer's ask the member approved,
  is not withdrawn.** ds would accept the first — it withdraws whatever grant stands in
  the cell, and stamps the withdrawal as the writer's — so the restraint is this
  service's. The second is a different instrument, in a cell the admin route does not
  write. Where one offer's datasets at one connector stand for two collectors (ds
  moves `collector` when a grant is re-registered with other keys), the withdrawal is
  per offer and takes all of them.
- **What will tempt someone to undo it**: routing each offer to "the" holder looks
  simpler. It is only correct while every offer's data sits in one place, and the
  failure when it does not is silent — a granted toggle over a half-recorded decision.
