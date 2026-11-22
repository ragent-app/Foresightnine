"""OCR for scanned PDF pages: local Tesseract first, optional vision-model fallback.

Policy (see ``ocr_pdf_pages``): every page is rendered once (``pdftoppm``), run
through Tesseract, and only when a vision engine is configured and the result is
unusable (engine missing/failed, low mean confidence, empty or tabular text) is
the vision engine called. Vision token usage is returned per page so the caller
can report it. Results are cached by (page image hash, engine, engine version,
languages); cache hits never carry usage because nothing was billed.

Environment: FORESIGHTNINE_OCR_DEFAULT_LANGS (eng), FORESIGHTNINE_OCR_WORKERS (2),
FORESIGHTNINE_OCR_FALLBACK_CONFIDENCE (70), and for the vision engine
FORESIGHTNINE_OCR_VISION_{PROTOCOL,BASE_URL,MODEL,API_KEY,MAX_OUTPUT_TOKENS,
ALLOWED_HOSTS,ANTHROPIC_VERSION,AUTH_SCHEME}. Nothing about a vendor or model is
hard coded; an unset variable leaves the vision engine unavailable.
"""
from __future__ import annotations

import base64
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from threading import Lock
from time import monotonic
from typing import Callable, Mapping, Protocol
import unicodedata
from urllib.parse import urlsplit

from .research_blob_store import configured_blob_store

PAGE_TIMEOUT_SECONDS = 60.0
LOW_CONFIDENCE_WORD = 60.0
_RENDER_LIMITS = ("--as=1073741824", "--cpu=60")
_TESSERACT_LIMITS = ("--as=2147483648", "--cpu=120")

VISION_INSTRUCTION = (
    "Transcribe all text on this page image verbatim, in reading order. Keep every number, "
    "unit and symbol exactly as printed. Render tables as one row per line with ' | ' between "
    "cells. Do not add commentary, headings of your own, summaries or translation."
)


@dataclass(frozen=True)
class OcrPageResult:
    text: str
    mean_confidence: float | None
    low_confidence_count: int
    engine: str
    engine_version: str
    langs: str
    usage: dict | None = None
    error: str | None = None
    cached: bool = False
    engines_tried: tuple[str, ...] = ()
    vision_calls: tuple[dict, ...] = ()


@dataclass
class OcrUsageHooks:
    """Run-owned accounting for vision-model OCR calls.

    ``reserve(call_id, max_tokens)`` admits one call (it raises to deny: budget, quota, cancellation) and
    ``settle(call_id, tokens)`` closes it (``None`` = usage unknown, nothing is invented). Without hooks the
    vision engine never runs. ``call_prefix`` makes call ids deterministic (operation id + source); the id of a
    page call is ``<prefix>:p<page>:<engine>``. ``events`` collects one record per vision call attempt.
    """
    reserve: Callable[[str, int], None]
    settle: Callable[[str, int | None], None]
    call_prefix: str = ""
    events: list = field(default_factory=list)
    lock: Lock = field(default_factory=Lock, repr=False)

    def for_source(self, operation_id: str, source_id: str) -> "OcrUsageHooks":
        """Same callbacks, event list and lock, with call ids scoped to one source."""
        return replace(self, call_prefix=f"{operation_id}:ocr:{source_id}")

    def call_id(self, page: int, engine: str) -> str:
        return f"{self.call_prefix}:p{page}:{engine}"


class OcrEngine(Protocol):
    name: str
    version: str

    def available(self) -> bool: ...

    def ocr(self, png: bytes, *, langs: str, timeout: float) -> OcrPageResult: ...


def _env_number(name: str, default: float, *, minimum: float = 0.0) -> float:
    try:
        value = float(os.environ.get(name, ""))
    except ValueError:
        return default
    return value if value >= minimum else default


def ocr_workers() -> int:
    return int(_env_number("FORESIGHTNINE_OCR_WORKERS", 2, minimum=1))


def vision_input_token_estimate() -> int:
    """Conservative admission bound for the image and instruction of one vision OCR call (not a run budget)."""
    return int(_env_number("FORESIGHTNINE_OCR_VISION_INPUT_TOKENS", 6000, minimum=1))


def on_demand_seconds() -> float:
    return _env_number("FORESIGHTNINE_OCR_ONDEMAND_SECONDS", 120.0, minimum=1.0)


def vision_engine_configured() -> bool:
    return VisionModelEngine.from_environment() is not None


def fallback_confidence() -> float:
    return _env_number("FORESIGHTNINE_OCR_FALLBACK_CONFIDENCE", 70.0, minimum=0.0)


