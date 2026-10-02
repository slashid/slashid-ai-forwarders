import hashlib
import mimetypes
import os
import threading
from pathlib import Path

import pytest

from slashid_ai_forwarder_core.events import AIAccessedFile
from slashid_ai_forwarder_core.files import hash_local_file


def test_hashes_small_file(tmp_path: Path) -> None:
    path = tmp_path / "x.md"
    path.write_bytes(b"hello")
    got = hash_local_file(path, max_bytes=100, provenance="tool_result")
    assert got.name == str(path)
    assert got.content_hashes == {
        "sha256": hashlib.sha256(b"hello").hexdigest(),
        "sha1": hashlib.sha1(b"hello").hexdigest(),
        "md5": hashlib.md5(b"hello").hexdigest(),
    }
    assert got.byte_length == 5
    assert got.media_type == mimetypes.guess_type("x.md")[0]
    assert got.provenance == "tool_result"


def test_over_max_bytes_has_size_but_no_hashes(tmp_path: Path) -> None:
    path = tmp_path / "big.bin"
    path.write_bytes(b"0123456789")
    got = hash_local_file(path, max_bytes=5, provenance="attachment")
    assert got.content_hashes is None
    assert got.byte_length == 10
    assert got.provenance == "attachment"


def test_missing_file(tmp_path: Path) -> None:
    got = hash_local_file(tmp_path / "nope.txt", max_bytes=100, provenance="tool_result")
    assert got.content_hashes is None
    assert got.byte_length is None


def test_directory_is_like_missing(tmp_path: Path) -> None:
    got = hash_local_file(tmp_path, max_bytes=100, provenance="tool_result")
    assert got.content_hashes is None
    assert got.byte_length is None


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX only")
def test_fifo_is_not_read(tmp_path: Path) -> None:
    path = tmp_path / "pipe"
    os.mkfifo(path)
    result: list[object] = []
    worker = threading.Thread(
        target=lambda: result.append(
            hash_local_file(path, max_bytes=100, provenance="tool_result")
        ),
        daemon=True,
    )
    worker.start()
    worker.join(timeout=5)
    assert not worker.is_alive()
    (got,) = result
    assert isinstance(got, AIAccessedFile)
    assert got.content_hashes is None
    assert got.byte_length is None


def test_symlink_hashes_target(tmp_path: Path) -> None:
    target = tmp_path / "x.md"
    target.write_bytes(b"hello")
    link = tmp_path / "link.md"
    link.symlink_to(target)
    got = hash_local_file(link, max_bytes=100, provenance="tool_result")
    assert got.name == str(link)
    assert got.content_hashes is not None
    assert got.content_hashes["sha256"] == hashlib.sha256(b"hello").hexdigest()
    assert got.byte_length == 5


def test_growth_past_cap_counts_as_over(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "grows.bin"
    path.write_bytes(b"0123456789")
    real_fstat = os.fstat

    def stale_fstat(fd: int) -> os.stat_result:
        info = real_fstat(fd)
        return os.stat_result((info.st_mode, *info[1:6], 3, *info[7:]))

    monkeypatch.setattr(os, "fstat", stale_fstat)
    got = hash_local_file(path, max_bytes=5, provenance="tool_result")
    assert got.content_hashes is None
    assert got.byte_length == 6
