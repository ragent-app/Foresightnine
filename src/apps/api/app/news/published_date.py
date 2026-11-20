"""Publication-date extraction from fetched HTML, with provenance of where each value came from.

Priority: JSON-LD ``datePublished`` > meta tags > ``<time datetime>`` > labelled visible text > (low confidence)
unlabelled visible text near the top. Naive values keep ``timezone == "timezone_unknown"``; nothing is ever inferred
from crawl time. Work is bounded: input is truncated, every regex is linear on bounded strings, and the HTML parser is
the stdlib streaming one with caps on collected scripts, tags and text.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser

MAX_HTML_CHARS = 1_500_000
_MAX_JSONLD_CHARS = 200_000
_MAX_JSONLD_BLOCKS = 12
_MAX_META = 400
_MAX_TIME_TAGS = 60
_MAX_SEGMENTS = 600
_MAX_SEGMENT_CHARS = 400
_MAX_VISIBLE_CHARS = 60_000
_UNLABELLED_WINDOW = 3_000
_YEAR_RANGE = (1990, 2100)

PUBLISHED_META = ("article:published_time", "og:published_time", "og:article:published_time", "pubdate", "date",
                  "dc.date.issued", "dcterms.issued", "parsely-pub-date", "sailthru.date", "publishdate", "publish_date",
                  "citation_publication_date", "citation_date", "datepublished")
MODIFIED_META = ("article:modified_time", "og:updated_time", "og:article:modified_time", "last-modified",
                 "dc.date.modified", "dcterms.modified", "datemodified", "revised")

_MONTHS = {m: i + 1 for i, names in enumerate([
    ("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"), ("may",), ("jun", "june"),
    ("jul", "july"), ("aug", "august"), ("sep", "sept", "september"), ("oct", "october"), ("nov", "november"),
    ("dec", "december")]) for m in names}
_MONTH_RE = r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
_TZ_ABBR = {"utc": 0, "gmt": 0, "z": 0, "est": -300, "edt": -240, "pst": -480, "pdt": -420, "mst": -420, "mdt": -360,
            "cet": 60, "cest": 120, "bst": 60, "jst": 540, "hkt": 480}

_TIME_PART = r"(?:[ T,]+(\d{1,2}):(\d{2})(?::(\d{2}))?\s*([ap]\.?m\.?)?\s*(utc|gmt|[ecmp][sd]t|cest|cet|bst|jst|hkt|z)?)?"
_RE_CN = re.compile(r"(\d{4})\s?年\s?(\d{1,2})\s?月\s?(\d{1,2})\s?[日号]?(?:[ T,]*(\d{1,2})[:：时](\d{2})(?:[:：分](\d{2}))?)?")
_RE_MDY = re.compile(r"\b(" + _MONTH_RE + r")\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b" + _TIME_PART, re.I)
_RE_DMY = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(" + _MONTH_RE + r")\.?,?\s+(\d{4})\b" + _TIME_PART, re.I)
_RE_ISO = re.compile(r"\b(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})\b(?:[ T]+(\d{1,2}):(\d{2})(?::(\d{2}))?)?")
_PUBLISHED_LABELS = ("published", "posted", "publication date", "date published", "release date", "发布时间", "发布日期",
                     "发布于", "发表于", "发表时间", "出版", "时间：", "日期：", "来源")
_MODIFIED_LABELS = ("updated", "modified", "last update", "更新时间", "更新于", "修改时间", "最后更新")
_ARTICLE_TYPE = re.compile(r"article|posting|report", re.I)


@dataclass(frozen=True)
class DateFound:
    value: datetime            # aware when ``timezone`` is "offset"/"utc"; naive when "timezone_unknown"
    precision: str             # "datetime" | "date"
    timezone: str              # "offset" | "utc" | "timezone_unknown"
    source: str                # e.g. "json_ld.datePublished", "meta:article:published_time", "time_tag", "visible_text"
    raw: str
    confidence: str            # "high" | "medium" | "low"

    def iso(self) -> str:
        return self.value.date().isoformat() if self.precision == "date" else self.value.isoformat()


@dataclass(frozen=True)
class PublishedDateResult:
    published: DateFound | None
    modified: DateFound | None
    reason: str | None         # None when published found; otherwise "no_date_found" / "empty_input" / ...
    truncated: bool = False


# ---------------------------------------------------------------- value parsing

def _valid_year(year: int) -> bool:
    return _YEAR_RANGE[0] <= year <= _YEAR_RANGE[1]


def _build(year: int, month: int, day: int, hour: int | None, minute: int, second: int, tz: timezone | None,
           raw: str, source: str, confidence: str) -> DateFound | None:
    if not _valid_year(year):
        return None
    try:
        if hour is None:
            return DateFound(datetime(year, month, day), "date", "timezone_unknown", source, raw[:80], confidence)
        value = datetime(year, month, day, hour, minute, second, tzinfo=tz)
    except ValueError:
        return None
    zone = "timezone_unknown" if tz is None else ("utc" if value.utcoffset() == timedelta(0) else "offset")
    return DateFound(value, "datetime", zone, source, raw[:80], confidence)


def parse_date_value(raw: str, source: str, confidence: str) -> DateFound | None:
    """Parse a machine-formatted value (ISO 8601, RFC 2822, ``YYYY/MM/DD``)."""
    text = (raw or "").strip()[:80]
    if not text:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        y, m, d = (int(p) for p in text.split("-"))
        return _build(y, m, d, None, 0, 0, None, text, source, confidence)
    iso = text.replace("Z", "+00:00").replace("z", "+00:00") if text.endswith(("Z", "z")) else text
    iso = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", iso)       # +0800 -> +08:00
    iso = re.sub(r"^(\d{4})/(\d{1,2})/(\d{1,2})", lambda m: f"{m[1]}-{int(m[2]):02d}-{int(m[3]):02d}", iso)
    try:
        value = datetime.fromisoformat(iso)
    except ValueError:
        value = None
    if value is not None and _valid_year(value.year):
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", iso):
            return DateFound(datetime(value.year, value.month, value.day), "date", "timezone_unknown", source, text, confidence)
        zone = "timezone_unknown" if value.tzinfo is None else ("utc" if value.utcoffset() == timedelta(0) else "offset")
        return DateFound(value, "datetime", zone, source, text, confidence)
    try:
        value = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        value = None
    if value is not None and _valid_year(value.year):
        zone = "timezone_unknown" if value.tzinfo is None else ("utc" if value.utcoffset() == timedelta(0) else "offset")
        return DateFound(value, "datetime", zone, source, text, confidence)
    # Fall back to the human-readable patterns for meta values like "Nov 19, 2026".
    return find_text_date(text, source, confidence)


def _tz_from(abbr: str | None) -> timezone | None:
    if not abbr:
        return None
    minutes = _TZ_ABBR.get(abbr.lower())
    return None if minutes is None else timezone(timedelta(minutes=minutes))


def _twelve(hour: int, meridiem: str | None) -> int | None:
    if not meridiem:
        return hour
    if not 1 <= hour <= 12:
        return None
    pm = meridiem.lower().startswith("p")
    return (hour % 12) + (12 if pm else 0)


def find_text_date(text: str, source: str, confidence: str) -> DateFound | None:
    """Find the first date written in prose (Chinese, ``Nov. 19, 2026``, ``19 November 2026``, ISO). ``text`` is bounded."""
    text = text[:_MAX_SEGMENT_CHARS * 2]
    hits: list[tuple[int, DateFound]] = []
    m = _RE_CN.search(text)
    if m:
        hour = int(m[4]) if m[4] else None
        f = _build(int(m[1]), int(m[2]), int(m[3]), hour, int(m[5] or 0), int(m[6] or 0), None, m[0], source, confidence)
        if f:
            hits.append((m.start(), f))
    m = _RE_MDY.search(text)
    if m:
        hour = _twelve(int(m[4]), m[7]) if m[4] else None
        if not (m[4] and hour is None):
            f = _build(int(m[3]), _MONTHS[m[1].lower().rstrip(".")], int(m[2]), hour, int(m[5] or 0), int(m[6] or 0), _tz_from(m[8]), m[0], source, confidence)
            if f:
                hits.append((m.start(), f))
    m = _RE_DMY.search(text)
    if m:
        hour = _twelve(int(m[4]), m[7]) if m[4] else None
        if not (m[4] and hour is None):
            f = _build(int(m[3]), _MONTHS[m[2].lower().rstrip(".")], int(m[1]), hour, int(m[5] or 0), int(m[6] or 0), _tz_from(m[8]), m[0], source, confidence)
            if f:
                hits.append((m.start(), f))
    m = _RE_ISO.search(text)
    if m:
        hour = int(m[4]) if m[4] else None
        f = _build(int(m[1]), int(m[2]), int(m[3]), hour, int(m[5] or 0), int(m[6] or 0), None, m[0], source, confidence)
        if f:
            hits.append((m.start(), f))
    return min(hits, key=lambda h: h[0])[1] if hits else None


# ---------------------------------------------------------------- HTML collection

class _Collector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: list[tuple[str, str]] = []
        self.jsonld: list[str] = []
        self.times: list[tuple[str, str, str]] = []   # (datetime attr, hint text from itemprop/class, inner text)
        self.segments: list[str] = []
        self._skip = 0
        self._ld = False
        self._ld_buf: list[str] = []
        self._ld_size = 0
        self._time_open: list | None = None
        self._visible = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "meta":
            key = (a.get("property") or a.get("name") or a.get("itemprop") or a.get("http-equiv") or "").strip().lower()
            content = a.get("content", "").strip()
            if key and content and len(self.meta) < _MAX_META:
                self.meta.append((key, content))
        elif tag == "script":
            if "ld+json" in a.get("type", "").lower() and len(self.jsonld) < _MAX_JSONLD_BLOCKS:
                self._ld, self._ld_buf, self._ld_size = True, [], 0
            else:
                self._skip += 1
        elif tag in ("style", "noscript", "template", "head", "svg"):
            self._skip += 1 if tag != "head" else 0
        elif tag == "time" and len(self.times) < _MAX_TIME_TAGS:
            hint = " ".join([a.get("itemprop", ""), a.get("class", ""), a.get("pubdate", "") and "pubdate",
                             a.get("id", "")]).lower()
            self._time_open = [a.get("datetime", "").strip(), hint, []]
        if a.get("hidden") is not None and tag not in ("meta", "script"):
            pass

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            if self._ld:
                self._ld = False
                if self._ld_buf:
                    self.jsonld.append("".join(self._ld_buf))
            elif self._skip:
                self._skip -= 1
        elif tag in ("style", "noscript", "template", "svg") and self._skip:
            self._skip -= 1
        elif tag == "time" and self._time_open is not None:
            dt, hint, inner = self._time_open
            self.times.append((dt, hint, " ".join("".join(inner).split())[:120]))
            self._time_open = None

    def handle_data(self, data: str) -> None:
        if self._ld:
            if self._ld_size < _MAX_JSONLD_CHARS:
                self._ld_buf.append(data[: _MAX_JSONLD_CHARS - self._ld_size])
                self._ld_size += len(data)
            return
        if self._skip:
            return
        if self._time_open is not None:
            self._time_open[2].append(data)
        text = " ".join(data.split())
        if text and len(self.segments) < _MAX_SEGMENTS and self._visible < _MAX_VISIBLE_CHARS:
            self.segments.append(text[:_MAX_SEGMENT_CHARS])
            self._visible += len(text)


def _walk_jsonld(node, key: str, out: list, depth: int = 0, budget: list | None = None) -> None:
    budget = budget if budget is not None else [2000]
    budget[0] -= 1
    if budget[0] <= 0 or depth > 8:
        return
    if isinstance(node, list):
        for item in node[:200]:
            _walk_jsonld(item, key, out, depth + 1, budget)
    elif isinstance(node, dict):
        types = node.get("@type")
        types = types if isinstance(types, list) else [types]
        article = any(isinstance(t, str) and _ARTICLE_TYPE.search(t) for t in types)
        if article and isinstance(node.get(key), str):
            out.append(node[key])
        for k, v in list(node.items())[:60]:
            if isinstance(v, (dict, list)):
                _walk_jsonld(v, key, out, depth + 1, budget)


def _from_jsonld(blocks: list[str], key: str, label: str) -> DateFound | None:
    for block in blocks:
        try:
            data = json.loads(block, strict=False)
        except (ValueError, RecursionError):
            continue
        found: list[str] = []
        _walk_jsonld(data, key, found)
        for raw in found:
            result = parse_date_value(raw, f"json_ld.{label}", "high")
            if result:
                return result
    return None


def _from_meta(meta: list[tuple[str, str]], keys: tuple[str, ...]) -> DateFound | None:
    by_key: dict[str, str] = {}
    for k, v in meta:
        by_key.setdefault(k, v)
    for key in keys:
        if key in by_key:
            result = parse_date_value(by_key[key], f"meta:{key}", "high")
            if result:
                return result
    return None


def _from_time_tags(times: list[tuple[str, str, str]], modified: bool) -> DateFound | None:
    pub_hint = ("datepublished", "pubdate", "publish", "posted", "entry-date", "created")
    mod_hint = ("datemodified", "modified", "updated")
    wanted = mod_hint if modified else pub_hint
    other = mod_hint if not modified else pub_hint
    for dt, hint, inner in times:
        if any(h in hint for h in wanted) and not any(h in hint for h in other):
            value = (dt and parse_date_value(dt, "time_tag", "medium")) or find_text_date(inner, "time_tag", "medium")
            if value:
                return value
    if not modified:   # an unlabelled first <time> is weaker evidence than a labelled one
        for dt, hint, inner in times:
            if any(h in hint for h in mod_hint):
                continue
            value = (dt and parse_date_value(dt, "time_tag_unlabelled", "low")) or find_text_date(inner, "time_tag_unlabelled", "low")
            if value:
                return value
    return None


def _from_visible(segments: list[str], modified: bool) -> DateFound | None:
    for seg in segments:
        low = seg.lower()
        if modified:
            matches = any(l in low for l in _MODIFIED_LABELS)
        else:
            matches = any(l in low for l in _PUBLISHED_LABELS)
        if matches:
            found = find_text_date(seg, "visible_text:labelled", "medium")
            if found:
                return found
    if modified:
        return None
    consumed = 0
    for seg in segments:
        if consumed > _UNLABELLED_WINDOW:
            break
        consumed += len(seg)
        if any(l in seg.lower() for l in _MODIFIED_LABELS):
            continue
        found = find_text_date(seg, "visible_text:unlabelled", "low")
        if found:
            return found
    return None


def extract_published_date(html: str | bytes | None) -> PublishedDateResult:
    """Extract publication and modification time from ``html``; ``published`` is None (with a reason) when absent."""
    if html is None:
        return PublishedDateResult(None, None, "empty_input")
    if isinstance(html, bytes):
        html = html[:MAX_HTML_CHARS * 2].decode("utf-8", errors="replace")
    if not html.strip():
        return PublishedDateResult(None, None, "empty_input")
    truncated = len(html) > MAX_HTML_CHARS
    html = html[:MAX_HTML_CHARS]
    collector = _Collector()
    try:
        collector.feed(html)
        collector.close()
    except Exception:  # hostile markup must never raise
        pass
    published = (_from_jsonld(collector.jsonld, "datePublished", "datePublished")
                 or _from_meta(collector.meta, PUBLISHED_META)
                 or _from_time_tags(collector.times, False)
                 or _from_visible(collector.segments, False))
    modified = (_from_jsonld(collector.jsonld, "dateModified", "dateModified")
                or _from_meta(collector.meta, MODIFIED_META)
                or _from_time_tags(collector.times, True)
                or _from_visible(collector.segments, True))
    return PublishedDateResult(published, modified, None if published else "no_date_found", truncated)