# ---- rendering -----------------------------------------------------------------------------

class OcrRenderError(Exception):
    pass


def render_page_png(pdf_path: Path, page: int, *, dpi: int = 300, timeout: float = PAGE_TIMEOUT_SECONDS) -> bytes:
    """Render one 1-based page to a grayscale PNG."""
    renderer, limiter = shutil.which("pdftoppm"), shutil.which("prlimit")
    if not renderer or not limiter:
        raise OcrRenderError("pdf_renderer_unavailable")
    if timeout <= 0:
        raise OcrRenderError("render_timeout")
    try:
        result = subprocess.run(
            [limiter, *_RENDER_LIMITS, "--", renderer, "-f", str(page), "-l", str(page),
             "-r", str(int(dpi)), "-gray", "-png", str(pdf_path)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=timeout, check=False)
    except subprocess.TimeoutExpired as error:
        raise OcrRenderError("render_timeout") from error
    except OSError as error:
        raise OcrRenderError("pdf_renderer_unavailable") from error
    if result.returncode != 0 or not result.stdout.startswith(b"\x89PNG"):
        raise OcrRenderError("render_failed")
    return result.stdout


# ---- Tesseract -----------------------------------------------------------------------------

_CJK_RANGES = ((0x2E80, 0x303F), (0x3040, 0x30FF), (0x3100, 0x312F), (0x3400, 0x4DBF), (0x4E00, 0x9FFF),
               (0xAC00, 0xD7AF), (0x1100, 0x11FF), (0xF900, 0xFAFF), (0xFF00, 0xFFEF), (0x20000, 0x2FA1F))


def is_cjk_char(char: str) -> bool:
    code = ord(char)
    return any(low <= code <= high for low, high in _CJK_RANGES)


def _cjk_langs_requested(langs: str) -> bool:
    return any(part.startswith(("chi_", "jpn", "kor")) for part in langs.split("+"))


def text_from_tsv(tsv: str, *, cjk: bool) -> tuple[str, float | None, int]:
    """Rebuild text, mean word confidence (conf >= 0) and low-confidence word count from tesseract TSV."""
    lines: list[str] = []
    current: list[str] = []
    key: tuple[str, str, str] | None = None
    last_block_par: tuple[str, str] | None = None
    confidences: list[float] = []
    low = 0

    def flush() -> None:
        nonlocal current
        if current:
            lines.append("".join(current))
            current = []

    for row in tsv.splitlines()[1:]:
        cells = row.split("\t", 11)
        if len(cells) < 12 or cells[0] != "5":
            continue
        word = cells[11].strip()
        if not word:
            continue
        try:
            conf = float(cells[10])
        except ValueError:
            conf = -1.0
        if conf >= 0:
            confidences.append(conf)
            if conf < LOW_CONFIDENCE_WORD:
                low += 1
        line_key = (cells[2], cells[3], cells[4])
        if line_key != key:
            flush()
            if last_block_par is not None and (cells[2], cells[3]) != last_block_par:
                lines.append("")
            key = line_key
            last_block_par = (cells[2], cells[3])
        if current and not (cjk and is_cjk_char(current[-1][-1]) and is_cjk_char(word[0])):
            current.append(" ")
        current.append(word)
    flush()
    mean = sum(confidences) / len(confidences) if confidences else None
    return "\n".join(lines), mean, low


class TesseractEngine:
    name = "tesseract"

    def __init__(self) -> None:
        self._binary = shutil.which("tesseract")
        self._limiter = shutil.which("prlimit")
        self._version: str | None = None
        self._langs: frozenset[str] | None = None

    def available(self) -> bool:
        return bool(self._binary and self._limiter)

    @property
    def version(self) -> str:
        if self._version is None:
            self._version = "unavailable"
            if self.available():
                try:
                    out = subprocess.run([self._binary, "--version"], stdin=subprocess.DEVNULL, capture_output=True,
                                         timeout=10, check=False)
                    match = re.search(rb"tesseract\s+(\S+)", out.stdout + out.stderr)
                    if match:
                        self._version = match.group(1).decode("ascii", "replace")
                except (subprocess.TimeoutExpired, OSError):
                    pass
        return self._version

    def installed_languages(self) -> frozenset[str]:
        if self._langs is None:
            found: set[str] = set()
            if self.available():
                try:
                    out = subprocess.run([self._binary, "--list-langs"], stdin=subprocess.DEVNULL,
                                         capture_output=True, timeout=10, check=False)
                    for line in out.stdout.decode("utf-8", "replace").splitlines()[1:]:
                        if line.strip():
                            found.add(line.strip())
                except (subprocess.TimeoutExpired, OSError):
                    pass
            self._langs = frozenset(found)
        return self._langs

    def ocr(self, png: bytes, *, langs: str, timeout: float) -> OcrPageResult:
        def failure(code: str) -> OcrPageResult:
            return OcrPageResult("", None, 0, self.name, self.version, langs, error=code)

        if not self.available():
            return failure("engine_unavailable")
        if timeout <= 0:
            return failure("timeout")
        env = {**os.environ, "OMP_THREAD_LIMIT": "1"}
        try:
            result = subprocess.run(
                [self._limiter, *_TESSERACT_LIMITS, "--", self._binary, "stdin", "stdout",
                 "-l", langs, "--psm", "3", "tsv"],
                input=png, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=timeout, check=False, env=env)
        except subprocess.TimeoutExpired:
            return failure("timeout")
        except OSError:
            return failure("engine_unavailable")
        if result.returncode != 0:
            return failure("engine_failed")
        text, mean, low = text_from_tsv(result.stdout.decode("utf-8", "replace"), cjk=_cjk_langs_requested(langs))
        return OcrPageResult(text, mean, low, self.name, self.version, langs)


# ---- vision model --------------------------------------------------------------------------

class VisionModelEngine:
    """Optional OCR through a vision-capable model over anthropic_messages or openai_compatible."""

    name = "vision_model"
    _SUPPORTED_PROTOCOLS = frozenset({"anthropic_messages", "openai_compatible"})

    def __init__(self, *, base_url: str, model: str, api_key: str, allowlist: str, max_output_tokens: int,
                 anthropic_version: str = "", auth_scheme: str = "x-api-key",
                 protocol: str = "anthropic_messages",
                 transport: Callable[..., object] | None = None):
        from .openai_compatible import OpenAICompatibleSettings
        from .phase3 import ProviderFailure
        self._ready = False
        self.model = model
        self.version = model
        self.max_output_tokens = max_output_tokens
        self.anthropic_version = anthropic_version
        self.auth_scheme = auth_scheme if auth_scheme in {"x-api-key", "bearer"} else "x-api-key"
        self.protocol = (protocol or "anthropic_messages").strip()
        self._transport = transport
        self._api_key = api_key
        self._settings = None
        if self.protocol not in self._SUPPORTED_PROTOCOLS:
            return
        try:
            self._settings = OpenAICompatibleSettings.from_values(
                base_url=base_url, model=model, api_key=api_key, allowlist=allowlist)
        except ProviderFailure:
            return
        if self.protocol == "anthropic_messages":
            valid_version = bool(anthropic_version and not any(c in anthropic_version for c in "\r\n"))
            self._ready = bool(api_key and valid_version and max_output_tokens > 0)
        else:
            self._ready = bool(api_key and max_output_tokens > 0)

    @classmethod
    def from_environment(cls, *, transport: Callable[..., object] | None = None) -> "VisionModelEngine | None":
        env = os.environ.get
        protocol = env("FORESIGHTNINE_OCR_VISION_PROTOCOL", "").strip()
        if protocol not in cls._SUPPORTED_PROTOCOLS:
            return None
        try:
            max_tokens = int(env("FORESIGHTNINE_OCR_VISION_MAX_OUTPUT_TOKENS", "").strip())
        except ValueError:
            return None
        engine = cls(
            base_url=env("FORESIGHTNINE_OCR_VISION_BASE_URL", ""), model=env("FORESIGHTNINE_OCR_VISION_MODEL", ""),
            api_key=env("FORESIGHTNINE_OCR_VISION_API_KEY", "").strip(),
            allowlist=env("FORESIGHTNINE_OCR_VISION_ALLOWED_HOSTS", "") or env("FORESIGHTNINE_RESEARCH_LLM_ALLOWED_HOSTS", ""),
            max_output_tokens=max_tokens,
            anthropic_version=(env("FORESIGHTNINE_OCR_VISION_ANTHROPIC_VERSION", "") or env("FORESIGHTNINE_MESSAGES_VERSION", "")).strip(),
            auth_scheme=env("FORESIGHTNINE_OCR_VISION_AUTH_SCHEME", "x-api-key").strip() or "x-api-key",
            protocol=protocol,
            transport=transport)
        return engine if engine.available() else None

    def available(self) -> bool:
        return self._ready

    def _build_request(self, png: bytes) -> tuple[str, dict[str, str], bytes]:
        assert self._settings is not None
        encoded = base64.b64encode(png).decode("ascii")
        if self.protocol == "openai_compatible":
            from .openai_compatible import _endpoint as _openai_endpoint
            url = _openai_endpoint(self._settings.base_url)
            body = json.dumps({
                "model": self.model,
                "max_tokens": self.max_output_tokens,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}},
                        {"type": "text", "text": VISION_INSTRUCTION},
                    ],
                }],
            }, sort_keys=True, separators=(",", ":")).encode()
            headers = {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": "Bearer " + self._api_key,
            }
            return url, headers, body
        from .anthropic_messages import _endpoint as _anthropic_endpoint
        url = _anthropic_endpoint(self._settings.base_url)
        body = json.dumps({
            "model": self.model, "max_tokens": self.max_output_tokens,
            "messages": [{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": encoded}},
                {"type": "text", "text": VISION_INSTRUCTION}]}],
        }, separators=(",", ":")).encode()
        headers = {"Accept": "application/json", "Content-Type": "application/json",
                   "anthropic-version": self.anthropic_version}
        if self.auth_scheme == "bearer":
            headers["Authorization"] = "Bearer " + self._api_key
        else:
            headers["x-api-key"] = self._api_key
        return url, headers, body

    def _parse_payload(self, payload: object) -> tuple[str, str | None, dict | None]:
        usage = _usage(payload.get("usage") if isinstance(payload, dict) else None)
        if not isinstance(payload, dict):
            return "", "response_invalid", usage
        if self.protocol == "openai_compatible":
            choices = payload.get("choices")
            if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                return "", "response_invalid", usage
            choice = choices[0]
            message = choice.get("message")
            if not isinstance(message, dict):
                return "", "response_invalid", usage
            content = message.get("content")
            if isinstance(content, str):
                text = content.strip()
            elif isinstance(content, list):
                text = "\n".join(
                    b["text"] for b in content
                    if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
                ).strip()
            elif content is None:
                text = ""
            else:
                return "", "response_invalid", usage
            if choice.get("finish_reason") == "length":
                return "", "output_truncated", usage
            if not text:
                return "", "empty_response", usage
            return text, None, usage
        blocks = payload.get("content")
        if not isinstance(blocks, list):
            return "", "response_invalid", usage
        text = "\n".join(b["text"] for b in blocks
                         if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)).strip()
        if payload.get("stop_reason") == "max_tokens":
            return "", "output_truncated", usage
        if not text:
            return "", "empty_response", usage
        return text, None, usage

    def ocr(self, png: bytes, *, langs: str, timeout: float) -> OcrPageResult:
        def failure(code: str, usage: dict | None = None) -> OcrPageResult:
            return OcrPageResult("", None, 0, self.name, self.version, langs, usage=usage, error=code)

        if not self._ready or self._settings is None:
            return failure("engine_unavailable")
        if timeout <= 0:
            return failure("timeout")
        from .openai_compatible import _TransportFailure, _TransportTimeout, _default_binary_transport
        url, headers, body = self._build_request(png)
        send = self._transport or _default_binary_transport
        try:
            response = send("POST", url, headers, body, timeout)
        except _TransportTimeout:
            return failure("timeout")
        except (_TransportFailure, OSError):
            return failure("transport_failed")
        if not 200 <= getattr(response, "status_code", 0) < 300:
            return failure(f"http_{getattr(response, 'status_code', 0)}")
        try:
            payload = json.loads(response.body.decode("utf-8"))
        except (ValueError, AttributeError):
            return failure("response_invalid")
        text, error_code, usage = self._parse_payload(payload)
        if error_code is not None:
            return failure(error_code, usage)
        return OcrPageResult(text, None, 0, self.name, self.version, langs, usage=usage)


