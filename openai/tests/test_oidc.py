from __future__ import annotations

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from slashid_openai_forwarder.oidc import TokenVerifier, Unauthenticated

from .conftest import Signer


async def test_the_email_claim_is_the_user(tokens: TokenVerifier, sign: Signer) -> None:
    assert await tokens.user(f"Bearer {sign()}") == "alice@example.com"


@pytest.mark.parametrize(
    "overrides",
    [
        {"aud": "https://elsewhere.example.com/mcp"},
        {"iss": "https://evil.example.com"},
        {"exp": 1},
        {"email": None},
        {"email": "not-an-email"},
    ],
    ids=["audience", "issuer", "expired", "no-email", "not-an-email"],
)
async def test_a_token_that_does_not_name_a_user_here_is_refused(
    tokens: TokenVerifier, sign: Signer, overrides: dict
) -> None:
    with pytest.raises(Unauthenticated):
        await tokens.user(f"Bearer {sign(**overrides)}")


@pytest.mark.parametrize("header", [None, "", "Basic abc", "Bearer ", "Bearer not.a.jwt"])
async def test_no_usable_bearer_is_refused(tokens: TokenVerifier, header: str | None) -> None:
    with pytest.raises(Unauthenticated):
        await tokens.user(header)


async def test_a_token_signed_by_another_key_is_refused(tokens: TokenVerifier) -> None:
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = jwt.encode(
        {"iss": tokens.issuer, "aud": tokens.audience, "email": "a@b.c", "exp": 2**31},
        other,
        "RS256",
    )
    with pytest.raises(Unauthenticated):
        await tokens.user(f"Bearer {token}")


async def test_a_symmetric_token_is_refused(tokens: TokenVerifier) -> None:
    token = jwt.encode(
        {"iss": tokens.issuer, "aud": tokens.audience, "email": "a@b.c", "exp": 2**31},
        "secret-that-is-long-enough-for-hs256-xx",
        "HS256",
    )
    with pytest.raises(Unauthenticated):
        await tokens.user(f"Bearer {token}")
