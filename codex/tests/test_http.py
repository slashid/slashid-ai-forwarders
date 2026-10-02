from __future__ import annotations

import os
import ssl
import sys
from pathlib import Path

import pytest

from slashid_codex.http import make_client, make_ssl_context


async def test_client_ignores_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
    monkeypatch.setenv("ALL_PROXY", "http://proxy.invalid:3128")
    async with make_client(timeout_seconds=7.0) as client:
        assert client.trust_env is False
        assert client.follow_redirects is False
        assert client._mounts == {}
        assert client.timeout.connect == 7.0
        assert client.timeout.read == 7.0


def test_ca_trust_ignores_ssl_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SSL_CERT_FILE", "/dev/null")
    monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path))
    paths = ssl.get_default_verify_paths()
    if not (paths.openssl_cafile and os.path.isfile(paths.openssl_cafile)):
        pytest.skip("no OpenSSL default CA file; capath certs load lazily")
    ctx = make_ssl_context()
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname
    assert ctx.cert_store_stats()["x509_ca"] > 0


def test_ca_trust_falls_back_to_certifi(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SSL_CERT_FILE", "/dev/null")
    monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path))
    if sys.platform == "win32":
        pytest.skip("Windows reads the system store")
    missing = str(tmp_path / "missing")
    monkeypatch.setattr(
        ssl,
        "get_default_verify_paths",
        lambda: ssl.DefaultVerifyPaths(
            missing, missing, "SSL_CERT_FILE", missing, "SSL_CERT_DIR", missing
        ),
    )
    assert make_ssl_context().cert_store_stats()["x509_ca"] > 0