def _usage(raw: object) -> dict | None:
    if not isinstance(raw, Mapping):
        return None
    out: dict[str, int] = {}
    for target, candidates in (("input_tokens", ("input_tokens", "prompt_tokens")),
                               ("output_tokens", ("output_tokens", "completion_tokens"))):
        for key in candidates:
            value = raw.get(key)
            if type(value) is int and value >= 0:
                out[target] = value
                break
    return out or None


# ---- cache ---------------------------------------------------------------------------------

class OcrCache:
    """JSON files under ``<blob store root>/ocr-cache`` when a store is configured, else a bounded LRU."""

    def __init__(self, directory: Path | None = None, *, max_memory_entries: int = 512):
        self.directory = directory
        self._memory: OrderedDict[str, dict] = OrderedDict()
        self._max = max_memory_entries
        self._lock = Lock()

    @staticmethod
    def key(png_sha256: str, engine: str, version: str, langs: str) -> str:
        return sha256("\0".join((png_sha256, engine, version, langs)).encode()).hexdigest()

    def get(self, key: str) -> dict | None:
        if self.directory is not None:
            try:
                data = json.loads((self.directory / f"{key}.json").read_text(encoding="utf-8"))
                return data if isinstance(data, dict) else None
            except (OSError, ValueError):
                return None
        with self._lock:
            value = self._memory.get(key)
            if value is not None:
                self._memory.move_to_end(key)
            return value

    def put(self, key: str, value: dict) -> None:
        if self.directory is not None:
            try:
                self.directory.mkdir(parents=True, exist_ok=True)
                temp = self.directory / f".{key}.{os.getpid()}.{id(value)}.tmp"
                temp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
                os.replace(temp, self.directory / f"{key}.json")
            except OSError:
                pass
            return
        with self._lock:
            self._memory[key] = value
            self._memory.move_to_end(key)
            while len(self._memory) > self._max:
                self._memory.popitem(last=False)


