"""Process-wide heavy-memory concurrency gate for constrained 512M/768M containers.

Serializes peak-RAM operations (companyfacts projection, streaming 10-K/10-Q HTML parsing,
single-worker PDF OCR, and >5MB OOXML parsing) so independent worker pools cannot stack
and trigger cgroup OOM kills.
"""
from __future__ import annotations

from contextlib import contextmanager
import os
from threading import BoundedSemaphore
from typing import Iterator


def _heavy_mem_concurrency() -> int:
    raw = os.environ.get("FORESIGHTNINE_HEAVY_MEM_CONCURRENCY", "").strip()
    try:
        val = int(raw)
        if val >= 1:
            return val
    except ValueError:
        pass
    return 1


HEAVY_MEM_GATE = BoundedSemaphore(_heavy_mem_concurrency())


@contextmanager
def heavy_memory_section() -> Iterator[None]:
    """Acquire the process-wide heavy-memory gate for the duration of a peak-RAM block."""
    HEAVY_MEM_GATE.acquire()
    try:
        yield
    finally:
        HEAVY_MEM_GATE.release()
