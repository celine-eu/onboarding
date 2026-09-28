# The client's address

Which address this service records for a request, and where it comes from.

---

### REQ-0020 — the consent and audit IP is the connection's peer as uvicorn resolved it, never a request header

The IP recorded as a consent's evidence (`consent_ip`, when a submission is created) and in
every audit row is the address of the connection's peer, as uvicorn hands it to the
application. The rate limits key on the same address.

- **No request header is read for it.** `X-Forwarded-For` and `X-Real-IP` are written by
  whoever sends the request; reading them let any caller choose the address its consent or
  its audit row carries.
- **Behind an ingress, uvicorn supplies the real client**: it replaces the peer with the
  forwarded address only when the connection comes from an address listed in
  `FORWARDED_ALLOW_IPS`, and ignores the header from anywhere else. Setting
  `FORWARDED_ALLOW_IPS` to the ingress's range is therefore a deployment requirement (README,
  *HTTP hardening*): without it every consent and audit row carries the ingress's address, and
  every visitor shares one rate limit.
- A request with no peer address is recorded as `unknown`, never as a header's value.
