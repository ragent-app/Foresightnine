import os
from pathlib import Path

import pytest

from app.research_blob_store import DocumentBlobStore, PruneReport


def test_blob_store_prune_dry_run_and_ttl(tmp_path: Path):
    store = DocumentBlobStore(tmp_path / "store")
    b1 = store.put_bytes(b"oldest-blob-1")
    b2 = store.put_bytes(b"older-blob-22")
    b3 = store.put_bytes(b"fresh-blob-333")

    # Non-blob entries (OCR cache and active temp files) must never be touched by prune().
    ocr_dir = store.root / "ocr-cache"
    ocr_dir.mkdir(parents=True, exist_ok=True)
    ocr_file = ocr_dir / "entry.json"
    ocr_file.write_text('{"text": "cached"}', encoding="utf-8")
    incoming = store.root / ".incoming-temp"
    incoming.write_bytes(b"partial")

    now = 1_700_000_000.0
    os.utime(store.path_for(b1), (now - 200, now - 200))
    os.utime(store.path_for(b2), (now - 100, now - 100))
    os.utime(store.path_for(b3), (now - 10, now - 10))
    os.utime(ocr_file, (now - 500, now - 500))
    os.utime(incoming, (now - 500, now - 500))

    dry = store.prune(max_age_seconds=50, dry_run=True, now=now)
    assert dry == PruneReport(
        scanned=3,
        deleted_count=2,
        freed_bytes=len(b"oldest-blob-1") + len(b"older-blob-22"),
        kept_count=1,
        dry_run=True,
    )
    assert store.path_for(b1) is not None and store.path_for(b2) is not None and store.path_for(b3) is not None

    real = store.prune(max_age_seconds=50, dry_run=False, now=now)
    assert real == PruneReport(
        scanned=3,
        deleted_count=2,
        freed_bytes=len(b"oldest-blob-1") + len(b"older-blob-22"),
        kept_count=1,
        dry_run=False,
    )
    assert store.path_for(b1) is None
    assert store.path_for(b2) is None
    assert store.path_for(b3) is not None
    assert ocr_file.is_file()
    assert incoming.is_file()


def test_blob_store_prune_max_total_bytes_mtime_lru_and_protected_hashes(tmp_path: Path):
    store = DocumentBlobStore(tmp_path / "store")
    d_oldest = store.put_bytes(b"a" * 100)
    d_second = store.put_bytes(b"b" * 200)
    d_third = store.put_bytes(b"c" * 300)
    d_newest = store.put_bytes(b"d" * 400)

    os.utime(store.path_for(d_oldest), (1000, 1000))
    os.utime(store.path_for(d_second), (2000, 2000))
    os.utime(store.path_for(d_third), (3000, 3000))
    os.utime(store.path_for(d_newest), (4000, 4000))

    # Protect the oldest blob (100 bytes). Total is 1000 bytes; cap at 600 bytes must evict
    # the next oldest unprotected blobs (200 + 300) so remaining = 100 + 400 = 500 <= 600.
    report = store.prune(
        max_total_bytes=600,
        protected_sha256s=[d_oldest.upper(), "not-a-sha256"],
    )
    assert report == PruneReport(scanned=4, deleted_count=2, freed_bytes=500, kept_count=2, dry_run=False)
    assert store.path_for(d_oldest) is not None
    assert store.path_for(d_second) is None
    assert store.path_for(d_third) is None
    assert store.path_for(d_newest) is not None

    # Even when protected blobs alone exceed max_total_bytes, protected hashes are never deleted.
    strict = store.prune(max_age_seconds=0, max_total_bytes=50, protected_sha256s={d_oldest, d_newest}, now=5000)
    assert strict == PruneReport(scanned=2, deleted_count=0, freed_bytes=0, kept_count=2, dry_run=False)
    assert store.path_for(d_oldest) is not None and store.path_for(d_newest) is not None


def test_blob_store_prune_handles_missing_root_and_invalid_bounds(tmp_path: Path):
    store = DocumentBlobStore(tmp_path / "missing")
    assert store.prune(max_age_seconds=10, max_total_bytes=100) == PruneReport(0, 0, 0, 0, False)
    with pytest.raises(ValueError):
        store.prune(max_age_seconds=-1)
    with pytest.raises(ValueError):
        store.prune(max_total_bytes=-1)
