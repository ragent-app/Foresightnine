"""Bounded, fail-closed inspection of a saved HTML table's source structure.

This traces a selected extracted row to original HTML cells. It does not
validate accounting meaning, identify the issuer, or verify a claim.
"""
from __future__ import annotations

import base64
import binascii
from collections import deque
from hashlib import sha256
import re
from threading import BoundedSemaphore, Event, Thread

from .research_fetch import _TextParser
from .research_calculations import quotation_is_inside_table_row
from .research_sources import ResearchSource
from .research_uploads import MAX_PDF_BYTES, MAX_TEXT_BYTES, _UploadedHtmlParser

_TABLE_TRACE_WORKERS = BoundedSemaphore(2)


class _TraceMixin:
    def __init__(self, target: str) -> None:
        super().__init__()
        self.target = target
        self.table_depth = 0
        self.table: dict | None = None
        self.row: dict | None = None
        self.cell: dict | None = None
        self.matches: list[tuple[dict, list[dict], int]] = []

    def visible(self) -> bool:
        return not self.hidden and not getattr(self, "hidden_tags", [])

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        before = self.visible()
        super().handle_starttag(tag, attrs)
        if not before or not self.visible():
            return
        if tag == "table":
            self.table_depth += 1
            if self.table_depth == 1:
                self.table = {"invalid": False, "closed": False, "previous": deque(maxlen=15), "row_count": 0}
            elif self.table is not None:
                self.table["invalid"] = True
        elif tag == "tr" and self.table_depth == 1 and self.table is not None:
            if self.row is not None:
                self.table["invalid"] = True
            self.row = {"part_start": len(self.parts) - 1, "cells": [], "width": 0, "invalid": False}
        elif tag in {"td", "th"} and self.table_depth == 1 and self.row is not None:
            if self.cell is not None:
                self.row["invalid"] = True
            spans = [value for name, value in attrs if name == "colspan"]
            rowspans = [value for name, value in attrs if name == "rowspan"]
            span = spans[0] if len(spans) == 1 else None
            if (len(spans) > 1 or len(rowspans) > 1 or
                    (span is not None and (not span.isascii() or not span.isdigit() or not 1 <= int(span) <= 99)) or
                    any(value is None or not value.isascii() or not value.isdigit() or int(value) != 1 for value in rowspans)):
                self.row["invalid"] = True
            width = int(span) if span is not None and span.isascii() and span.isdigit() and 1 <= int(span) <= 99 else 1
            self.cell = {"part_start": len(self.parts), "span": width, "kind": tag}

    def handle_endtag(self, tag: str) -> None:
        if self.visible() and self.table_depth == 1:
            if tag in {"td", "th"} and self.cell is not None and self.row is not None:
                content = "".join(self.parts[self.cell["part_start"]:]).strip()
                width = self.cell["span"]
                if (len(content) > 1000 or "|" in content or "\n" in content or
                        re.search(r"\[(?:colspan|rowspan)=", content, re.IGNORECASE)):
                    self.row["invalid"] = True
                self.row["cells"].append({"start": self.row["width"], "end": self.row["width"] + width,
                                          "text": content, "kind": self.cell["kind"]})
                self.row["width"] += width
                if self.row["width"] > 120 or len(self.row["cells"]) > 120:
                    self.row["invalid"] = True
                self.cell = None
            elif tag == "tr" and self.row is not None:
                if self.cell is not None:
                    self.row["invalid"] = True
                    self.cell = None
                # _TextParser emits the final row delimiter at the closing tag.
                raw = "".join(self.parts[self.row["part_start"]:]) + " |\n"
                row_text = "\n".join(line.strip() for line in raw.splitlines() if line.strip())
                item = {"text": row_text, "cells": self.row["cells"], "width": self.row["width"],
                        "invalid": self.row["invalid"] or "\n" in row_text or not self.row["cells"]}
                # A rowspan or malformed row outside the returned window can
                # still alter the columns inside it. Reject the whole table.
                if item["invalid"]:
                    self.table["invalid"] = True
                if row_text == self.target:
                    self.matches.append((self.table, [*self.table["previous"], item], self.table["row_count"]))
                self.table["previous"].append(item)
                self.table["row_count"] += 1
                self.row = None
            elif tag == "table" and self.table is not None:
                if self.row is not None or self.cell is not None:
                    self.table["invalid"] = True
                self.table["closed"] = True
        if self.visible() and tag == "table" and self.table_depth:
            self.table_depth -= 1
            if not self.table_depth:
                self.table = self.row = self.cell = None
        super().handle_endtag(tag)


