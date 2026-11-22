import base64
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
from time import monotonic

import pytest

import app.research_fetch as research_fetch
import app.research_ocr as research_ocr
from app.openai_compatible import BinaryHttpResponse
from app.research_blob_store import DocumentBlobStore, configured_blob_store
from app.research_documents import merge_ocr_into_document, page_for_offset
from app.research_fetch import FetchFailure, fetch_source_document
from app.research_ocr import (
    OcrCache,
    OcrUsageHooks,
    OcrPageResult,
    TesseractEngine,
    VisionModelEngine,
    choose_ocr_languages,
    ocr_pdf_pages,
    render_page_png,
    text_from_tsv,
)

from .test_research_large_documents import (  # noqa: F401  (fixtures)
    build_pdf,
    local_transport,
    needs_poppler,
    req,
    server,
)

needs_tesseract = pytest.mark.skipif(
    not (shutil.which("tesseract") and shutil.which("pdftoppm") and shutil.which("prlimit")),
    reason="tesseract, poppler-utils and prlimit are required",
)


# ---- synthetic scanned PDFs -------------------------------------------------------------------

def _jpeg_components(data: bytes) -> int:
    index = 2
    while index < len(data):
        marker = data[index + 1]
        if marker in (0xC0, 0xC1, 0xC2):
            return data[index + 9]
        index += 2 + int.from_bytes(data[index + 2:index + 4], "big")
    raise AssertionError("no SOF marker")


def _jpeg_size(data: bytes) -> tuple[int, int]:
    index = 2
    while index < len(data):
        marker = data[index + 1]
        length = int.from_bytes(data[index + 2:index + 4], "big")
        if marker in (0xC0, 0xC1, 0xC2):
            return int.from_bytes(data[index + 7:index + 9], "big"), int.from_bytes(data[index + 5:index + 7], "big")
        index += 2 + length
    raise AssertionError("no SOF marker")


def scanned_page_jpeg(tmp_path: Path, text: str, name: str = "scan") -> bytes:
    """Rasterise a vector page to a gray JPEG, i.e. a page that only exists as an image."""
    source = tmp_path / f"{name}.pdf"
    source.write_bytes(build_pdf([text]).replace(b"BT /F1 12 Tf 72 700 Td", b"BT /F1 30 Tf 40 600 Td"))
    prefix = tmp_path / name
    subprocess.run(["pdftoppm", "-r", "200", "-gray", "-jpeg", "-singlefile", str(source), str(prefix)], check=True)
    return (tmp_path / f"{name}.jpg").read_bytes()