_PROCESS_CACHE = OcrCache()


def default_cache() -> OcrCache:
    store = configured_blob_store()
    return OcrCache(store.root / "ocr-cache") if store is not None else _PROCESS_CACHE


# ---- language choice -----------------------------------------------------------------------

_TLD_LANGS = {"cn": ("chi_sim",), "hk": ("chi_tra",), "tw": ("chi_tra",), "jp": ("jpn",), "kr": ("kor",)}
_shared_tesseract: TesseractEngine | None = None


def _tesseract() -> TesseractEngine:
    global _shared_tesseract
    if _shared_tesseract is None:
        _shared_tesseract = TesseractEngine()
    return _shared_tesseract


def _scripts(text: str) -> set[str]:
    found: set[str] = set()
    for char in text:
        code = ord(char)
        if 0x3040 <= code <= 0x30FF:
            found.add("kana")
        elif 0xAC00 <= code <= 0xD7AF or 0x1100 <= code <= 0x11FF:
            found.add("hangul")
        elif 0x4E00 <= code <= 0x9FFF or 0x3400 <= code <= 0x4DBF:
            found.add("han")
    return found


def choose_ocr_languages(*, title: str | None, url: str | None, sample_text: str | None,
                         installed: frozenset[str] | None = None) -> str:
    """Tesseract language string ('a+b'), always including eng, restricted to installed packs."""
    wanted: list[str] = []
    scripts = _scripts(f"{title or ''} {sample_text or ''}")
    if "kana" in scripts:
        wanted.append("jpn")
    if "hangul" in scripts:
        wanted.append("kor")
    if "han" in scripts and "kana" not in scripts:
        wanted.extend(["chi_sim", "chi_tra"])
    host = (urlsplit(url or "").hostname or "").lower().rstrip(".")
    if host:
        wanted.extend(_TLD_LANGS.get(host.rsplit(".", 1)[-1], ()))
    defaults = [part for part in os.environ.get("FORESIGHTNINE_OCR_DEFAULT_LANGS", "").strip().split("+") if part]
    wanted.extend(defaults or ["eng"])
    if "eng" not in wanted:
        wanted.append("eng")
    available = installed if installed is not None else _tesseract().installed_languages()
    chosen: list[str] = []
    for lang in wanted:
        if lang not in chosen and (not available or lang in available) and re.fullmatch(r"[A-Za-z0-9_]+", lang):
            chosen.append(lang)
    return "+".join(chosen) if chosen else "eng"


