import hashlib
import mimetypes
from pathlib import Path

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
