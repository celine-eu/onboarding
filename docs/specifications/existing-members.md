# Existing members

How someone who is already a member of the community onboards without typing what the
community already holds. The applicant declares it; the operator completes the POD and the
supply address from the community's own member register; approval waits for both.

The declaration is the applicant's word and nothing checks it. It therefore never makes data
unneeded: it moves who supplies the POD and the supply address, from the applicant to the
operator. Somebody who is not in the register is stuck at review and rejected. Every member
still enters through this service
([ADR-0011](../decisions/ADR-0011-on-a-deployed-realm-every-member-enters-through-onboarding.md)).

---

### REQ-0024 — a template may let an applicant declare they are already a member

A template offers the declaration with an `existing_members` block:

```yaml
existing_members:
  enabled: true
  skip_steps: [energy]
```

- **Off unless declared.** A template without the block, or with `enabled: false`, does not
  offer it. `GET /api/{rec}/config` answers `existing_members: {enabled, skip_steps}` either
  way, so the wizard never has to infer it.
- **`skip_steps` names only `utility`, `phone_verify`, `energy` and `eligibility`.** These are
  the steps that collect nothing approval needs from a declared applicant. The coverage step
  is among them because the operator completes the supply address from the register anyway.
  Any other step, an unknown key, or an `enabled` that is not a
  boolean is refused at template import and at startup.
- **The declaration is made where the submission is created.** `POST /api/{rec}/submissions`
  takes `declared_existing_member`. The wizard's `PATCH` may change it while the submission is
  a draft. The submission records it as `declared_existing_member`.
- **A declaration the template does not offer is refused with `422`**, on create and on
  `PATCH`. It is not recorded and then ignored.
- **The wizard does not show the skipped steps to a declared applicant.** A required extra
  field that belongs to a skipped step is not required at submit.
- **A skipped `phone_verify` is not required at approval.** It is not waived either: the
  member was never asked, so approval neither waits for a verified phone nor reports one as
  waived, whether or not this deployment can send SMS.

### REQ-0025 — a declared member's POD and supply address are completed by the operator before approval

- **At submit** (`can_submit`, `supply_boundary.resolve_for_submit`):
  - a declared applicant may submit without a POD;
  - **the coverage step is kept or skipped, never reduced.** Where the template keeps
    `eligibility`, a declared applicant takes it and its submit check like everyone else
    (REQ-0007);
  - where the template skips it, the supply address is **deferred** to the operator:
    - the applicant may submit without one;
    - an address the submission holds anyway (a scanned bill's) is resolved, and its
      boundary is recorded as a hint for the review;
    - an address that is not found, outside every area, or not checkable now (the Digital
      Twin not answering) refuses nothing.
- **At approval**, a declared member is refused, with `422` and nothing run, until:
  - the submission holds a POD, whoever gave it;
  - where the supply address was deferred, an **operator has revised it** (REQ-0026). Nothing
    the applicant gave was checked against the areas, so only the register's address decides
    the area.
- **The operator console reads `existing_member_pending`** on the admin read of a submission:
  the fields still to complete, `pod_code` and (where deferred) `supply_address`, in that
  order. It is empty
  when nothing is missing or the applicant declared nothing.
- **The queue filters on the declaration.** `GET /api/admin/{rec}/submissions` takes
  `declared_existing_member=true|false`, and the total follows the filter.
- **An applicant who declared nothing is unaffected.** Every check of REQ-0007 applies to
  them as before.

### REQ-0026 — an operator corrects the fiscal code and the supply address by revision

`fiscal_code` and `supply_address` are revisable fields, beside the POD, names and email
(`POST /api/admin/{rec}/submissions/{id}/revisions`), with the same evidence, note and
capability.

- **The fiscal code** is format-checked and upper-cased.
  - It is revisable from `submitted` through `approved`. It never leaves this service (PDF,
    CSV export, console), so a revision after approval has no propagation step.
  - It is masked in the revision history, like the POD, unless `reveal` is asked for.
- **The supply address** is its text, as the eligibility step saves it (`{"text": …}`).
  - It is revisable **before approval only**. After approval the answer is `409`: the member
    is already registered in the area it resolved to.
  - The recorded boundary is resolved again from the new address, as on a wizard save.
- **The admin and wizard `PATCH` refuse both fields from `submitted` on**, as they refuse the
  other revisable fields.

### REQ-0027 — the console shows what is left to complete, and prefills a rejection email

Verified end to end by `ui/tests/existing-member.spec.ts`, which runs through `scripts/e2e.sh ui`.

- **The queue.** A declared member's submission carries a badge, and the queue filters on the
  declaration (REQ-0025).
- **The review.** It lists what is pending, and offers a revision of an empty POD or supply
  address.
- **Rejecting a declared member** offers a `mailto:` to the applicant:
  - it is in the submission's locale;
  - it says the community could not find them among its members, and invites them to apply as
    a new member;
  - the operator's own mail client sends it. This service sends nothing.
