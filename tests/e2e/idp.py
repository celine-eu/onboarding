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

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - http.server's interface
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(jwks)))
                self.end_headers()
                self.wfile.write(jwks)

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
        realm: tuple[str, ...] = (),
        sub: str = "operator-1",
        email: str = "operator@example.org",
    ) -> str:
        """A human: an organization membership plus a group inside it."""
        claims: dict = {
            "sub": sub,
            "email": email,
            "preferred_username": sub,
            "organization": {organization: {"id": "org-uuid", "groups": [f"/{g}" for g in groups]}},
        }
        if realm:
            claims["groups"] = [f"/{g}" for g in realm]
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