# ---- policy --------------------------------------------------------------------------------

_NUMERIC_TOKEN = re.compile(r"^[\(\-−$€£¥]?[\d][\d,. ]*%?\)?$")


def looks_tabular_or_empty(text: str) -> bool:
    stripped = [c for c in text if not c.isspace()]
    if len(stripped) < 3:
        return True
    lines = [line for line in text.splitlines() if line.strip()]
    numeric_rows = sum(1 for line in lines
                       if sum(1 for tok in line.split() if _NUMERIC_TOKEN.match(tok)) >= 3)
    return numeric_rows >= 3 and numeric_rows / len(lines) >= 0.3


def default_engines() -> list[OcrEngine]:
    engines: list[OcrEngine] = [_tesseract()]
    vision = VisionModelEngine.from_environment()
    if vision is not None:
        engines.append(vision)
    return [engine for engine in engines if engine.available()]


def _cache_get(cache: OcrCache, key: str, engine: OcrEngine, langs: str) -> OcrPageResult | None:
    data = cache.get(key)
    if not data or not isinstance(data.get("text"), str):
        return None
    mean = data.get("mean_confidence")
    return OcrPageResult(data["text"], mean if isinstance(mean, (int, float)) else None,
                         int(data.get("low_confidence_count") or 0), engine.name, engine.version, langs,
                         usage=None, cached=True, engines_tried=(engine.name,))


