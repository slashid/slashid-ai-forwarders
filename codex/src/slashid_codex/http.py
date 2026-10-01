"""The outbound client to SlashID."""

from __future__ import annotations

import os
import ssl
import sys

import certifi
import httpx


def make_ssl_context() -> ssl.SSLContext:
    """The system's CAs, never ``SSL_CERT_FILE``/``SSL_CERT_DIR``: the daemon
    inherits the first hook's environment. Falls back to certifi's bundle when
    OpenSSL's default paths are absent (some uv-managed Pythons on macOS)."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    if sys.platform == "win32":
        ctx.load_default_certs()
        return ctx
    paths = ssl.get_default_verify_paths()
    cafile = paths.openssl_cafile if os.path.isfile(paths.openssl_cafile) else None
    capath = paths.openssl_capath if os.path.isdir(paths.openssl_capath) else None
    if cafile or capath:
        ctx.load_verify_locations(cafile=cafile, capath=capath)
    else:
        ctx.load_verify_locations(cafile=certifi.where())
    return ctx


def make_client(*, timeout_seconds: float = 10.0) -> httpx.AsyncClient:
    """No proxy, CA bundle or ``.netrc`` from the environment (hooks inherit
    the user's), no redirects."""
    return httpx.AsyncClient(
        trust_env=False,
        follow_redirects=False,
        verify=make_ssl_context(),
        timeout=httpx.Timeout(timeout_seconds),
    )
