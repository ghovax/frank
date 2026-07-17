"""FilePart ingest/emit and signed-URL round trips (``harness.core.a2a_files``)."""

import base64
import time


from a2a.types import FilePart, FileWithBytes, FileWithUri, Part

from harness.core.a2a_files import (
    FileUrlSigner,
    build_file_part,
    ingest_file_part,
    load_or_create_secret,
)


async def test_ingest_bytes_stores_and_dedupes(tmp_path):
    payload = b"hello file"
    part = FilePart(file=FileWithBytes(bytes=base64.b64encode(payload).decode(), name="note.txt", mime_type="text/plain"))
    attachment = await ingest_file_part(part, tmp_path)
    assert attachment is not None
    stored = tmp_path / "uploads" / f"{attachment['sha256']}.txt"
    assert stored.read_bytes() == payload
    assert attachment["mime_type"] == "text/plain"
    assert attachment["size"] == len(payload)

    # Identical bytes dedupe to the same content-addressed path.
    again = await ingest_file_part(part, tmp_path)
    assert again["path"] == attachment["path"]


async def test_ingest_rejects_oversize(tmp_path):
    part = FilePart(file=FileWithBytes(bytes=base64.b64encode(b"x" * 100).decode(), name="big.bin"))
    assert await ingest_file_part(part, tmp_path, maximum_bytes=10) is None


async def test_ingest_refuses_non_http_uri(tmp_path):
    part = FilePart(file=FileWithUri(uri="file:///etc/passwd", name="passwd"))
    assert await ingest_file_part(part, tmp_path) is None


def test_signer_round_trip_and_expiry(tmp_path):
    secret = load_or_create_secret(tmp_path)
    # Persisted and stable across calls.
    assert load_or_create_secret(tmp_path) == secret

    signer = FileUrlSigner(secret, "http://localhost:8822")
    url = signer.sign("/data/report.pdf", ttl_seconds=60)
    token = url.rsplit("/", 1)[1]
    assert signer.verify(token) == "/data/report.pdf"

    # Tampered token fails.
    assert signer.verify(token[:-2] + "xy") is None

    # An already-expired token fails.
    import jwt

    expired = jwt.encode({"path": "/data/report.pdf", "exp": int(time.time()) - 10}, secret, algorithm="HS256")
    assert signer.verify(expired) is None

    # A different secret cannot verify.
    assert FileUrlSigner(b"other-secret-32-bytes-length!!", "http://localhost:8822").verify(token) is None


def test_build_file_part_produces_signed_uri(tmp_path):
    stored = tmp_path / "uploads"
    stored.mkdir()
    file_path = stored / "abc.txt"
    file_path.write_text("data")
    signer = FileUrlSigner(b"secret", "http://localhost:8822")
    part: Part = build_file_part(
        {"path": str(file_path), "filename": "abc.txt", "mime_type": "text/plain"}, signer
    )
    assert isinstance(part.root, FilePart)
    assert isinstance(part.root.file, FileWithUri)
    token = part.root.file.uri.rsplit("/", 1)[1]
    assert signer.verify(token) == str(file_path)


def test_build_file_part_missing_file_returns_none(tmp_path):
    signer = FileUrlSigner(b"secret", "http://localhost:8822")
    assert build_file_part({"path": str(tmp_path / "nope.txt")}, signer) is None
