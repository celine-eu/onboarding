"""A one-key JWKS server, so end-to-end tests can mint tokens the app verifies.

The alternative — stubbing `JwtUser.from_token` — would leave the single most
security-relevant step untested. Here the running service fetches a real JWKS over
HTTP and checks signature, issuer, audience and expiry exactly as it does against
Keycloak; only the key's origin differs.

Runnable standalone (`python tests/e2e/idp.py 8099`) so the same harness backs the
Playwright suite, which needs an operator token from outside pytest.
"""

from __future__ import annotations

import base64
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

AUDIENCE = "svc-onboarding"


def _b64(value: int) -> str:
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


class TestIdp:
    """An issuer the service can be pointed at, plus the tokens to go with it."""

    def __init__(self, port: int = 0) -> None:
        self._key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        numbers = self._key.public_key().public_numbers()
        jwks = json.dumps(
            {
                "keys": [
                    {
                        "kty": "RSA",
                        "use": "sig",
                        "alg": "RS256",
                        "kid": "test",
                        "n": _b64(numbers.n),
                        "e": _b64(numbers.e),
                    }
                ]
            }
        ).encode()

        idp = self

        class Handler(BaseHTTPRequestHandler):
            def _json(self, body: bytes) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802 - http.server's interface
                # Discovery, so the app can fetch a *service* token from here as
                # it does from Keycloak when it calls a dataspace service as
                # itself. Every other path is the JWKS, as it always was.
                if self.path == "/.well-known/openid-configuration":
                    self._json(
                        json.dumps(
                            {
                                "issuer": idp.issuer,
                                "jwks_uri": idp.jwks_uri,
                                "token_endpoint": f"{idp.issuer}/token",
                            }
                        ).encode()
                    )
                    return
                self._json(jwks)

            def do_POST(self):  # noqa: N802 - http.server's interface
                # A client-credentials grant, answered for any client and minted
                # *as* that client: what the token is for is decided by the stub
                # it is presented to, which can then tell which client asked.
                form = parse_qs(
                    self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
                )
                client_id = (form.get("client_id") or ["svc-e2e"])[0]
                self._json(
                    json.dumps(
                        {
                            "access_token": idp.service("e2e", client_id=client_id),
                            "token_type": "Bearer",
                            "expires_in": 3600,
                        }
                    ).encode()
                )

            def log_message(self, *args):
                pass

        self._server = HTTPServer(("127.0.0.1", port), Handler)
        self.port = self._server.server_address[1]
        self.issuer = f"http://127.0.0.1:{self.port}"
        self.jwks_uri = f"{self.issuer}/certs"

    def start(self) -> TestIdp:
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def mint(self, **claims) -> str:
        payload = {
            "iss": self.issuer,
            "aud": AUDIENCE,
            "iat": int(time.time()),
            "exp": int(time.time()) + 3600,
            **claims,
        }
        return jwt.encode(payload, self._key, algorithm="RS256", headers={"kid": "test"})

    def operator(
        self,
        organization: str,
        *groups: str,
        roles: tuple[str, ...] = (),
        realm_groups: tuple[str, ...] = (),
        sub: str = "operator-1",
        email: str = "operator@example.org",
        org_type: str | None = "rec",
    ) -> str:
        """A human: an organization membership plus a group inside it.

        Typed `rec` by default, flattened the way a real token carries it: the
        policy grants an organization-scoped operator nothing on an organization
        of any other type. `roles` go to `realm_access.roles` (`platform-admin` is
        the platform-wide grant); `realm_groups` writes a legacy top-level
        `groups` claim, which grants nothing.
        """
        org_claim: dict = {"id": "org-uuid", "groups": [f"/{g}" for g in groups]}
        if org_type is not None:
            org_claim["type"] = [org_type]
        claims: dict = {
            "sub": sub,
            "email": email,
            "preferred_username": sub,
            "organization": {organization: org_claim},
        }
        if roles:
            claims["realm_access"] = {"roles": list(roles)}
        if realm_groups:
            claims["groups"] = [f"/{g}" for g in realm_groups]
        return self.mint(**claims)

    def service(self, *scopes: str, client_id: str = "svc-onboarding-cli") -> str:
        """A client_credentials token: scopes, no organization, no groups."""
        return self.mint(
            sub=f"service-account-{client_id}",
            preferred_username=f"service-account-{client_id}",
            client_id=client_id,
            azp=client_id,
            scope=" ".join(scopes),
        )


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8099
    idp = TestIdp(port).start()
    organization = sys.argv[2] if len(sys.argv) > 2 else "community-a"
    group = sys.argv[3] if len(sys.argv) > 3 else "admins"
    # Both tokens up front: only this process holds the signing key, so a caller
    # cannot mint the second one later.
    print(
        json.dumps(
            {
                "issuer": idp.issuer,
                "operator": idp.operator(organization, group),
                # An operator of an organization that owns no community here —
                # authenticated, authorised for nothing. The denied page.
                "denied": idp.operator("community-nowhere", "admins", sub="nobody"),
            }
        ),
        flush=True,
    )
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        idp.stop()