def _run_engine(engine: OcrEngine, png: bytes, png_hash: str, langs: str, timeout: float,
                cache: OcrCache) -> OcrPageResult:
    key = OcrCache.key(png_hash, engine.name, engine.version, langs)
    hit = _cache_get(cache, key, engine, langs)
    if hit is not None:
        return hit
    result = engine.ocr(png, langs=langs, timeout=timeout)
    if result.error is None:
        cache.put(key, {"text": result.text, "mean_confidence": result.mean_confidence,
                        "low_confidence_count": result.low_confidence_count})
    return replace(result, engines_tried=(engine.name,))


def _needs_fallback(result: OcrPageResult) -> bool:
    if result.error is not None:
        return True
    if result.mean_confidence is not None and result.mean_confidence < fallback_confidence():
        return True
    return looks_tabular_or_empty(result.text)


def _vision_total_tokens(usage: dict | None) -> int | None:
    if not usage:
        return None
    values = [usage[key] for key in ("input_tokens", "output_tokens") if type(usage.get(key)) is int]
    return sum(values) if values else None


def _denial_code(error: BaseException) -> str:
    code = getattr(error, "code", None) or getattr(error, "error_code", None)
    if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_]{1,80}", code):
        return code
    return "already_attempted" if isinstance(error, ValueError) else "denied"


def _run_vision(engine: OcrEngine, png: bytes, png_hash: str, langs: str, timeout: float, cache: OcrCache,
                usage: OcrUsageHooks, page: int) -> OcrPageResult:
    """One billed vision call: cache hit (free) or reserve -> single request -> settle. Never re-issued here."""
    key = OcrCache.key(png_hash, engine.name, engine.version, langs)
    hit = _cache_get(cache, key, engine, langs)
    if hit is not None:
        return hit
    call_id = usage.call_id(page, engine.name)
    event = {"kind": "ocr_vision", "call_id": call_id, "page": page, "engine": engine.name, "model": engine.version}
    max_tokens = vision_input_token_estimate() + int(getattr(engine, "max_output_tokens", 0) or 0)
    try:
        with usage.lock:
            usage.reserve(call_id, max_tokens)
    except Exception as error:  # budget, quota, cancellation or a repeated call id: the page stays needs_ocr
        event.update(status="denied", external_state="not_started", reason=_denial_code(error))
        usage.events.append(event)
        return OcrPageResult("", None, 0, engine.name, engine.version, langs, error="vision_denied",
                             engines_tried=(engine.name,), vision_calls=(event,))
    try:
        result = engine.ocr(png, langs=langs, timeout=timeout)
    except Exception:
        result = OcrPageResult("", None, 0, engine.name, engine.version, langs, error="engine_failed")
    tokens = _vision_total_tokens(result.usage)
    settle_error: str | None = None
    try:
        with usage.lock:
            usage.settle(call_id, tokens)
    except Exception as error:
        settle_error = _denial_code(error)
    event.update(status="settled" if tokens is not None else "unknown",
                 external_state="returned" if result.usage else "unknown",
                 input_tokens=(result.usage or {}).get("input_tokens"), output_tokens=(result.usage or {}).get("output_tokens"),
                 error=result.error)
    if settle_error is not None:
        event["settlement_error"] = settle_error
    usage.events.append(event)
    if result.error is None:
        cache.put(key, {"text": result.text, "mean_confidence": result.mean_confidence,
                        "low_confidence_count": result.low_confidence_count})
    return replace(result, engines_tried=(engine.name,), vision_calls=(event,))


def _ocr_one(pdf_path: Path, page: int, langs: str, deadline: float, engines: list[OcrEngine],
             cache: OcrCache, usage: OcrUsageHooks | None = None) -> OcrPageResult | None:
    budget = min(PAGE_TIMEOUT_SECONDS, deadline - monotonic())
    if budget <= 0 or not engines:
        return None
    try:
        png = render_page_png(pdf_path, page, timeout=budget)
    except OcrRenderError as error:
        return OcrPageResult("", None, 0, "render", "", langs, error=str(error))
    png_hash = sha256(png).hexdigest()
    local = [e for e in engines if e.name != VisionModelEngine.name]
    vision = [e for e in engines if e.name == VisionModelEngine.name] if usage is not None else []
    best: OcrPageResult | None = None
    tried: list[str] = []
    vision_calls: list[dict] = []
    for engine in local:
        remaining = min(PAGE_TIMEOUT_SECONDS, deadline - monotonic())
        if remaining <= 0:
            break
        best = _run_engine(engine, png, png_hash, langs, remaining, cache)
        tried.append(engine.name)
        if best.error is None:
            break
    if vision and usage is not None and (best is None or _needs_fallback(best)):
        remaining = min(PAGE_TIMEOUT_SECONDS, deadline - monotonic())
        if remaining > 0:
            llm = _run_vision(vision[0], png, png_hash, langs, remaining, cache, usage, page)
            tried.append(vision[0].name)
            vision_calls.extend(llm.vision_calls)
            if llm.error is None:
                best = llm
            elif best is None:
                best = llm
            elif llm.usage:
                # Keep the usable local text but still report tokens the failed call consumed.
                best = replace(best, usage=llm.usage)
    if best is None:
        return None
    return replace(best, engines_tried=tuple(tried), vision_calls=tuple(vision_calls))


