# The registry member

How approval registers a participant as a member of the REC registry, as far as a
requirement names it. The rest is described in `docs/dataspace-integration.md` and in
`services/rec_registry.py`.

---

### REQ-0022 — the registry member client authenticates as `svc-onboarding` unless the dataspace is enabled

Approval's registry step (register the member, read back a conflicting holder, deactivate a
member) and the other calls of the registry member client authenticate with a
client-credentials token of:

- **`svc-onboarding`** (`OIDC_CLIENT_ID` / `OIDC_CLIENT_SECRET`) when `DATASPACE_ENABLED` is
  false, which is the default;
- **`svc-ds-onboarding`** (`DS_ONBOARDING_CLIENT_ID` / `DS_ONBOARDING_CLIENT_SECRET`) only when
  `DATASPACE_ENABLED` is true.

Both clients hold `rec-registry.members.write` and `rec-registry.lookup` as default scopes
(`../celine-policies`: `clients.yaml` for the first, `clients.ds-host.yaml` for the second),
which cover every registry call the member client makes: create, look up by `user_id`,
deactivate, write the DID, and look up by DID. The token asks for no optional scope, so it
never carries the registry sync's `rec-registry.community.write`.

The dataspace client is declared only on a realm that hosts the dataspace. A deployment
without it, with the dataspace disabled, needs no `DS_ONBOARDING_CLIENT_SECRET` to approve
anybody (on such a realm, approval used to fail at this step with a 401 from the token
endpoint, after the login had been created).

### REQ-0052 — approval sends the provisioned account's id with the member, as its `subject`

Approval's registry step (step 2) creates the member with `subject` set to the Keycloak user
id of the account step 1 provisioned or found — the account's own id, the `sub` its tokens
carry — beside `user_id`, which stays that account's username. The id is read from step 1's
row, as the dataspace step reads it, so a retry of step 2 alone sends it too. With no account
(no provisioning service, or step 1 skipped) the body carries no `subject` key at all, and the
member is registered without one: a member can exist before its account.

The registry keeps the subject in a column of its own and reads it back in its full export
only (rec-registry REQ-0101); the member mirrors of the intermediate environments create each
mirrored account under it. A member registered before this field, or by a step 2 that answered
`409` for an earlier attempt, is filled in by the registry's `backfill-subjects` (rec-registry
REQ-0102), not by onboarding. The SDK's `MemberCreate` declares no `subject` yet and carries it
as an additional property. A registry that predates REQ-0101 keeps the key in the member's
`extra`, so the registry is released first.