class _UploadedTrace(_TraceMixin, _UploadedHtmlParser):
    pass


class _OfficialTrace(_TraceMixin, _TextParser):
    pass


def trace_html_table(source: ResearchSource, start: int, end: int) -> dict | None:
    """Trace one uniquely occurring extracted row to bounded original cells.

    Callers must enforce ownership and display permission and bound execution.
    Mismatched hashes, ambiguous rows, malformed or unsupported markup return
    None. This is structural provenance only, never a verified financial fact.
    """
    if (source.retrieval_method not in {"user_html", "sec_filing_html", "official_html"} or
            not source.original_html_base64 or not source.response_sha256 or
            not 0 <= start < end <= len(source.text) or
            not quotation_is_inside_table_row(source.text, start, source.text[start:end])):
        return None
    line_start = source.text.rfind("\n", 0, start) + 1
    line_end = source.text.find("\n", end)
    line_end = len(source.text) if line_end < 0 else line_end
    target = source.text[line_start:line_end]
    if len(target) > 4000 or "\n" in target or source.text.count(target) != 1:
        return None
    try:
        raw = base64.b64decode(source.original_html_base64, validate=True)
        if not raw or len(raw) > MAX_PDF_BYTES or sha256(raw).hexdigest() != source.response_sha256:
            return None
        if sha256(source.text.encode("utf-8")).hexdigest() != source.content_sha256:
            return None
        html = raw.decode("utf-8-sig" if source.retrieval_method != "official_html" else "utf-8")
        parser = (_OfficialTrace if source.retrieval_method == "official_html" else _UploadedTrace)(target)
        parser.feed(html)
        parser.close()
        extracted = "\n".join(line.strip() for line in "".join(parser.parts).splitlines() if line.strip())
        if (len(extracted) > MAX_TEXT_BYTES or extracted != source.text or
                getattr(parser, "hidden_tags", []) or len(parser.matches) != 1):
            return None
        table, rows, prior_rows = parser.matches[0]
        if table["invalid"] or not table["closed"] or any(row["invalid"] for row in rows):
            return None
        while len(rows) > 1 and sum(len(row["text"]) for row in rows) > 10_000:
            rows.pop(0)
        return {"source_sha256": source.response_sha256, "row_start": line_start, "row_end": line_end,
                "truncated_before": prior_rows > len(rows) - 1,
                "rows": [{"text": row["text"], "width": row["width"], "cells": row["cells"]} for row in rows]}
    except (binascii.Error, UnicodeError, ValueError, RecursionError, OverflowError):
        return None


def bounded_table_trace(source: ResearchSource, start: int, end: int, timeout: float = 5.0) -> dict:
    """Inspect locally in bounded daemon workers; timed-out work keeps its slot."""
    if not _TABLE_TRACE_WORKERS.acquire(blocking=False):
        return {"status": "busy"}
    done = Event()
    result: list[dict | None] = []
    def run() -> None:
        try:
            result.append(trace_html_table(source, start, end))
        except Exception:
            result.append(None)
        finally:
            _TABLE_TRACE_WORKERS.release()
            done.set()
    try:
        Thread(target=run, name="foresight-html-table", daemon=True).start()
    except RuntimeError:
        _TABLE_TRACE_WORKERS.release()
        return {"status": "busy"}
    if not done.wait(max(0, min(timeout, 5.0))):
        return {"status": "timeout"}
    return {"status": "available", **result[0]} if result and result[0] is not None else {"status": "unavailable"}