def ocr_pdf_pages(pdf_path: Path, pages: list[int], *, langs: str, deadline: float, workers: int | None = None,
                  engines: list[OcrEngine] | None = None, cache: OcrCache | None = None,
                  usage: OcrUsageHooks | None = None) -> dict[int, OcrPageResult]:
    """OCR the given 1-based pages in priority order; pages not reached before the deadline are omitted.

    The vision engine runs only when ``usage`` hooks are supplied (each call is reserved and settled through
    them); without hooks only local engines run.
    """
    active = list(engines) if engines is not None else default_engines()
    active = [e for e in active if e.available() and (usage is not None or e.name != VisionModelEngine.name)]
    if not pages or not active:
        return {}
    cache = cache if cache is not None else default_cache()
    count = max(1, workers if workers is not None else ocr_workers())
    results: dict[int, OcrPageResult] = {}
    with ThreadPoolExecutor(max_workers=count, thread_name_prefix="foresight-ocr") as pool:
        futures = [(page, pool.submit(_ocr_one, Path(pdf_path), page, langs, deadline, active, cache, usage))
                   for page in pages]
        for page, future in futures:
            try:
                result = future.result()
            except Exception:  # one page must not fail the others
                continue
            if result is not None:
                results[page] = result
    return results


# ---- OCR of a stored source (on demand) -------------------------------------------------------

MAX_ONDEMAND_PAGES = 20


@dataclass
class OcrOutcome:
    """Result of an on-demand OCR request against one stored source version."""
    status: str  # completed | partial | nothing_to_do | ocr_unavailable | failed
    source: object | None = None  # the new source version (a ResearchSource) when text was added
    reason: str | None = None
    message: str | None = None
    pages: list = field(default_factory=list)
    events: list = field(default_factory=list)
    usage: dict | None = None


@dataclass
class SourceOcrResult:
    status: str
    reason: str | None
    message: str | None
    pages: list
    text: str | None = None
    document_pages: dict | None = None
    usage: dict | None = None


def _original_pdf(source, directory: str) -> Path | None:
    document_pages = (source.provenance or {}).get("document_pages") or {}
    raw_b64 = getattr(source, "original_pdf_base64", None)
    if raw_b64:
        try:
            raw = base64.b64decode(raw_b64, validate=True)
        except (ValueError, TypeError):
            raw = b""
        if raw.startswith(b"%PDF-"):
            path = Path(directory) / "source.pdf"
            path.write_bytes(raw)
            return path
    digest = document_pages.get("original_blob_sha256")
    store = configured_blob_store()
    if digest and store is not None:
        return store.path_for(digest)
    return None


