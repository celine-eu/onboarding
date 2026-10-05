# Deployment posture

Which settings are development-only, and what decides whether they are allowed.

The service runs with no configuration on the celine-dev workspace because its defaults name
that workspace. Each of those defaults is also a value no deployment may run with. The rule is
the platform's (`celine.sdk.posture`): **only `CELINE_ENV=dev` relaxes**. The signal is
`CELINE_ENV`, then `ENVIRONMENT`; the first non-empty one wins. Unset, empty, `prod`,
`staging`, `test` or a typo is hardened.

`CELINE_ENV` is read from the process environment, not from `.env`. `task run:api` exports
`CELINE_ENV=dev` unless the shell already sets it, so `CELINE_ENV=staging task run:api` is the
prod-like mode of the same entry point.

---

### REQ-0028 — outside `CELINE_ENV=dev`, startup refuses every development-only setting

Before the database is touched, startup refuses to run when any of these is in force, naming
all of them in one message:

- `DATABASE_URL` carrying a development database password;
- `ALLOW_PERMISSIVE_POLICY=true`;
- `REQUIRE_ENCRYPTION=false`;
- `ALLOW_LOCAL_ADMIN=true` in the service's environment (the `onboarding-cli --local`
  break-glass sets it on its own invocation);
- `SMS_PROVIDER` naming a development provider (`log`, `console`, `dev`), which turns phone
  verification on while sending nothing;
- `OIDC_BASE_URL` still the workspace's issuer;
- `SMTP_HOST` still the workspace's Mailpit, or `SMTP_TLS=false` with a relay configured.
  `SMTP_HOST=` (empty) switches email off and is not refused;
- `OIDC_CLIENT_SECRET` or `DS_ONBOARDING_CLIENT_SECRET` equal to its client id, the dev realm's
  convention;
- phone verification on without `OTP_HMAC_KEY`, or with an `OTP_HMAC_KEY` that is one of the
  `ENCRYPTION_KEY` keys (REQ-0033).

### REQ-0029 — in `CELINE_ENV=dev` the same settings are one warning, and the service starts

The list above is logged once, as a warning, and startup proceeds exactly as it does without
the check.
