from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re

import httpx

logger = logging.getLogger(__name__)


def resolve_env(value: str) -> str:
    return re.sub(r"\$\{(\w+)\}", lambda m: os.environ.get(m.group(1), ""), value)


async def fire_webhook(
    url: str,
    payload: dict,
    secret: str | None = None,
    timeout: float = 10.0,
) -> bool:
    body = json.dumps(payload, default=str).encode()
    headers = {"Content-Type": "application/json"}
    if secret:
        sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        headers["X-Signature-256"] = f"sha256={sig}"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, content=body, headers=headers, timeout=timeout)
            resp.raise_for_status()
            return True
    except Exception:
        logger.exception("Webhook POST to %s failed", url)
        return False