def run_source_ocr(source, pages: list[int], *, usage: OcrUsageHooks | None = None,
                   engines: list[OcrEngine] | None = None, cache: OcrCache | None = None,
                   seconds: float | None = None) -> SourceOcrResult:
    """OCR the ``needs_ocr`` pages among ``pages`` of ``source`` and merge them into a new text + page map.

    Nothing is persisted here; the caller stores the result as a new source version.
    """
    from .research_documents import merge_ocr_into_document

    document_pages = (source.provenance or {}).get("document_pages")
    page_list = document_pages.get("pages") if isinstance(document_pages, dict) else None
    if not isinstance(page_list, list) or not page_list:
        return SourceOcrResult("failed", "no_page_map", "This source has no page map.", [])
    requested = list(dict.fromkeys(p for p in pages if isinstance(p, int) and not isinstance(p, bool)))[:MAX_ONDEMAND_PAGES]
    report: list[dict] = []
    targets: list[int] = []
    for number in requested:
        if not 1 <= number <= len(page_list):
            report.append({"page": number, "status": "out_of_range"})
            continue
        status = page_list[number - 1][2]
        if status == "needs_ocr":
            targets.append(number)
        else:
            report.append({"page": number, "status": status,
                           **({"note": "already readable"} if status in {"text", "ocr"} else
                              {"note": "page was not extracted and cannot be OCR'd from this source"})})
    if not targets:
        return SourceOcrResult("nothing_to_do", "no_needs_ocr_pages",
                               "None of the requested pages needs OCR.", report)
    active = list(engines) if engines is not None else default_engines()
    active = [e for e in active if e.available() and (usage is not None or e.name != VisionModelEngine.name)]
    pending_rows = report + [{"page": n, "status": "needs_ocr"} for n in targets]
    if not active:
        return SourceOcrResult("ocr_unavailable", "no_ocr_engine", "No OCR engine is available.", pending_rows)
    with tempfile.TemporaryDirectory(prefix="foresight-ocr-demand-") as directory:
        path = _original_pdf(source, directory)
        if path is None:
            return SourceOcrResult("ocr_unavailable", "original_not_retained",
                                   "The original PDF was not retained for this source, so its pages cannot be OCR'd.",
                                   pending_rows)
        langs = choose_ocr_languages(title=source.title, url=str(source.url) if source.url else None,
                                     sample_text=source.text[:4000])
        deadline = monotonic() + (seconds if seconds is not None else on_demand_seconds())
        results = ocr_pdf_pages(path, targets, langs=langs, deadline=deadline, engines=active, cache=cache, usage=usage)
    text, merged = merge_ocr_into_document(source.text, document_pages, results)
    changed = 0
    for number in targets:
        entry = merged["pages"][number - 1]
        row: dict = {"page": number, "status": entry[2]}
        result = results.get(number)
        if entry[2] == "ocr":
            changed += 1
            meta = (merged.get("ocr") or {}).get(str(number)) or {}
            row.update(engine=meta.get("engine"), mean_confidence=meta.get("mean_confidence"),
                       low_confidence_count=meta.get("low_confidence_count"))
        elif result is None:
            row["note"] = "not reached before the OCR deadline"
        else:
            row["error"] = result.error or "empty_text"
        report.append(row)
    report.sort(key=lambda r: r["page"])
    usage_totals = {
        "input_tokens": sum((r.usage or {}).get("input_tokens", 0) for r in results.values()),
        "output_tokens": sum((r.usage or {}).get("output_tokens", 0) for r in results.values())}
    if not changed:
        return SourceOcrResult("failed", "ocr_no_text", "OCR produced no usable text for the requested pages.",
                               report, usage=usage_totals)
    status = "completed" if changed == len(targets) else "partial"
    return SourceOcrResult(status, None, None, report, text=text, document_pages=merged, usage=usage_totals)


def build_ocr_pages_callback(*, operation_id: str, persist_version: Callable[..., object],
                             usage: OcrUsageHooks | None = None, engines: list[OcrEngine] | None = None,
                             cache: OcrCache | None = None, seconds: float | None = None) -> Callable[[object, list], OcrOutcome]:
    """Callback for the research runtime: OCR pages of a source and store the result as a new source version.

    ``persist_version(old_source, new_source_id, text, provenance)`` returns the stored new ResearchSource.
    """
    def ocr_pages(source, pages: list[int]) -> OcrOutcome:
        hooks = usage.for_source(operation_id, source.source_id) if usage is not None else None
        first_event = len(hooks.events) if hooks is not None else 0
        result = run_source_ocr(source, pages, usage=hooks, engines=engines, cache=cache, seconds=seconds)
        events = list(hooks.events[first_event:]) if hooks is not None else []
        if result.text is None or result.document_pages is None:
            return OcrOutcome(result.status, None, result.reason, result.message, result.pages, events, result.usage)
        digest = sha256(json.dumps([source.source_id, sorted(r["page"] for r in result.pages
                                                          if r.get("status") == "ocr")]).encode()).hexdigest()
        new_id = "source_" + sha256(f"{operation_id}:ocr-version:{digest}".encode()).hexdigest()[:32]
        provenance = {**(source.provenance or {}),
                      "document_pages": {**result.document_pages},
                      "supersedes_source_id": source.source_id}
        try:
            new_source = persist_version(source, new_id, result.text, provenance)
        except Exception as error:  # nothing stored: the old version stays authoritative
            return OcrOutcome("failed", None, "version_not_stored",
                              f"OCR text was produced but the new source version could not be stored ({type(error).__name__}).",
                              result.pages, events, result.usage)
        return OcrOutcome(result.status, new_source, result.reason, result.message, result.pages, events, result.usage)
    return ocr_pages
