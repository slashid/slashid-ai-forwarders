"""The user behind a hook call, from the MCP connection's bearer token.

This service is an OAuth resource server and nothing more: the customer's IdP
issues the tokens ChatGPT presents for its connection to us, and we check
each one against that issuer's keys. No token is minted or stored here.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
import jwt

log = logging.getLogger(__name__)

# Asymmetric only: an HS* token would be checked against a key the IdP
# publishes, which anyone can read.
ALGORITHMS = ["RS256", "RS384", "RS512", "ES256", "ES384", "PS256", "PS384", "PS512"]
LEEWAY_SECONDS = 30


class Unauthenticated(Exception):
    """No user: a missing, malformed, expired or foreign token. The message
    never carries the token."""


class SigningKeys(Protocol):
    """``jwt.PyJWKClient``'s one method this needs."""

    def get_signing_key_from_jwt(self, token: str) -> Any: ...


@dataclass(frozen=True)
class TokenVerifier:
    issuer: str
    audience: str
    user_claim: str
    jwks: SigningKeys

    async def user(self, authorization: str | None) -> str:
        """The lowercased email the token names; ``Unauthenticated`` otherwise."""
        token = _bearer(authorization)
        if token is None:
            raise Unauthenticated("no bearer token")
        try:
            # PyJWKClient fetches over urllib; keep the event loop free.
            key = await asyncio.to_thread(self.jwks.get_signing_key_from_jwt, token)
            claims = jwt.decode(
                token,
                key.key,
                algorithms=ALGORITHMS,
                audience=self.audience,
                issuer=self.issuer,
                leeway=LEEWAY_SECONDS,
                options={"require": ["exp", "iss", "aud"]},
            )
        except jwt.PyJWKClientError as exc:
            raise Unauthenticated(f"signing key: {exc}") from None
        except jwt.InvalidTokenError as exc:
            raise Unauthenticated(type(exc).__name__) from None
        value = claims.get(self.user_claim)
        if not isinstance(value, str) or "@" not in value:
            raise Unauthenticated(f"no {self.user_claim} claim")
        return value.strip().lower()


def _bearer(header: str | None) -> str | None:
    if not header:
        return None
    scheme, _, token = header.partition(" ")
    return token.strip() if scheme.lower() == "bearer" and token.strip() else None


async def discover_jwks_url(client: httpx.AsyncClient, issuer: str) -> str:
    response = await client.get(f"{issuer}/.well-known/openid-configuration")
    response.raise_for_status()
    url = response.json().get("jwks_uri")
    if not isinstance(url, str) or not url.startswith("https://"):
        raise ValueError(f"{issuer} publishes no https jwks_uri")
    return url


def verifier(*, issuer: str, audience: str, user_claim: str, jwks_url: str) -> TokenVerifier:
    return TokenVerifier(
        issuer=issuer,
        audience=audience,
        user_claim=user_claim,
        jwks=jwt.PyJWKClient(jwks_url, cache_keys=True, lifespan=3600, timeout=5),
    )
