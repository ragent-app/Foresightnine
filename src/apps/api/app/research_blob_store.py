"""Content-addressed file store for large document originals and OCR caches.

Files are stored under ``<root>/<sha256[:2]>/<sha256>`` and written atomically
(temp file in the same directory, then ``os.replace``). Identical content is
stored once. Only lowercase 64-character hex digests are accepted as keys, so a
key can never escape the store root.

``DocumentBlobStore.prune()`` provides bounded mtime-LRU / TTL retention over
stored content-addressed blobs while skipping protected hashes and leaving
non-blob subdirectories (such as ``ocr-cache``) untouched.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from hashlib import sha256
import os
from pathlib import Path
import re
import tempfile
import time

STORE_DIR_ENV = "FORESIGHTNINE_DOCUMENT_STORE_DIR"
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_PREFIX_DIR_RE = re.compile(r"[0-9a-f]{2}")
_CHUNK = 1024 * 1024


def is_valid_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


@dataclass(frozen=True)
class PruneReport:
    scanned: int = 0
    deleted_count: int = 0
    freed_bytes: int = 0
    kept_count: int = 0
    dry_run: bool = False


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

    def _iter_blobs(self) -> list[tuple[float, int, str, Path]]:
        if not self.root.is_dir():
            return []
        entries: list[tuple[float, int, str, Path]] = []
        try:
            subdirs = list(self.root.iterdir())
        except OSError:
            return []
        for subdir in subdirs:
            if _PREFIX_DIR_RE.fullmatch(subdir.name) is None or subdir.is_symlink() or not subdir.is_dir():
                continue
            try:
                children = list(subdir.iterdir())
            except OSError:
                continue
            for child in children:
                digest = child.name
                if not is_valid_sha256(digest) or digest[:2] != subdir.name or child.is_symlink():
                    continue
                try:
                    stat = child.stat(follow_symlinks=False)
                except OSError:
                    continue
                if not child.is_file():
                    continue
                entries.append((stat.st_mtime, stat.st_size, digest, child))
        return entries

    def prune(
        self,
        max_age_seconds: float | None = None,
        max_total_bytes: int | None = None,
        protected_sha256s: Iterable[str] | None = None,
        dry_run: bool = False,
        *,
        now: float | None = None,
    ) -> PruneReport:
        """Prune expired and over-quota blobs using mtime-LRU ordering, never deleting protected hashes."""
        import math

        if max_age_seconds is not None:
            if isinstance(max_age_seconds, bool) or not isinstance(max_age_seconds, (int, float)) or not math.isfinite(max_age_seconds) or max_age_seconds < 0:
                raise ValueError("max_age_seconds must be a finite non-negative number")
        if max_total_bytes is not None:
            if type(max_total_bytes) is not int or max_total_bytes < 0:
                raise ValueError("max_total_bytes must be a non-negative integer")

        protected: set[str] = set()
        if protected_sha256s is not None:
            for item in protected_sha256s:
                if isinstance(item, str):
                    normalized = item.strip().lower()
                    if is_valid_sha256(normalized):
                        protected.add(normalized)

        blobs = self._iter_blobs()
        scanned = len(blobs)
        if not blobs:
            return PruneReport(scanned=0, deleted_count=0, freed_bytes=0, kept_count=0, dry_run=dry_run)

        current_time = float(now) if now is not None else time.time()
        candidates: list[tuple[float, int, str, Path]] = []
        remaining: list[tuple[float, int, str, Path]] = []

        for entry in blobs:
            mtime, size, digest, _ = entry
            if digest not in protected and max_age_seconds is not None and (current_time - mtime) >= max_age_seconds:
                candidates.append(entry)
            else:
                remaining.append(entry)

        if max_total_bytes is not None:
            total_bytes = sum(entry[1] for entry in remaining)
            if total_bytes > max_total_bytes:
                evictable = sorted((entry for entry in remaining if entry[2] not in protected), key=lambda item: (item[0], item[2]))
                for entry in evictable:
                    if total_bytes <= max_total_bytes:
                        break
                    candidates.append(entry)
                    total_bytes -= entry[1]

        deleted_count = 0
        freed_bytes = 0
        if dry_run:
            deleted_count = len(candidates)
            freed_bytes = sum(entry[1] for entry in candidates)
        else:
            for _, size, _, path in candidates:
                try:
                    path.unlink()
                except OSError:
                    continue
                deleted_count += 1
                freed_bytes += size
                try:
                    path.parent.rmdir()
                except OSError:
                    pass

        return PruneReport(
            scanned=scanned,
            deleted_count=deleted_count,
            freed_bytes=freed_bytes,
            kept_count=scanned - deleted_count,
            dry_run=dry_run,
        )


def configured_blob_store() -> DocumentBlobStore | None:
    raw = os.environ.get(STORE_DIR_ENV, "").strip()
    return DocumentBlobStore(Path(raw)) if raw else None