def build_mixed_pdf(pages: list, *, padding: int = 0) -> bytes:
    """Pages: str = vector text, bytes = JPEG image-only page, None = blank page."""
    objects: list[bytes] = [b"<< /Type /Catalog /Pages 2 0 R >>", b""]
    kids = []
    for page in pages:
        page_id = len(objects) + 1
        kids.append(f"{page_id} 0 R")
        content_id = page_id + 1
        resources = "/Font << /F1 << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> >>"
        if isinstance(page, bytes):
            width, height = _jpeg_size(page)
            box = f"[0 0 {width * 72 // 200} {height * 72 // 200}]"
            image_id = content_id + 1
            resources += f" /XObject << /Im0 {image_id} 0 R >>"
            stream = f"q {width * 72 // 200} 0 0 {height * 72 // 200} 0 0 cm /Im0 Do Q".encode()
        else:
            box = "[0 0 612 792]"
            stream = b"" if page is None else f"BT /F1 12 Tf 72 700 Td ({page}) Tj ET".encode()
        objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox {box} /Contents {content_id} 0 R /Resources << {resources} >> >>".encode())
        objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        if isinstance(page, bytes):
            objects.append((f"<< /Type /XObject /Subtype /Image /Width {width} /Height {height} /ColorSpace /{'DeviceGray' if _jpeg_components(page) == 1 else 'DeviceRGB'} "
                            f"/BitsPerComponent 8 /Filter /DCTDecode /Length {len(page)} >>\nstream\n").encode()
                           + page + b"\nendstream")
    objects[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>".encode()
    if padding:
        objects.append(b"<< /Length %d >>\nstream\n" % padding + b"\x00" * padding + b"\nendstream")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


@pytest.fixture
def scan_jpeg(tmp_path):
    return scanned_page_jpeg(tmp_path, "Quarterly revenue 4215 million")


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    monkeypatch.delenv("FORESIGHTNINE_DOCUMENT_STORE_DIR", raising=False)
    for name in list(os.environ):
        if name.startswith("FORESIGHTNINE_OCR_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(research_ocr, "_PROCESS_CACHE", OcrCache())


class FakeEngine:
    def __init__(self, name="tesseract", text="Revenue 100", confidence=95.0, usage=None, error=None, version="1"):
        self.name, self.version = name, version
        self._text, self._conf, self._usage, self._error = text, confidence, usage, error
        self.calls = 0

    def available(self):
        return True

    def ocr(self, png, *, langs, timeout):
        self.calls += 1
        return OcrPageResult(self._text, self._conf, 0, self.name, self.version, langs, usage=self._usage, error=self._error)


def recording_hooks(prefix="op:ocr:src"):
    log = []
    hooks = OcrUsageHooks(lambda call_id, max_tokens: log.append(("reserve", call_id, max_tokens)),
                          lambda call_id, tokens: log.append(("settle", call_id, tokens)), call_prefix=prefix)
    return hooks, log


# ---- blob store ---------------------------------------------------------------------------------

def test_blob_store_content_addressed_atomic_and_traversal_safe(tmp_path):
    store = DocumentBlobStore(tmp_path / "store")
    source = tmp_path / "a.bin"
    source.write_bytes(b"hello blob")
    digest = store.put_file(source)
    assert digest == store.put_bytes(b"hello blob")
    assert store.path_for(digest).read_bytes() == b"hello blob"
    assert not [p for p in (tmp_path / "store").iterdir() if p.name.startswith(".incoming-")]
    for bad in ("../x", "A" * 64, digest[:-1], digest + "0", "", "..%2f" + "a" * 59):
        assert store.path_for(bad) is None
    with pytest.raises(ValueError):
        store._target("../../etc/passwd")
    assert store.path_for("0" * 64) is None


def test_blob_store_failed_write_leaves_no_partial_blob(tmp_path):
    store = DocumentBlobStore(tmp_path / "store")
    with pytest.raises(FileNotFoundError):
        store.put_file(tmp_path / "missing.bin")
    assert [p for p in (tmp_path / "store").rglob("*") if p.is_file()] == []


def test_configured_blob_store_env(monkeypatch, tmp_path):
    assert configured_blob_store() is None
    monkeypatch.setenv("FORESIGHTNINE_DOCUMENT_STORE_DIR", str(tmp_path))
    assert configured_blob_store().root == tmp_path


# ---- tesseract ----------------------------------------------------------------------------------

def tsv(rows):
    header = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext"
    return "\n".join([header] + [f"5\t1\t{b}\t{p}\t{l}\t{w}\t0\t0\t1\t1\t{c}\t{t}" for b, p, l, w, c, t in rows])


def test_tsv_rebuilds_lines_confidence_and_low_count():
    text, mean, low = text_from_tsv(tsv([(1, 1, 1, 1, 90, "Total"), (1, 1, 1, 2, 30, "revenue"),
                                         (1, 1, 2, 1, 80, "next"), (1, 1, 2, 2, -1, "x"),
                                         (2, 1, 1, 1, 100, "Para")]), cjk=False)
    assert text == "Total revenue\nnext x\n\nPara"
    assert mean == pytest.approx((90 + 30 + 80 + 100) / 4) and low == 1


def test_tsv_cjk_spacing_rule():
    rows = [(1, 1, 1, 1, 90, "营业"), (1, 1, 1, 2, 90, "收入"), (1, 1, 1, 3, 90, "Revenue"), (1, 1, 1, 4, 90, "增长"),
            (1, 1, 1, 5, 90, "12%")]
    text, _, _ = text_from_tsv(tsv(rows), cjk=True)
    assert text == "营业收入 Revenue 增长 12%"
    latin, _, _ = text_from_tsv(tsv(rows[:2]), cjk=False)
    assert latin == "营业 收入"


def test_language_chooser():
    inst = frozenset({"eng", "chi_sim", "chi_tra", "jpn", "kor", "osd"})
    assert choose_ocr_languages(title="Annual report", url="https://a.example.com/x.pdf", sample_text=None, installed=inst) == "eng"
    assert choose_ocr_languages(title="年度报告", url=None, sample_text=None, installed=inst) == "chi_sim+chi_tra+eng"
    assert choose_ocr_languages(title="決算短信", url="https://x.co.jp/a.pdf", sample_text="の売上高", installed=inst) == "jpn+eng"
    assert choose_ocr_languages(title="보고서", url=None, sample_text=None, installed=inst) == "kor+eng"
    assert choose_ocr_languages(title="x", url="https://ir.example.com.hk/a.pdf", sample_text=None, installed=inst) == "chi_tra+eng"
    assert choose_ocr_languages(title="年度报告", url=None, sample_text=None, installed=frozenset({"eng"})) == "eng"


def test_language_chooser_default_env(monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_OCR_DEFAULT_LANGS", "deu")
    inst = frozenset({"eng", "deu"})
    assert choose_ocr_languages(title="Bericht", url=None, sample_text=None, installed=inst) == "deu+eng"


@needs_tesseract
def test_tesseract_reads_rendered_image_only_page(tmp_path, scan_jpeg):
    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(build_mixed_pdf([scan_jpeg]))
    png = render_page_png(pdf, 1, timeout=30)
    assert png.startswith(b"\x89PNG")
    engine = TesseractEngine()
    assert engine.available() and engine.version[0].isdigit()
    result = engine.ocr(png, langs="eng", timeout=60)
    assert result.error is None and result.engine == "tesseract" and result.engine_version == engine.version
    assert "revenue" in result.text.lower() and "4215" in result.text.replace(",", "")
    assert result.mean_confidence is not None and result.mean_confidence > 50 and result.usage is None


def test_tesseract_unavailable_binary(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    engine = TesseractEngine()
    assert not engine.available()
    assert engine.ocr(b"x", langs="eng", timeout=5).error == "engine_unavailable"


# ---- cache and policy ---------------------------------------------------------------------------

@needs_tesseract
def test_cache_hit_avoids_rerunning_engine(tmp_path, scan_jpeg):
    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(build_mixed_pdf([scan_jpeg]))
    engine = FakeEngine(usage={"input_tokens": 5, "output_tokens": 2})
    cache = OcrCache()
    first = ocr_pdf_pages(pdf, [1], langs="eng", deadline=monotonic() + 60, engines=[engine], cache=cache)
    second = ocr_pdf_pages(pdf, [1], langs="eng", deadline=monotonic() + 60, engines=[engine], cache=cache)
    assert engine.calls == 1 and first[1].cached is False and second[1].cached is True
    assert second[1].text == first[1].text and second[1].usage is None
    # a different language set is a different cache key
    ocr_pdf_pages(pdf, [1], langs="eng+deu", deadline=monotonic() + 60, engines=[engine], cache=cache)
    assert engine.calls == 2


@needs_tesseract
def test_disk_cache_under_blob_store(tmp_path, monkeypatch, scan_jpeg):
    monkeypatch.setenv("FORESIGHTNINE_DOCUMENT_STORE_DIR", str(tmp_path / "store"))
    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(build_mixed_pdf([scan_jpeg]))
    engine = FakeEngine()
    ocr_pdf_pages(pdf, [1], langs="eng", deadline=monotonic() + 60, engines=[engine])
    ocr_pdf_pages(pdf, [1], langs="eng", deadline=monotonic() + 60, engines=[engine])
    assert engine.calls == 1 and list((tmp_path / "store" / "ocr-cache").glob("*.json"))


@needs_tesseract
def test_low_confidence_page_falls_back_to_vision_and_records_usage(tmp_path, scan_jpeg):
    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(build_mixed_pdf([scan_jpeg, scanned_page_jpeg(tmp_path, "Net income 987 million", "scan2")]))
    local = FakeEngine(confidence=40.0, text="Rvnue 1O0")
    vision = FakeEngine(name="vision_model", text="Revenue 100", confidence=None, usage={"input_tokens": 900, "output_tokens": 30})
    results = ocr_pdf_pages(pdf, [2, 1], langs="eng", deadline=monotonic() + 60, engines=[local, vision], cache=OcrCache())
    assert {r.engine for r in results.values()} == {"tesseract"} and vision.calls == 0  # no hooks: vision disabled
    hooks, log = recording_hooks()
    results = ocr_pdf_pages(pdf, [2, 1], langs="eng", deadline=monotonic() + 60, engines=[local, vision], cache=OcrCache(),
                            usage=hooks)
    assert set(results) == {1, 2}
    assert sorted(entry[1] for entry in log if entry[0] == "reserve") == ["op:ocr:src:p1:vision_model", "op:ocr:src:p2:vision_model"]
    assert sorted(entry[2] for entry in log if entry[0] == "settle") == [930, 930]
    for result in results.values():
        assert result.engine == "vision_model" and result.text == "Revenue 100"
        assert result.usage == {"input_tokens": 900, "output_tokens": 30}
        assert result.engines_tried == ("tesseract", "vision_model")
    # confident prose never reaches the vision engine
    good, vision2 = FakeEngine(confidence=95.0, text="Plain narrative text here"), FakeEngine(name="vision_model")
    ocr_pdf_pages(pdf, [1], langs="eng", deadline=monotonic() + 60, engines=[good, vision2], cache=OcrCache(),
                  usage=recording_hooks()[0])
    assert vision2.calls == 0


@needs_tesseract
def test_tabular_and_failed_tesseract_use_vision_but_vision_failure_keeps_local(tmp_path, scan_jpeg):
    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(build_mixed_pdf([scan_jpeg]))
    table = "Item 1,200 3,400 5,600\nCost 7 8 9\nNet 10 11 12\nTotal 13 14 15"
    local = FakeEngine(confidence=99.0, text=table)
    vision = FakeEngine(name="vision_model", text="Item | 1,200 | 3,400 | 5,600", confidence=None)
    out = ocr_pdf_pages(pdf, [1], langs="eng", deadline=monotonic() + 60, engines=[local, vision], cache=OcrCache(),
                        usage=recording_hooks()[0])
    assert out[1].engine == "vision_model"
    broken_vision = FakeEngine(name="vision_model", text="", error="http_500", usage={"input_tokens": 3})
    out = ocr_pdf_pages(pdf, [1], langs="eng", deadline=monotonic() + 60, engines=[local, broken_vision], cache=OcrCache(),
                        usage=recording_hooks()[0])
    assert out[1].engine == "tesseract" and out[1].text == table and out[1].usage == {"input_tokens": 3}
    dead_local = FakeEngine(error="engine_failed")
    out = ocr_pdf_pages(pdf, [1], langs="eng", deadline=monotonic() + 60, engines=[dead_local, vision], cache=OcrCache(),
                        usage=recording_hooks()[0])
    assert out[1].engine == "vision_model"


def test_expired_deadline_and_no_engines_return_nothing(tmp_path):
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(build_pdf([None]))
    assert ocr_pdf_pages(pdf, [1], langs="eng", deadline=monotonic() - 1, engines=[FakeEngine()], cache=OcrCache()) == {}
    assert ocr_pdf_pages(pdf, [1], langs="eng", deadline=monotonic() + 30, engines=[], cache=OcrCache()) == {}


# ---- vision engine ------------------------------------------------------------------------------

def vision_engine(transport, **overrides):
    values = dict(base_url="https://vision.example/v1", model="vision-test-model", api_key="sk-secret-ocr-key",
                  allowlist="vision.example:443", max_output_tokens=4096, anthropic_version="2023-06-01",
                  transport=transport)
    values.update(overrides)
    return VisionModelEngine(**values)


def test_vision_engine_request_shape_usage_and_no_secret_logging(caplog):
    seen = {}

    def transport(method, url, headers, body, timeout, **kwargs):
        seen.update(method=method, url=url, headers=dict(headers), body=json.loads(body), timeout=timeout)
        reply = {"content": [{"type": "text", "text": "Revenue | 4,215"}, {"type": "text", "text": "Net | 12"}],
                 "usage": {"input_tokens": 1500, "output_tokens": 40}, "stop_reason": "end_turn"}
        return BinaryHttpResponse(200, json.dumps(reply).encode(), {})

    png = b"\x89PNG-fake-image-bytes"
    with caplog.at_level(logging.DEBUG):
        result = vision_engine(transport).ocr(png, langs="eng", timeout=30)
    assert result.error is None and result.text == "Revenue | 4,215\nNet | 12"
    assert result.usage == {"input_tokens": 1500, "output_tokens": 40} and result.mean_confidence is None
    assert result.engine == "vision_model" and result.engine_version == "vision-test-model"
    assert seen["url"] == "https://vision.example/v1/messages" and seen["method"] == "POST"
    assert seen["headers"]["x-api-key"] == "sk-secret-ocr-key" and seen["headers"]["anthropic-version"] == "2023-06-01"
    body = seen["body"]
    assert body["model"] == "vision-test-model" and body["max_tokens"] == 4096
    content = body["messages"][0]["content"]
    image = next(block for block in content if block["type"] == "image")
    assert image["source"]["type"] == "base64" and image["source"]["media_type"] == "image/png"
    assert base64.b64decode(image["source"]["data"]) == png
    assert "verbatim" in next(b for b in content if b["type"] == "text")["text"]
    assert "sk-secret-ocr-key" not in caplog.text and "repr" not in caplog.text


def test_vision_engine_errors_and_truncation_keep_usage():
    def reply(status, payload):
        return lambda *a, **k: BinaryHttpResponse(status, json.dumps(payload).encode(), {})

    truncated = vision_engine(reply(200, {"content": [{"type": "text", "text": "part"}], "stop_reason": "max_tokens",
                                          "usage": {"input_tokens": 10, "output_tokens": 4096}})).ocr(b"p", langs="eng", timeout=5)
    assert truncated.error == "output_truncated" and truncated.usage == {"input_tokens": 10, "output_tokens": 4096}
    assert vision_engine(reply(500, {})).ocr(b"p", langs="eng", timeout=5).error == "http_500"
    assert vision_engine(reply(200, {"content": []})).ocr(b"p", langs="eng", timeout=5).error == "empty_response"


def test_vision_engine_disabled_unless_fully_configured(monkeypatch):
    assert VisionModelEngine.from_environment() is None
    env = {"FORESIGHTNINE_OCR_VISION_PROTOCOL": "anthropic_messages", "FORESIGHTNINE_OCR_VISION_BASE_URL": "https://vision.example/v1",
           "FORESIGHTNINE_OCR_VISION_MODEL": "m", "FORESIGHTNINE_OCR_VISION_API_KEY": "k",
           "FORESIGHTNINE_OCR_VISION_MAX_OUTPUT_TOKENS": "2048", "FORESIGHTNINE_OCR_VISION_ALLOWED_HOSTS": "vision.example:443",
           "FORESIGHTNINE_OCR_VISION_ANTHROPIC_VERSION": "v"}
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert VisionModelEngine.from_environment().available()
    monkeypatch.setenv("FORESIGHTNINE_OCR_VISION_ALLOWED_HOSTS", "other.example:443")
    assert VisionModelEngine.from_environment() is None
    monkeypatch.setenv("FORESIGHTNINE_OCR_VISION_ALLOWED_HOSTS", "vision.example:443")
    monkeypatch.setenv("FORESIGHTNINE_OCR_VISION_PROTOCOL", "openai_chat_completions")
    assert VisionModelEngine.from_environment() is None
    monkeypatch.setenv("FORESIGHTNINE_OCR_VISION_PROTOCOL", "anthropic_messages")
    monkeypatch.delenv("FORESIGHTNINE_OCR_VISION_API_KEY")
    assert VisionModelEngine.from_environment() is None

    # OpenAI-compatible protocol requires no anthropic_version.
    monkeypatch.setenv("FORESIGHTNINE_OCR_VISION_PROTOCOL", "openai_compatible")
    monkeypatch.setenv("FORESIGHTNINE_OCR_VISION_API_KEY", "k")
    monkeypatch.delenv("FORESIGHTNINE_OCR_VISION_ANTHROPIC_VERSION", raising=False)
    oai_engine = VisionModelEngine.from_environment()
    assert oai_engine is not None and oai_engine.available() and oai_engine.protocol == "openai_compatible"


def test_vision_engine_openai_compatible_request_usage_errors_and_cache(tmp_path, caplog):
    seen = []

    def transport(method, url, headers, body, timeout, **kwargs):
        seen.append({"method": method, "url": url, "headers": dict(headers), "body": json.loads(body), "timeout": timeout})
        reply = {
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "Revenue | 4,215\nNet | 12"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 1200, "completion_tokens": 35, "total_tokens": 1235},
        }
        return BinaryHttpResponse(200, json.dumps(reply).encode(), {})

    engine = VisionModelEngine(
        base_url="https://vision.example/v1",
        model="vision-oai-model",
        api_key="sk-secret-oai-key",
        allowlist="vision.example:443",
        max_output_tokens=2048,
        protocol="openai_compatible",
        transport=transport,
    )
    png = b"\x89PNG-fake-openai-image"
    with caplog.at_level(logging.DEBUG):
        result = engine.ocr(png, langs="eng", timeout=20)
    assert result.error is None and result.text == "Revenue | 4,215\nNet | 12"
    assert result.usage == {"input_tokens": 1200, "output_tokens": 35}
    assert result.engine == "vision_model" and result.engine_version == "vision-oai-model"
    assert seen[0]["method"] == "POST" and seen[0]["url"] == "https://vision.example/v1/chat/completions"
    assert seen[0]["headers"]["Authorization"] == "Bearer sk-secret-oai-key"
    assert "x-api-key" not in seen[0]["headers"] and "anthropic-version" not in seen[0]["headers"]
    content = seen[0]["body"]["messages"][0]["content"]
    img_part = next(part for part in content if part["type"] == "image_url")
    assert img_part["image_url"]["url"] == "data:image/png;base64," + base64.b64encode(png).decode("ascii")
    assert "verbatim" in next(part for part in content if part["type"] == "text")["text"]
    assert "sk-secret-oai-key" not in caplog.text

    # Error handling: length truncation preserves usage, HTTP 500, empty content.
    def reply(status, payload):
        return VisionModelEngine(
            base_url="https://vision.example/v1/chat/completions",
            model="vision-oai-model",
            api_key="sk-secret-oai-key",
            allowlist="vision.example:443",
            max_output_tokens=2048,
            protocol="openai_compatible",
            transport=lambda *a, **k: BinaryHttpResponse(status, json.dumps(payload).encode(), {}),
        )

    truncated = reply(200, {
        "choices": [{"message": {"content": "partial"}, "finish_reason": "length"}],
        "usage": {"prompt_tokens": 50, "completion_tokens": 2048},
    }).ocr(b"p", langs="eng", timeout=5)
    assert truncated.error == "output_truncated" and truncated.usage == {"input_tokens": 50, "output_tokens": 2048}
    assert reply(500, {}).ocr(b"p", langs="eng", timeout=5).error == "http_500"
    assert reply(200, {"choices": [{"message": {"content": ""}, "finish_reason": "stop"}]}).ocr(b"p", langs="eng", timeout=5).error == "empty_response"

    # OcrUsageHooks + OcrCache integration: second run hits cache without calling transport or reserving.
    from hashlib import sha256
    cache = OcrCache(tmp_path / "ocr-cache")
    hooks, log = recording_hooks("op:ocr:oai")
    png_hash = sha256(png).hexdigest()
    first = research_ocr._run_vision(engine, png, png_hash, "eng", 20.0, cache, hooks, 1)
    second = research_ocr._run_vision(engine, png, png_hash, "eng", 20.0, cache, hooks, 2)
    assert first.error is None and first.cached is False and first.usage == {"input_tokens": 1200, "output_tokens": 35}
    assert second.error is None and second.cached is True and second.usage is None and second.text == first.text
    assert len(seen) == 2  # 1 direct ocr() call + 1 uncached _run_vision call
    assert [entry[0] for entry in log] == ["reserve", "settle"]
    assert log[1][2] == 1235


# ---- merge --------------------------------------------------------------------------------------

def test_merge_rebuilds_offsets_and_totals_usage():
    from app.research_documents import page_map_from_text
    text = "Alpha page\f\fGamma page\f"
    pages = page_map_from_text(text, original_retained=True)
    assert [p[2] for p in pages["pages"]] == ["text", "needs_ocr", "text"]
    ocr = OcrPageResult("Scanned beta words", 88.4, 2, "tesseract", "5.3", "eng")
    paid = OcrPageResult("", None, 0, "vision_model", "m", "eng", usage={"input_tokens": 7, "output_tokens": 1}, error="empty_response")
    merged, doc = merge_ocr_into_document(text, pages, {2: ocr, 9: paid})
    assert merged == "Alpha page\fScanned beta words\fGamma page\f"
    assert doc["pages"] == [[0, 10, "text"], [11, 29, "ocr"], [30, 40, "text"]] and doc["version"] == 2
    assert doc["ocr"]["2"] == {"engine": "tesseract", "engine_version": "5.3", "langs": "eng",
                               "mean_confidence": 88.4, "low_confidence_count": 2}
    assert doc["ocr_usage"] == {"pages": 1, "engines": {"tesseract": 1}, "cached_pages": 0,
                                "llm_input_tokens": 7, "llm_output_tokens": 1}
    assert merged[11:29] == "Scanned beta words" and page_for_offset(doc, 15) == 2
    # a failed result leaves the page as needs_ocr and the text unchanged
    same, doc2 = merge_ocr_into_document(text, pages, {2: paid})
    assert same == text and doc2["pages"][1][2] == "needs_ocr" and "2" not in doc2["ocr"]


# ---- ingestion ----------------------------------------------------------------------------------

@needs_poppler
@needs_tesseract
def test_scanned_small_pdf_becomes_source_with_ocr_pages(server, local_transport, scan_jpeg):
    server["body"] = build_mixed_pdf([scan_jpeg, None, scan_jpeg])
    doc = fetch_source_document(req())
    pages = doc.pages
    assert pages["version"] == 2 and pages["page_count"] == 3 and doc.original_pdf_base64
    assert [p[2] for p in pages["pages"]] == ["ocr", "needs_ocr", "ocr"]
    first, _, third = pages["pages"]
    assert "revenue" in doc.text[first[0]:first[1]].lower() and "revenue" in doc.text[third[0]:third[1]].lower()
    assert set(pages["ocr"]) == {"1", "3"} and pages["ocr"]["1"]["engine"] == "tesseract"
    assert pages["ocr_usage"]["pages"] == 2 and pages["ocr_usage"]["engines"] == {"tesseract": 2}
    assert pages["ocr_usage"]["llm_input_tokens"] == 0
    assert page_for_offset(pages, third[0] + 2) == 3


@needs_poppler
@needs_tesseract
def test_scanned_large_pdf_mixed_pages_and_max_pages(server, local_transport, scan_jpeg, monkeypatch):
    monkeypatch.setenv("FORESIGHTNINE_OCR_INGEST_MAX_PAGES", "1")
    server["body"] = build_mixed_pdf(["Born digital page one", scan_jpeg, scan_jpeg], padding=6_000_000)
    doc = fetch_source_document(req())
    pages = doc.pages
    assert doc.original_pdf_base64 is None
    assert [p[2] for p in pages["pages"]] == ["text", "ocr", "needs_ocr"]
    assert doc.text[pages["pages"][0][0]:pages["pages"][0][1]].strip() == "Born digital page one"
    assert "revenue" in doc.text[pages["pages"][1][0]:pages["pages"][1][1]].lower()
    assert pages["pages"][2][0] == pages["pages"][2][1]
    assert doc.text.count("\f") == 3 and set(pages["ocr"]) == {"2"}


@needs_poppler
def test_no_engine_keeps_ocr_required(server, local_transport, monkeypatch):
    import app.research_ocr
    monkeypatch.setattr(app.research_ocr, "default_engines", lambda: [])
    for padding in (0, 6_000_000):
        server["body"] = build_mixed_pdf([None, None], padding=padding)
        with pytest.raises(FetchFailure) as error:
            fetch_source_document(req())
        assert error.value.code == "ocr_required"


@needs_poppler
def test_nothing_ocred_keeps_ocr_required_with_engine(server, local_transport, monkeypatch):
    import app.research_ocr
    monkeypatch.setattr(app.research_ocr, "default_engines", lambda: [FakeEngine(text="", confidence=None)])
    server["body"] = build_mixed_pdf([None, None])
    with pytest.raises(FetchFailure) as error:
        fetch_source_document(req())
    assert error.value.code == "ocr_required"


@needs_poppler
def test_large_original_retained_when_store_configured(server, local_transport, monkeypatch, tmp_path):
    from hashlib import sha256
    raw = build_pdf(["Alpha page"] * 2, padding=6_000_000)
    server["body"] = raw
    doc = fetch_source_document(req())
    assert doc.pages["original_retained"] is False and "original_blob_sha256" not in doc.pages
    monkeypatch.setenv("FORESIGHTNINE_DOCUMENT_STORE_DIR", str(tmp_path / "store"))
    doc = fetch_source_document(req())
    digest = sha256(raw).hexdigest()
    assert doc.pages["original_retained"] is True and doc.pages["original_blob_sha256"] == digest
    assert doc.original_pdf_base64 is None
    assert DocumentBlobStore(tmp_path / "store").path_for(digest).read_bytes() == raw


@needs_poppler
def test_ingestion_vision_ocr_needs_usage_hooks_and_bills_each_page(server, local_transport, scan_jpeg, monkeypatch):
    engines = [FakeEngine(error="engine_failed"),
               FakeEngine(name="vision_model", text="Revenue 4215", confidence=None,
                          usage={"input_tokens": 800, "output_tokens": 20})]
    monkeypatch.setattr(research_ocr, "default_engines", lambda: list(engines))
    server["body"] = build_mixed_pdf(["Born digital page one", scan_jpeg])
    # Without hooks the vision engine never runs: the scanned page stays unreadable.
    plain = fetch_source_document(req())
    assert [p[2] for p in plain.pages["pages"]] == ["text", "needs_ocr"] and engines[1].calls == 0
    hooks, log = recording_hooks("op:ocr:source_x")
    doc = fetch_source_document(req(), ocr_usage=hooks)
    assert [p[2] for p in doc.pages["pages"]] == ["text", "ocr"] and engines[1].calls == 1
    assert log == [("reserve", "op:ocr:source_x:p2:vision_model", research_ocr.vision_input_token_estimate()),
                   ("settle", "op:ocr:source_x:p2:vision_model", 820)]
    assert hooks.events[0]["status"] == "settled" and hooks.events[0]["page"] == 2
    assert doc.pages["ocr_usage"]["llm_input_tokens"] == 800 and doc.pages["ocr_usage"]["llm_output_tokens"] == 20
