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
