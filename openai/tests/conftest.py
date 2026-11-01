"""Shared fixtures: an RSA key pair, a token signer and a verifier trusting it."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import jwt
import jwt.algorithms
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from slashid_openai_forwarder.oidc import TokenVerifier

ISSUER = "https://idp.example.com"
AUDIENCE = "https://hooks.example.com/mcp"

Signer = Callable[..., str]


class _Keys:
    def __init__(self, public: Any) -> None:
        self._public = public

    def get_signing_key_from_jwt(self, token: str) -> Any:
        return jwt.PyJWK.from_dict(
            {**jwt.algorithms.RSAAlgorithm.to_jwk(self._public, as_dict=True), "alg": "RS256"}
        )


@pytest.fixture(scope="session")
def key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def sign(key: rsa.RSAPrivateKey) -> Signer:
    def _sign(**overrides: Any) -> str:
        now = int(time.time())
        claims = {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": "00u1",
            "email": "Alice@Example.com",
            "iat": now,
            "exp": now + 300,
            **overrides,
        }
        return jwt.encode({k: v for k, v in claims.items() if v is not None}, key, "RS256")

    return _sign


@pytest.fixture
def tokens(key: rsa.RSAPrivateKey) -> TokenVerifier:
    return TokenVerifier(
        issuer=ISSUER, audience=AUDIENCE, user_claim="email", jwks=_Keys(key.public_key())
    )
