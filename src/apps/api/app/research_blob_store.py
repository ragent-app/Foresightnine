"""Content-addressed file store for large document originals and OCR caches.

Files are stored under ``<root>/<sha256[:2]>/<sha256>`` and written atomically
(temp file in the same directory, then ``os.replace``). Identical content is
stored once. Only lowercase 64-character hex digests are accepted as keys, so a
key can never escape the store root.

Out of scope for this module: retention policy, quota and deletion. Nothing is
ever removed here; operators own the volume lifecycle until a policy exists.
"""
from __future__ import annotations

from hashlib import sha256
import os
from pathlib import Path
import re
import tempfile

STORE_DIR_ENV = "FORESIGHTNINE_DOCUMENT_STORE_DIR"
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_CHUNK = 1024 * 1024


def is_valid_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


class DocumentBlobStore:
    def __init__(self, root: Path):
        self.root = Path(root)

    def _target(self, digest: str) -> Path:
        if not is_valid_sha256(digest):
            raise ValueError("invalid sha256 digest")
        return self.root / digest[:2] / digest

    def path_for(self, digest: str) -> Path | None:
        """Path of a stored blob, or None when absent or the digest is not valid hex."""
        if not is_valid_sha256(digest):
            return None
        target = self._target(digest)
        return target if target.is_file() else None

    def _commit(self, temp_path: str, digest: str) -> str:
        target = self._target(digest)
        if target.is_file():
            os.unlink(temp_path)
            return digest
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(temp_path, target)
        return digest

    def put_file(self, path: Path) -> str:
        """Copy a file into the store; returns its sha256 hex digest."""
        self.root.mkdir(parents=True, exist_ok=True)
        hasher = sha256()
        descriptor, temp_path = tempfile.mkstemp(prefix=".incoming-", dir=self.root)
        try:
            with os.fdopen(descriptor, "wb") as out, Path(path).open("rb") as source:
                while chunk := source.read(_CHUNK):
                    hasher.update(chunk)
                    out.write(chunk)
                out.flush()
                os.fsync(out.fileno())
            return self._commit(temp_path, hasher.hexdigest())
        except BaseException:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            raise

    def put_bytes(self, data: bytes) -> str:
        self.root.mkdir(parents=True, exist_ok=True)
        digest = sha256(data).hexdigest()
        descriptor, temp_path = tempfile.mkstemp(prefix=".incoming-", dir=self.root)
        try:
            with os.fdopen(descriptor, "wb") as out:
                out.write(data)
                out.flush()
                os.fsync(out.fileno())
            return self._commit(temp_path, digest)
        except BaseException:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            raise


def configured_blob_store() -> DocumentBlobStore | None:
    raw = os.environ.get(STORE_DIR_ENV, "").strip()
    return DocumentBlobStore(Path(raw)) if raw else None
